"""Routes of the Connect flow (ADR-0046 §3 steps 4, 6, 7).

- GET  /askariane/connect/callback?code&state   back from askariane.ai
- GET  /askariane/connect/confirm/<id>          the final confirmation screen
- POST /askariane/connect/confirm/<id>/decide   Confirm or Cancel (Odoo CSRF)

Every route checks base.group_system on the server, and the enrolment checks
that the caller is the administrator who started it. Responses carry
``Cache-Control: no-store`` and ``Referrer-Policy: no-referrer``; the callback
answers with a redirect to a clean URL, so the code and state leave the
address bar. Nothing here logs a code, a state, a verifier or a body.
"""
import logging

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from markupsafe import escape

from ..models import wire
from ..models.connect_service import check_system

_logger = logging.getLogger(__name__)

PANEL_URL = '/odoo/action-askariane_connect.action_connect_panel'
SAFE_HEADERS = [
    ('Cache-Control', 'no-store'),
    ('Referrer-Policy', 'no-referrer'),
    ('X-Content-Type-Options', 'nosniff'),
]


def _headers(extra=()):
    return SAFE_HEADERS + list(extra)


def _page(status, title, text):
    html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        f'<title>{escape(title)}</title></head>'
        '<body style="font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px;">'
        f'<h2>{escape(title)}</h2><p>{escape(text)}</p>'
        f'<p><a href="{PANEL_URL}">Back to Ask Ariane &gt; Connection</a></p></body></html>'
    )
    return request.make_response(html, headers=_headers([('Content-Type', 'text/html; charset=utf-8')]),
                                 status=status)


def _redirect(location):
    return request.make_response('', headers=_headers([('Location', location)]), status=303)


def _forbidden():
    return _page(403, 'Not allowed',
                 'Only the Odoo administrator who started this connection can continue it.')


class AskArianeConnectController(http.Controller):

    @http.route('/askariane/connect/callback', type='http', auth='user', methods=['GET'])
    def callback(self, code=None, state=None, **kwargs):
        env = request.env
        try:
            check_system(env)
            enrolment, outcome = env['askariane.connect.enrolment']._take_for_callback(state)
        except AccessError:
            _logger.warning('askariane_connect: callback refused for user %s (not an administrator)', env.uid)
            return _forbidden()
        if not enrolment:
            _logger.warning('askariane_connect: callback refused for user %s (%s)', env.uid, outcome)
            return _page(400, 'This link cannot be used',
                         'It has expired, was already used, or was started by someone else. '
                         'Start again from Ask Ariane > Connection.')
        enrolment._exchange(code)
        return _redirect(f'/askariane/connect/confirm/{enrolment.id}')

    def _enrolment(self, enrolment_id):
        check_system(request.env)
        enrolment = request.env['askariane.connect.enrolment'].sudo().browse(enrolment_id).exists()
        if not enrolment:
            raise AccessError('Unknown connection attempt.')
        # Back to the caller's own environment, so the initiator check binds to them.
        return request.env['askariane.connect.enrolment'].browse(enrolment.id)

    @http.route('/askariane/connect/confirm/<int:enrolment_id>', type='http', auth='user', methods=['GET'])
    def confirm_page(self, enrolment_id, **kwargs):
        try:
            enrolment = self._enrolment(enrolment_id)
            values = enrolment._confirm_values()
        except AccessError:
            return _forbidden()
        record = values['enrolment']
        if record.status == 'done':
            return _redirect(PANEL_URL)
        if record.status == 'failed':
            return _page(200, 'Not connected',
                         'Ask Ariane did not complete the connection. '
                         f'{wire.error_text(record.error or "unavailable")} '
                         'Nothing was stored. Start again from Ask Ariane > Connection.')
        if record.status != 'exchanged' or values['expired']:
            return _page(200, 'Not connected', 'This connection attempt has expired. Start again.')
        values['csrf_token'] = request.csrf_token()
        values['panel_url'] = PANEL_URL
        html = request.env['ir.qweb']._render('askariane_connect.confirm_page', values)
        return request.make_response(html, headers=_headers([('Content-Type', 'text/html; charset=utf-8')]))

    @http.route('/askariane/connect/confirm/<int:enrolment_id>/decide', type='http', auth='user',
                methods=['POST'])
    def confirm_decide(self, enrolment_id, decision=None, **kwargs):
        try:
            enrolment = self._enrolment(enrolment_id)
            if decision == 'confirm':
                ok = enrolment.action_confirm()
            else:
                enrolment.action_cancel()
                return _redirect(PANEL_URL)
        except AccessError:
            return _forbidden()
        except UserError as error:
            return _page(400, 'Not connected', str(error.args[0] if error.args else 'Start again.'))
        if not ok:
            return _redirect(f'/askariane/connect/confirm/{enrolment_id}')
        return _redirect(PANEL_URL)
