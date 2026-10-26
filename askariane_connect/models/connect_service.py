"""The connection once it exists: where its state lives, the signed calls,
key rotation, the daily snapshot and Disconnect.

State lives in ir.config_parameter under ``askariane_connect.``; data/neutralize.sql
wipes every one of those keys on a staging or dev copy.

Every signed call first compares this Odoo's current web.base.url origin with
the origin pinned at connect. If they differ, nothing is sent (no HTTP call at
all) and the key is kept: a restored copy elsewhere goes quiet.
"""
import logging
import re
from datetime import date, datetime, timedelta, timezone

from odoo import api, models, release
from odoo.exceptions import AccessError

from . import wire

_logger = logging.getLogger(__name__)

PARAM_PREFIX = 'askariane_connect.'
P_PRIVATE_KEY = PARAM_PREFIX + 'private_key'
P_PENDING_KEY = PARAM_PREFIX + 'pending_private_key'
P_KEY_ID = PARAM_PREFIX + 'key_id'
P_GENERATION = PARAM_PREFIX + 'generation'
P_TENANT_ID = PARAM_PREFIX + 'tenant_id'
P_ORIGIN = PARAM_PREFIX + 'origin'
P_COMPANY_IDS = PARAM_PREFIX + 'company_ids'
P_EXPIRES_AT = PARAM_PREFIX + 'expires_at'
P_ACCOUNT_EMAIL = PARAM_PREFIX + 'account_email'
ALL_PARAMS = (P_PRIVATE_KEY, P_PENDING_KEY, P_KEY_ID, P_GENERATION, P_TENANT_ID, P_ORIGIN,
              P_COMPANY_IDS, P_EXPIRES_AT, P_ACCOUNT_EMAIL)

MAX_COMPANIES = 10
ROTATE_WHEN_LEFT = timedelta(days=30)
COUNT_CAP = 10 ** 9
TOTAL_CAP = 10 ** 12
MODULE_RE = re.compile(r'^[a-z0-9_]{1,64}$')
VERSION_RE = re.compile(r'^\d{2}\.\d$')
MAX_MODULES = 400
SNAPSHOT_COMPANY_KEYS = ('odoo_company_id', 'customers', 'products', 'users',
                         'open_quotations', 'open_invoices', 'monthly_sales')


def check_system(env):
    """Server-side gate for every button and route: Settings administrators only."""
    if not env.user.has_group('base.group_system'):
        raise AccessError('Only an Odoo administrator (Settings) can manage the Ask Ariane connection.')


def parse_ids(text):
    ids = []
    for part in (text or '').split(','):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return sorted(set(ids))


def parse_iso(text):
    if not text or not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


