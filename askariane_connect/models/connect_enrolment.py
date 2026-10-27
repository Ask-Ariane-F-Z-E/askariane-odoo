"""One Connect attempt (ADR-0046 §3, steps 1, 4, 6, 7).

One record per attempt, so two administrators never overwrite each other. It
holds the initiating user, sha256(state) (never the state itself), the PKCE
verifier, the new key pair, the ticked companies, the origin and a 10-minute
expiry. Only the user who started it may finish it.

The private key and the verifier are wiped from the record as soon as the
attempt is done or has failed; the key then lives only in the connection
parameters (see connect_service.py).
"""
import logging
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from . import wire
from .connect_service import (MAX_COMPANIES, P_ACCOUNT_EMAIL, P_COMPANY_IDS, P_EXPIRES_AT,
                              P_GENERATION, P_KEY_ID, P_ORIGIN, P_PENDING_KEY, P_PRIVATE_KEY,
                              P_TENANT_ID, SNAPSHOT_COMPANY_KEYS, check_system, parse_iso)

_logger = logging.getLogger(__name__)

TTL = timedelta(minutes=10)
STATE_RE = re.compile(r'^[A-Za-z0-9_-]{16,64}$')
CODE_RE = re.compile(r'^[A-Za-z0-9._~-]{1,512}$')
SWEEP_AFTER = timedelta(days=1)


