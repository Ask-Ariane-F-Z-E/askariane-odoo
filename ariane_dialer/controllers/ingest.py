"""Server-to-server push of a call transcript from the dialler service.

Closed (404) unless ``ariane_dialer.ingest_key`` is set. The service signs
``<unix ts>.<raw body>`` with HMAC-SHA256 and sends
``X-Ariane-Signature: t=<ts>,v1=<hex>``; a timestamp more than 300 s away
from now, either way, is refused. Errors are fixed codes only.

Closed too (404) unless ``ariane_dialer.tenant_id`` is set.

The push names the Odoo call (``call_id``), the provider's call id, and the
rep (``uid``) and company (``company_id``) the dialler service verified for that
call from Odoo's signed pass; the call must belong to both, so a rep cannot get
a transcript written onto another rep's call by naming its id. The first
accepted push binds the provider id; after that it must match. A rep can never
set it. Writes are ordered by a per-call ``revision`` under a row lock. A purge
is applied whatever its revision, and ``purged`` is a tombstone: no later push
brings a transcript back. After the retention job removed a call's
transcript (``transcript_purged_at``), a new transcript is refused (409 expired).

After the signature passes, this route writes as superuser, and only to the
matched ``ariane.dialer.call`` row, its transcript lines, and one short note on
the call's contact: the AI summary and a link to the call, never the
transcript. That note is posted once per call (``summary_message_id``) and is
deleted when the transcript is withheld or purged.
"""
import hashlib
import hmac
import json
import logging
import time

from markupsafe import Markup

from odoo import http
from odoo.http import request
from odoo.tools import plaintext2html

_logger = logging.getLogger(__name__)

MAX_BODY = 2 * 1024 * 1024
MAX_SEGMENTS = 5000
MAX_TEXT = 200000
MAX_SEGMENT_TEXT = 4000
WINDOW_S = 300
STATES = ('ready', 'withheld', 'purged')
SPEAKERS = ('rep', 'customer', 'unknown')
SUMMARY_NOTE_MAX = 1500


def _post_summary(call):
    """Post the call's AI summary on its contact (or lead) once. Superuser env."""
    target = call.partner_id or call.lead_id
    if call.summary_message_id or not target or not (call.summary or '').strip():
        return
    text = call.summary.strip()
    if len(text) > SUMMARY_NOTE_MAX:
        text = text[:SUMMARY_NOTE_MAX].rstrip() + '…'
    body = plaintext2html(f'Call summary (AI): {text}') + Markup(
        '<p><a href="/odoo/ariane.dialer.call/%s">Open the call record</a></p>') % call.id
    message = target.message_post(body=body, message_type='comment', subtype_xmlid='mail.mt_note',
                                  author_id=call.user_id.partner_id.id)
    call.summary_message_id = message


def _drop_summary(call):
    if call.summary_message_id:
        call.summary_message_id.unlink()


def _reply(status, code, **extra):
    return request.make_json_response({'ok': status == 200, 'code': code, **extra}, status=status)


def verify_signature(key, header, body, now=None):
    """True when header carries a fresh, valid signature of body under key."""
    if not key or not header:
        return False
    parts = dict(p.strip().split('=', 1) for p in header.split(',') if '=' in p)
    try:
        ts = int(parts.get('t', ''))
    except ValueError:
        return False
    now = int(time.time()) if now is None else now
    if abs(now - ts) > WINDOW_S:
        return False
    expect = hmac.new(key.encode(), f'{ts}.'.encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, parts.get('v1', ''))


def _num(value):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _text(value, limit):
    return str(value or '')[:limit] or False