class AskArianeConnect(models.AbstractModel):
    _name = 'askariane.connect'
    _description = 'Ask Ariane connection service'

    # ------------------------------------------------------------------ state

    def _icp(self):
        return self.env['ir.config_parameter'].sudo()

    @api.model
    def _connection(self):
        """The stored connection, or None when not connected."""
        icp = self._icp()
        values = {
            'private_key': icp.get_param(P_PRIVATE_KEY) or '',
            'pending_private_key': icp.get_param(P_PENDING_KEY) or '',
            'key_id': icp.get_param(P_KEY_ID) or '',
            'generation': icp.get_param(P_GENERATION) or '',
            'tenant_id': icp.get_param(P_TENANT_ID) or '',
            'origin': icp.get_param(P_ORIGIN) or '',
            'company_ids': parse_ids(icp.get_param(P_COMPANY_IDS)),
            'expires_at': icp.get_param(P_EXPIRES_AT) or '',
            'account_email': icp.get_param(P_ACCOUNT_EMAIL) or '',
        }
        if not (values['private_key'] and values['key_id'] and values['tenant_id']
                and values['origin'] and values['generation'].isdigit()):
            return None
        values['generation'] = int(values['generation'])
        return values

    @api.model
    def _current_origin(self):
        return wire.origin_of(self._icp().get_param('web.base.url'))

    @api.model
    def _origin_ok(self, pinned):
        current = self._current_origin()
        if not pinned or current != pinned:
            _logger.warning('askariane_connect: web.base.url origin differs from the origin pinned at '
                            'connect; nothing sent, key kept')
            return False
        return True

    @api.model
    def _store_connection(self, values):
        icp = self._icp()
        for key, value in values.items():
            icp.set_param(key, value)

    @api.model
    def _wipe_connection(self):
        icp = self._icp()
        for key in ALL_PARAMS:
            icp.set_param(key, False)

    @api.model
    def _store_pending_key_now(self, private_key):
        """Store the pending key in its own committed transaction, before the
        rotate call: if this run then fails, the key the server may already
        hold is not lost with the rollback."""
        self._in_new_cursor(lambda svc: svc._icp().set_param(P_PENDING_KEY, private_key))
        return private_key

    @api.model
    def _in_new_cursor(self, function):
        """Run ``function(service)`` in a fresh transaction that commits on its
        own. A fresh one also sees what an earlier one committed (Odoo cursors
        are REPEATABLE READ, so the cron's own cursor would not)."""
        with self.env.registry.cursor() as cr:
            result = function(self.env(cr=cr)['askariane.connect'])
        self.env.registry.clear_cache()
        return result

    # ------------------------------------------------------------ signed call

    @api.model
    def _signed_call(self, name, body_for_nonce, allow_pending_retry=True):
        """(status, data) of one signed call with the stored key, or (None,
        {'error': 'origin_changed'}) when the origin pin stops it before any
        HTTP call. On generation_mismatch with a pending key, retries once with
        the pending key and generation + 1, and promotes it if that works."""
        conn = self._connection()
        if not conn:
            return None, {'error': 'not_connected'}
        if not self._origin_ok(conn['origin']):
            return None, {'error': 'origin_changed'}
        status, data = wire.post_signed(name, body_for_nonce, conn['private_key'], conn['key_id'],
                                        conn['generation'], conn['origin'])
        if (allow_pending_retry and status != 200 and conn['pending_private_key']
                and data.get('error') == 'generation_mismatch'):
            status, data = wire.post_signed(name, body_for_nonce, conn['pending_private_key'],
                                            conn['key_id'], conn['generation'] + 1, conn['origin'])
            if status == 200:
                # The server took an earlier rotate whose answer was lost. Its
                # new expiry is unknown here, so expires_at is cleared and the
                # next run rotates again to learn a real one.
                self._store_connection({
                    P_PRIVATE_KEY: conn['pending_private_key'],
                    P_GENERATION: str(conn['generation'] + 1),
                    P_PENDING_KEY: False,
                    P_EXPIRES_AT: False,
                })
                _logger.info('askariane_connect: pending key promoted after generation_mismatch')
        _logger.info('askariane_connect: %s answered %s', name, status)
        return status, data

    # --------------------------------------------------------------- rotation

    @api.model
    def _needs_rotation(self, conn):
        if conn['pending_private_key']:
            return True
        expires = parse_iso(conn['expires_at'])
        return expires is None or expires - datetime.now(timezone.utc) < ROTATE_WHEN_LEFT

    @api.model
    def _rotate(self, new_key):
        """Signed rotate to ``new_key`` (already stored as the pending key),
        signed with the current key and generation. True when promoted."""
        conn = self._connection()
        if not conn or not self._origin_ok(conn['origin']):
            return False

        def body(nonce):
            return {'new_public_key': wire.public_key_of(new_key),
                    'proof': wire.rotate_proof(new_key, conn['key_id'], conn['generation'], nonce)}

        status, data = self._signed_call('rotate', body, allow_pending_retry=False)
        if status == 200 and data.get('generation') == conn['generation'] + 1 \
                and parse_iso(data.get('expires_at')):
            self._store_connection({
                P_PRIVATE_KEY: new_key,
                P_GENERATION: str(conn['generation'] + 1),
                P_EXPIRES_AT: data['expires_at'],
                P_PENDING_KEY: False,
            })
            _logger.info('askariane_connect: key rotated to generation %s', conn['generation'] + 1)
            return True
        _logger.warning('askariane_connect: rotate not done (%s); pending key kept', wire.error_code(data))
        return False

    # --------------------------------------------------------------- snapshot

    @api.model
    def _count(self, model, domain):
        if model not in self.env:
            return 0
        return min(self.env[model].sudo().search_count(domain), COUNT_CAP)

    @api.model
    def _company_counts(self, company_id):
        """Counts for ONE company. Every domain carries an explicit
        company_id = <this company>; records shared across companies (company
        not set) are deliberately not counted, so nothing outside the approved
        company can be read."""
        partner_fields = self.env['res.partner']._fields
        customers = 0
        if 'customer_rank' in partner_fields:
            customers = self._count('res.partner', [('company_id', '=', company_id),
                                                    ('customer_rank', '>', 0)])
        return {
            'odoo_company_id': company_id,
            'customers': customers,
            'products': self._count('product.template', [('company_id', '=', company_id)]),
            'users': self._count('res.users', [('share', '=', False), ('active', '=', True),
                                               ('company_ids', 'in', [company_id])]),
            'open_quotations': self._count('sale.order', [('company_id', '=', company_id),
                                                          ('state', 'in', ('draft', 'sent'))]),
            'open_invoices': self._count('account.move', [
                ('company_id', '=', company_id), ('move_type', '=', 'out_invoice'),
                ('state', '=', 'posted'), ('payment_state', 'in', ('not_paid', 'partial'))]),
            'monthly_sales': self._monthly_sales(company_id),
        }

    @api.model
    def _monthly_sales(self, company_id):
        """Posted customer invoices, untaxed, per calendar month: this month and
        the 11 before it. Months with no (or a negative) total are left out."""
        if 'account.move' not in self.env:
            return []
        today = date.today()
        year, month = today.year, today.month - 11
        while month < 1:
            month += 12
            year -= 1
        start = date(year, month, 1)
        groups = self.env['account.move'].sudo()._read_group(
            [('company_id', '=', company_id), ('move_type', '=', 'out_invoice'),
             ('state', '=', 'posted'), ('invoice_date', '>=', start), ('invoice_date', '<=', today)],
            ['invoice_date:month'], ['amount_untaxed_signed:sum'])
        rows = []
        for month_start, total in groups:
            if not month_start or not total or total <= 0:
                continue
            rows.append({'month': month_start.strftime('%Y-%m'),
                         'total': round(min(float(total), TOTAL_CAP), 2)})
        rows.sort(key=lambda row: row['month'])
        return rows[-12:]

    @api.model
    def _modules(self):
        names = self.env['ir.module.module'].sudo().search([('state', '=', 'installed')]).mapped('name')
        return sorted({name for name in names if MODULE_RE.match(name)})[:MAX_MODULES]

    @api.model
    def _build_snapshot(self, approved_ids):
        """The snapshot.v1 body for the approved companies only, or None when
        nothing can be sent."""
        existing = self.env['res.company'].sudo().browse(approved_ids).exists()
        company_ids = sorted(existing.ids)[:MAX_COMPANIES]
        if not company_ids or not VERSION_RE.match(release.serie):
            return None
        return {
            'schema': 'snapshot.v1',
            'odoo_version': release.serie,
            'modules': self._modules(),
            'companies': [self._company_counts(cid) for cid in company_ids],
        }

    @api.model
    def _push_snapshot(self):
        conn = self._connection()
        if not conn:
            return None
        payload = self._build_snapshot(conn['company_ids'])
        if payload is None:
            _logger.warning('askariane_connect: no snapshot built (no approved company left, or an '
                            'Odoo version outside snapshot.v1)')
            return None
        status, data = self._signed_call('snapshot', lambda nonce: payload)
        if status != 200:
            _logger.warning('askariane_connect: snapshot not accepted (%s)',
                            data.get('error') if data.get('error') == 'origin_changed'
                            else wire.error_code(data))
        return status

    @api.model
    def _cron_push_snapshot(self):
        """Daily, inside this Odoo. Rotates the key when fewer than 30 days
        remain, then pushes one snapshot. Old enrolments are swept here too."""
        self.env['askariane.connect.enrolment'].sudo()._sweep_old()
        conn = self._connection()
        if not conn:
            return
        if not self._origin_ok(conn['origin']):
            return
        if self._needs_rotation(conn):
            new_key = conn['pending_private_key'] or self._store_pending_key_now(wire.new_private_key())
            self._in_new_cursor(lambda svc: svc._rotate(new_key))
        self._in_new_cursor(lambda svc: svc._push_snapshot())

    # ------------------------------------------------------------- disconnect

    @api.model
    def _disconnect(self):
        """Signed disconnect, then wipe the local key whatever the answer.
        Returns True when Ask Ariane confirmed the revoke."""
        check_system(self.env)
        status, data = self._signed_call('disconnect', lambda nonce: {})
        revoked = status == 200 and data.get('revoked') is True
        self._wipe_connection()
        self.env['askariane.connect.enrolment'].sudo().search(
            [('status', 'in', ('started', 'exchanging', 'exchanged'))])._mark_failed('disconnected')
        _logger.info('askariane_connect: disconnected locally; remote revoke %s',
                     'confirmed' if revoked else 'not confirmed')
        return revoked