def _short_str(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit


class AskArianeConnectEnrolment(models.Model):
    _name = 'askariane.connect.enrolment'
    _description = 'Ask Ariane connect attempt'
    _order = 'id desc'

    user_id = fields.Many2one('res.users', required=True, readonly=True, ondelete='cascade',
                              string='Started by')
    state_hash = fields.Char(required=True, readonly=True, index=True, groups='base.group_system')
    verifier = fields.Char(readonly=True, groups='base.group_system')
    private_key = fields.Char(readonly=True, groups='base.group_system')
    public_key = fields.Char(readonly=True)
    company_ids = fields.Many2many('res.company', 'askariane_connect_enrolment_company_rel',
                                   'enrolment_id', 'company_id', readonly=True, string='Companies')
    origin = fields.Char(required=True, readonly=True)
    expires_at = fields.Datetime(required=True, readonly=True)
    status = fields.Selection([
        ('started', 'Started'),
        ('exchanging', 'Exchanging'),
        ('exchanged', 'Waiting for confirmation'),
        ('done', 'Connected'),
        ('failed', 'Failed'),
    ], required=True, default='started', readonly=True)
    error = fields.Char(readonly=True)
    # What the exchange answered (no secret in it).
    remote_enrolment_id = fields.Char(readonly=True)
    key_id = fields.Char(readonly=True)
    account_email = fields.Char(readonly=True)
    tenant_display_name = fields.Char(readonly=True)
    scope = fields.Char(readonly=True)

    # ---------------------------------------------------------------- helpers

    def _expired(self):
        self.ensure_one()
        return not self.expires_at or self.expires_at <= fields.Datetime.now()

    def _mark_failed(self, error):
        self.sudo().write({'status': 'failed', 'error': error, 'private_key': False, 'verifier': False})

    def _check_initiator(self):
        """Settings administrator AND the user who started this attempt."""
        self.ensure_one()
        check_system(self.env)
        record = self.sudo()
        if record.user_id.id != self.env.uid:
            raise AccessError('Only the administrator who started this connection can finish it.')
        return record

    @api.model
    def _sweep_old(self):
        cutoff = fields.Datetime.now() - SWEEP_AFTER
        self.sudo().search([('create_date', '<', cutoff)]).unlink()

    # ---------------------------------------------------------------- step 1

    @api.model
    def _start(self, companies):
        """Create the attempt and return the URL the browser goes to."""
        check_system(self.env)
        service = self.env['askariane.connect']
        if service._connection():
            raise UserError('This Odoo is already connected. Disconnect first.')
        companies = companies.sorted('id')
        if not 1 <= len(companies) <= MAX_COMPANIES:
            raise UserError(f'Tick between 1 and {MAX_COMPANIES} companies.')
        if companies - self.env.user.company_ids:
            raise AccessError('You can only connect companies you have access to.')
        origin = service._current_origin()
        if not origin:
            raise UserError(wire.origin_problem(service._icp().get_param('web.base.url'))
                            or 'The Odoo address (web.base.url) must be an https address.')
        db_uuid = self.env['ir.config_parameter'].sudo().get_param('database.uuid') or ''
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(32)
        private_key = wire.new_private_key()
        record = self.sudo().create({
            'user_id': self.env.uid,
            'state_hash': wire.sha256_hex(state),
            'verifier': verifier,
            'private_key': private_key,
            'public_key': wire.public_key_of(private_key),
            'company_ids': [(6, 0, companies.ids)],
            'origin': origin,
            'expires_at': fields.Datetime.now() + TTL,
        })
        _logger.info('askariane_connect: enrolment %s started by user %s', record.id, self.env.uid)
        query = urlencode([
            ('state', state),
            ('challenge', wire.pkce_challenge(verifier)),
            ('origin', origin),
            ('db_uuid', db_uuid),
            ('company', (self.env.company.display_name or '')[:120]),
            ('company_ids', ','.join(str(cid) for cid in companies.ids)),
        ])
        return wire.connect_page() + '?' + query

    # ---------------------------------------------------------------- step 4

    @api.model
    def _take_for_callback(self, state):
        """The attempt this state belongs to, locked and consumed (status
        'exchanging'), or an empty recordset with nothing changed when the
        state is unknown, expired, already used, or not this user's."""
        check_system(self.env)
        empty = self.browse()
        if not isinstance(state, str) or not STATE_RE.match(state):
            return empty, 'bad_state'
        record = self.sudo().search([('state_hash', '=', wire.sha256_hex(state))], limit=1)
        if not record:
            return empty, 'unknown_state'
        # Lock the row so two callbacks for the same attempt cannot both pass.
        self.env.cr.execute(
            'SELECT id FROM askariane_connect_enrolment WHERE id = %s FOR UPDATE NOWAIT', [record.id])
        record.invalidate_recordset()
        if record.status != 'started':
            return empty, 'already_used'
        if record._expired():
            return empty, 'expired'
        if record.user_id.id != self.env.uid:
            return empty, 'other_user'
        record.write({'status': 'exchanging'})
        return record, 'ok'

    def _exchange(self, code):
        """Server-to-server code exchange; stores the answer on the attempt."""
        self.ensure_one()
        record = self.sudo()
        if record.status != 'exchanging':
            raise UserError('This connection attempt cannot be exchanged.')
        if not isinstance(code, str) or not CODE_RE.match(code):
            record._mark_failed('code_invalid')
            return False
        status, data = wire.post_exchange({
            'code': code,
            'verifier': record.verifier,
            'public_key': record.public_key,
            'origin': record.origin,
        })
        if status != 200:
            record._mark_failed(wire.error_code(data))
            _logger.warning('askariane_connect: enrolment %s exchange refused (%s, %s)',
                            record.id, status, wire.error_code(data))
            return False
        scope = data.get('scope')
        company_ids = data.get('company_ids')
        valid = (
            _short_str(data.get('enrolment_id'), 64)
            and _short_str(data.get('key_id'), 128)
            and _short_str(data.get('account_email'), 254)
            and isinstance(data.get('tenant_display_name', ''), str)
            and isinstance(scope, list) and len(scope) <= 50
            and all(_short_str(item, 64) for item in scope)
            and isinstance(company_ids, list)
            and all(isinstance(cid, int) and not isinstance(cid, bool) for cid in company_ids)
            and sorted(company_ids) == sorted(record.company_ids.ids)
            and parse_iso(data.get('ack_expires_at'))
        )
        if not valid:
            record._mark_failed('bad_response')
            _logger.warning('askariane_connect: enrolment %s exchange answer refused', record.id)
            return False
        record.write({
            'status': 'exchanged',
            'remote_enrolment_id': data['enrolment_id'],
            'key_id': data['key_id'],
            'account_email': data['account_email'],
            'tenant_display_name': (data.get('tenant_display_name') or '')[:200],
            'scope': ','.join(scope),
            'verifier': False,
            # Ten minutes from now to read and confirm.
            'expires_at': fields.Datetime.now() + TTL,
        })
        _logger.info('askariane_connect: enrolment %s exchanged', record.id)
        return True

    # ------------------------------------------------------------- steps 6, 7

    def _confirm_values(self):
        """What the confirmation screen shows. Initiator only."""
        record = self._check_initiator()
        return {
            'enrolment': record,
            'account_email': record.account_email,
            'tenant_display_name': record.tenant_display_name,
            'companies': record.company_ids.sorted('id'),
            'origin': record.origin,
            'scope': [item for item in (record.scope or '').split(',') if item],
            'fields': ['odoo_version', 'modules'] + list(SNAPSHOT_COMPANY_KEYS),
            'expired': record._expired(),
        }

    def action_confirm(self):
        """Signed ack (generation 1); on success store the connection."""
        record = self._check_initiator()
        service = self.env['askariane.connect']
        if record.status != 'exchanged' or record._expired():
            raise UserError('This connection attempt has expired or was already used. Start again.')
        if service._connection():
            raise UserError('This Odoo is already connected. Disconnect first.')
        if not service._origin_ok(record.origin):
            record._mark_failed('origin_changed')
            return False
        status, data = wire.post_signed(
            'ack', lambda nonce: {'enrolment_id': record.remote_enrolment_id},
            record.private_key, record.key_id, 1, record.origin)
        company_ids = data.get('company_ids')
        valid = (
            status == 200
            and _short_str(data.get('tenant_id'), 128)
            and data.get('key_id') == record.key_id
            and data.get('generation') == 1 and not isinstance(data.get('generation'), bool)
            and isinstance(company_ids, list)
            and all(isinstance(cid, int) and not isinstance(cid, bool) for cid in company_ids)
            and sorted(company_ids) == sorted(record.company_ids.ids)
            and parse_iso(data.get('expires_at'))
        )
        if not valid:
            error = wire.error_code(data) if status != 200 else 'bad_response'
            record._mark_failed(error)
            _logger.warning('askariane_connect: enrolment %s ack refused (%s, %s)', record.id, status, error)
            return False
        service._store_connection({
            P_PRIVATE_KEY: record.private_key,
            P_PENDING_KEY: False,
            P_KEY_ID: record.key_id,
            P_GENERATION: '1',
            P_TENANT_ID: data['tenant_id'],
            P_ORIGIN: record.origin,
            P_COMPANY_IDS: ','.join(str(cid) for cid in sorted(record.company_ids.ids)),
            P_EXPIRES_AT: data['expires_at'],
            P_ACCOUNT_EMAIL: record.account_email,
        })
        record.write({'status': 'done', 'private_key': False, 'verifier': False})
        _logger.info('askariane_connect: enrolment %s confirmed; connected', record.id)
        return True

    def action_cancel(self):
        record = self._check_initiator()
        if record.status in ('started', 'exchanging', 'exchanged'):
            record._mark_failed('cancelled')
        _logger.info('askariane_connect: enrolment %s cancelled', record.id)
        return True