class ArianeDialerIngest(http.Controller):

    @http.route('/ariane_dialer/ingest/transcript', type='http', auth='none', methods=['POST'],
                csrf=False, save_session=False, readonly=False, max_content_length=MAX_BODY)
    def ingest_transcript(self, **_kw):
        if not request.db:
            return _reply(404, 'not_found')
        env = request.env(su=True)
        icp = env['ir.config_parameter']
        key = icp.get_param('ariane_dialer.ingest_key') or ''
        tenant = icp.get_param('ariane_dialer.tenant_id') or ''
        if not key or not tenant:
            return _reply(404, 'not_found')
        httpreq = request.httprequest
        # Size is decided from the declared length before anything is read; a
        # body without a length is refused rather than read without a bound.
        if httpreq.content_length is None:
            return _reply(411, 'length_required')
        if httpreq.content_length > MAX_BODY:
            return _reply(413, 'too_large')
        # Cached: if the transaction is retried after a serialisation failure,
        # Odoo runs this method again and the body must still be there.
        body = httpreq.get_data()
        if not verify_signature(key, httpreq.headers.get('X-Ariane-Signature', ''), body):
            return _reply(401, 'bad_signature')
        try:
            data = json.loads(body)
        except ValueError:
            return _reply(400, 'bad_body')
        if not isinstance(data, dict):
            return _reply(400, 'bad_body')
        if data.get('tenant_id') != tenant:
            return _reply(403, 'bad_tenant')
        state = data.get('transcript_state', 'ready')
        segments = data.get('segments') or []
        provider = str(data.get('provider_call_id') or '')[:128]
        revision, call_id = data.get('revision'), data.get('call_id')
        uid, company_id = data.get('uid'), data.get('company_id')
        if state not in STATES or not isinstance(segments, list) or not provider \
                or type(revision) is not int or revision < 1 or type(call_id) is not int \
                or type(uid) is not int or type(company_id) is not int:
            return _reply(400, 'bad_body')
        if len(segments) > MAX_SEGMENTS:
            return _reply(413, 'too_large')

        call = env['ariane.dialer.call'].browse(call_id).exists()
        if not call:
            return _reply(404, 'unknown_call')
        call._lock()
        if call.user_id.id != uid or call.company_id.id != company_id:
            return _reply(409, 'wrong_call')
        if call.external_call_id and call.external_call_id != provider:
            return _reply(409, 'wrong_call')
        if call.transcript_state == 'purged':
            if state == 'purged':
                return _reply(200, 'unchanged', call_id=call.id)
            return _reply(409, 'purged')
        # Retention removed this call's transcript (models/dialer_retention.py):
        # a late push must not bring it back. A withhold or purge still applies.
        if call.transcript_purged_at and state == 'ready':
            return _reply(409, 'expired')
        # A purge is never ordered behind a transcript: whatever its revision, it wins.
        if revision <= call.transcript_revision and state != 'purged':
            return _reply(200, 'unchanged', call_id=call.id)
        if not call.external_call_id and env['ariane.dialer.call'].search_count([
                ('company_id', '=', call.company_id.id), ('external_call_id', '=', provider)], limit=1):
            return _reply(409, 'provider_in_use')

        call.segment_ids.unlink()
        vals = {'transcript_state': state, 'transcript_revision': max(revision, call.transcript_revision)}
        if not call.external_call_id:
            vals['external_call_id'] = provider
        if state == 'ready':
            vals.update({
                'transcript': _text(data.get('text'), MAX_TEXT),
                'summary': _text(data.get('summary'), 8000),
                'next_action': _text(data.get('next_action'), 2000),
                'language': _text(data.get('language'), 16),
                'transcript_source': _text(data.get('source'), 64),
                'recording_ref': _text(data.get('recording_ref'), 128),
            })
        else:
            # Consent withheld or purged: the Odoo copy goes too.
            vals.update(transcript=False, summary=False, next_action=False, recording_ref=False,
                        language=False, transcript_source=False)
        if state != 'ready':
            _drop_summary(call)
            vals['summary_message_id'] = False
        call.write(vals)
        if state == 'ready':
            _post_summary(call)
            rows = [{'call_id': call.id, 'sequence': i,
                     'start_s': _num(seg.get('start')), 'end_s': _num(seg.get('end')),
                     'speaker': seg.get('speaker') if seg.get('speaker') in SPEAKERS else 'unknown',
                     'text': str(seg['text'])[:MAX_SEGMENT_TEXT]}
                    for i, seg in enumerate(segments)
                    if isinstance(seg, dict) and str(seg.get('text') or '').strip()]
            env['ariane.dialer.call.segment'].create(rows)
        _logger.info('ariane_dialer ingest: call %s transcript %s rev %s', call.id, state, revision)
        return _reply(200, 'ok', call_id=call.id)
