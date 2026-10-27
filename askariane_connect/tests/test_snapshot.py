"""The daily snapshot: approved companies only, snapshot.v1 exactly, the
origin pin, and key rotation."""
import re
from datetime import date

from requests import ConnectionError as RequestsConnectionError

from odoo.tests import TransactionCase, new_test_user, tagged

from ..models import wire
from .common import ORIGIN, ConnectMixin, iso_in, params, recorded, verify, verify_signed_call

TOP_KEYS = {'schema', 'odoo_version', 'modules', 'companies'}
COMPANY_KEYS = {'odoo_company_id', 'customers', 'products', 'users', 'open_quotations',
                'open_invoices', 'monthly_sales'}
COUNT_KEYS = ('customers', 'products', 'users', 'open_quotations', 'open_invoices')


class CronTestMixin(ConnectMixin):
    """The cron runs its rotation and push in their own transactions; in tests
    those must share the test transaction, as HttpCase arranges."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.registry.enter_test_mode(cls.cr)
        cls.addClassCleanup(cls.registry.leave_test_mode)
        cls._setup_connect()
        cls.company_b = cls.env['res.company'].create({'name': 'Second Test Company'})
        cls.admin1.write({'company_ids': [(4, cls.company_b.id)]})

    def service(self):
        return self.env['askariane.connect']

    def run_cron(self):
        self.service()._cron_push_snapshot()
        self.env.invalidate_all()
        self.env.registry.clear_cache()


@tagged('post_install', '-at_install')
class TestSnapshot(CronTestMixin, TransactionCase):

    def _company(self, payload, company):
        rows = [row for row in payload['companies'] if row['odoo_company_id'] == company.id]
        return rows[0] if rows else None

    def _add_user(self, company, login):
        return new_test_user(self.env, login=login, groups='base.group_user',
                             company_id=company.id, company_ids=[(6, 0, company.ids)])

    def test_only_approved_company_and_counts_exclude_the_other(self):
        before = self.service()._build_snapshot([self.company_a.id])
        self._add_user(self.company_a, 'aac_user_a')
        self._add_user(self.company_b, 'aac_user_b')
        self._add_user(self.company_b, 'aac_user_b2')
        extra = {}
        if 'product.template' in self.env:
            self.env['product.template'].create({'name': 'Item A', 'company_id': self.company_a.id})
            self.env['product.template'].create({'name': 'Item B', 'company_id': self.company_b.id})
            self.env['product.template'].create({'name': 'Item B2', 'company_id': self.company_b.id})
            self.env['product.template'].create({'name': 'Shared item', 'company_id': False})
            extra['products'] = 1
        if 'customer_rank' in self.env['res.partner']._fields:
            for name, company in (('Cust A', self.company_a), ('Cust B', self.company_b),
                                  ('Cust B2', self.company_b), ('Shared cust', False)):
                self.env['res.partner'].create({'name': name, 'customer_rank': 1,
                                                'company_id': company.id if company else False})
            extra['customers'] = 1
        after = self.service()._build_snapshot([self.company_a.id])
        self.assertEqual([row['odoo_company_id'] for row in after['companies']], [self.company_a.id])
        a_before, a_after = self._company(before, self.company_a), self._company(after, self.company_a)
        self.assertEqual(a_after['users'], a_before['users'] + 1, 'user in A counted, users in B not')
        for key, delta in extra.items():
            self.assertEqual(a_after[key], a_before[key] + delta, f'{key}: only company A is counted')
        self.assertIsNone(self._company(after, self.company_b))
        # Positive control: approving B too makes B appear with its own counts.
        both = self.service()._build_snapshot([self.company_a.id, self.company_b.id])
        self.assertEqual([row['odoo_company_id'] for row in both['companies']],
                         sorted([self.company_a.id, self.company_b.id]))
        b_row = self._company(both, self.company_b)
        self.assertGreaterEqual(b_row['users'], 2)
        for key in extra:
            self.assertEqual(b_row[key], 2, f'{key}: company B has its own two')
        self.assertEqual(self._company(both, self.company_a), a_after)

    def test_payload_is_exactly_snapshot_v1_with_no_free_text(self):
        payload = self.service()._build_snapshot([self.company_a.id, self.company_b.id])
        self.assertEqual(set(payload), TOP_KEYS)
        self.assertEqual(payload['schema'], 'snapshot.v1')
        self.assertRegex(payload['odoo_version'], r'^\d{2}\.\d$')
        modules = payload['modules']
        self.assertLessEqual(len(modules), 400)
        self.assertEqual(modules, sorted(set(modules)))
        self.assertTrue(all(re.match(r'^[a-z0-9_]{1,64}$', name) for name in modules))
        self.assertIn('askariane_connect', modules)
        self.assertTrue(1 <= len(payload['companies']) <= 10)
        for row in payload['companies']:
            self.assertEqual(set(row), COMPANY_KEYS)
            self.assertIs(type(row['odoo_company_id']), int)
            for key in COUNT_KEYS:
                self.assertIs(type(row[key]), int, key)
                self.assertTrue(0 <= row[key] <= 10 ** 9)
            self.assertLessEqual(len(row['monthly_sales']), 12)
            for item in row['monthly_sales']:
                self.assertEqual(set(item), {'month', 'total'})
                self.assertRegex(item['month'], r'^\d{4}-\d{2}$')
                self.assertIn(type(item['total']), (int, float))
        # No company name or other text anywhere in the body.
        body = wire.canonical_body(payload).decode()
        self.assertNotIn(self.company_a.name, body)
        self.assertNotIn(self.company_b.name, body)
        strings = []

        def walk(value, key=None):
            if isinstance(value, dict):
                for k, v in value.items():
                    walk(v, k)
            elif isinstance(value, list):
                for v in value:
                    walk(v, key)
            elif isinstance(value, str):
                strings.append(key)
        walk(payload)
        self.assertTrue(set(strings) <= {'schema', 'odoo_version', 'modules', 'month'}, strings)

    def test_cron_pushes_one_signed_snapshot(self):
        private_key = self.store_connection([self.company_a.id])
        with recorded({'snapshot': [(200, {'accepted': True, 'snapshot_id': 's1'})]}) as rec:
            self.run_cron()
        self.assertEqual(rec.names(), ['snapshot'])
        body = verify_signed_call(self, rec.calls[0], wire.public_key_of(private_key), 'k_abc123', 1, ORIGIN)
        self.assertEqual(set(body), TOP_KEYS)
        self.assertEqual([row['odoo_company_id'] for row in body['companies']], [self.company_a.id])

    def test_origin_pin_stops_every_push(self):
        private_key = self.store_connection([self.company_a.id])
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://copy.example.net')
        with recorded({'snapshot': [(200, {'accepted': True})],
                       'rotate': [(200, {'generation': 2, 'expires_at': iso_in(90)})]}) as rec:
            self.run_cron()
            self.assertEqual(rec.calls, [], 'no HTTP call at all')
            self.assertEqual(params(self.env)['askariane_connect.private_key'], private_key, 'key kept')
            # Even with rotation due, nothing is sent.
            self.env['askariane.connect']._store_connection({'askariane_connect.expires_at': iso_in(2)})
            self.run_cron()
            self.assertEqual(rec.calls, [])
            # Positive control: the pinned address back, the push happens.
            self.env['askariane.connect']._store_connection({'askariane_connect.expires_at': iso_in(80)})
            self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://erp.example.com/')
            self.run_cron()
        self.assertEqual(rec.names(), ['snapshot'])

    def test_rotation_when_under_30_days(self):
        old_key = self.store_connection([self.company_a.id], expires_days=10)
        new_expiry = iso_in(90)
        with recorded({'rotate': [(200, {'generation': 2, 'expires_at': new_expiry})],
                       'snapshot': [(200, {'accepted': True})]}) as rec:
            self.run_cron()
        self.assertEqual(rec.names(), ['rotate', 'snapshot'])
        rotate = rec.calls[0]
        body = verify_signed_call(self, rotate, wire.public_key_of(old_key), 'k_abc123', 1, ORIGIN)
        self.assertEqual(set(body), {'new_public_key', 'proof'})
        nonce = rotate['headers']['X-Ariane-Nonce']
        verify(body['new_public_key'], body['proof'], f'askariane-v1-rotate\nk_abc123\n2\n{nonce}')
        stored = params(self.env)
        self.assertEqual(wire.public_key_of(stored['askariane_connect.private_key']), body['new_public_key'])
        self.assertEqual(stored['askariane_connect.generation'], '2')
        self.assertEqual(stored['askariane_connect.expires_at'], new_expiry)
        self.assertNotIn('askariane_connect.pending_private_key', stored)
        # The snapshot after it is signed with the new key and generation 2.
        verify_signed_call(self, rec.calls[1], body['new_public_key'], 'k_abc123', 2, ORIGIN)

    def test_no_rotation_with_time_left(self):
        self.store_connection([self.company_a.id], expires_days=31)
        with recorded({'snapshot': [(200, {'accepted': True})]}) as rec:
            self.run_cron()
        self.assertEqual(rec.names(), ['snapshot'])

    def test_lost_rotate_answer_recovers_with_pending_key(self):
        old_key = self.store_connection([self.company_a.id], expires_days=10)
        # The rotate answer is lost (network error): the pending key stays.
        with recorded({'rotate': [RequestsConnectionError('lost')], 'snapshot': [(200, {'accepted': True})]}) as rec:
            self.run_cron()
        stored = params(self.env)
        pending = stored['askariane_connect.pending_private_key']
        self.assertTrue(pending)
        self.assertEqual(stored['askariane_connect.private_key'], old_key)
        self.assertEqual(rec.names(), ['rotate', 'snapshot'])
        # Next run: the server had in fact rotated. Rotate with the old key
        # answers generation_mismatch; the snapshot retries with the pending
        # key and generation 2, and promotes it.
        with recorded({'rotate': [(401, {'error': 'generation_mismatch'})],
                       'snapshot': [(401, {'error': 'generation_mismatch'}),
                                    (200, {'accepted': True})]}) as rec:
            self.run_cron()
        self.assertEqual(rec.names(), ['rotate', 'snapshot', 'snapshot'])
        verify_signed_call(self, rec.calls[2], wire.public_key_of(pending), 'k_abc123', 2, ORIGIN)
        stored = params(self.env)
        self.assertEqual(stored['askariane_connect.private_key'], pending)
        self.assertEqual(stored['askariane_connect.generation'], '2')
        self.assertNotIn('askariane_connect.pending_private_key', stored)

    def test_account_counts_respect_company(self):
        if 'account.move' not in self.env or 'sale.order' not in self.env:
            self.skipTest('sale and account are not installed in this database')
        chart = self.env['account.chart.template']
        for company in (self.company_a, self.company_b):
            if not self.env['account.journal'].search_count([('company_id', '=', company.id),
                                                             ('type', '=', 'sale')]):
                chart.try_loading('generic_coa', company=company, install_demo=False)
        before = self.service()._build_snapshot([self.company_a.id])
        this_month = date.today().strftime('%Y-%m')

        def month_total(payload):
            row = self._company(payload, self.company_a)
            return sum(item['total'] for item in row['monthly_sales'] if item['month'] == this_month)

        for company, amount in ((self.company_a, 100.25), (self.company_b, 999.0)):
            env = self.env(context=dict(self.env.context, allowed_company_ids=[company.id]))
            partner = env['res.partner'].create({'name': f'Buyer {company.id}', 'company_id': company.id})
            env['sale.order'].with_company(company).create({'partner_id': partner.id, 'company_id': company.id})
            move = env['account.move'].with_company(company).create({
                'move_type': 'out_invoice', 'partner_id': partner.id, 'company_id': company.id,
                'invoice_date': date.today(),
                'invoice_line_ids': [(0, 0, {'name': 'Line', 'quantity': 1, 'price_unit': amount,
                                             'tax_ids': [(6, 0, [])]})],
            })
            move.action_post()
        after = self.service()._build_snapshot([self.company_a.id])
        a_before, a_after = self._company(before, self.company_a), self._company(after, self.company_a)
        self.assertEqual(a_after['open_quotations'], a_before['open_quotations'] + 1)
        self.assertEqual(a_after['open_invoices'], a_before['open_invoices'] + 1)
        self.assertAlmostEqual(month_total(after), month_total(before) + 100.25, places=2)
        # Positive control: company B's own numbers are there when B is approved.
        both = self.service()._build_snapshot([self.company_a.id, self.company_b.id])
        b_row = self._company(both, self.company_b)
        self.assertEqual(b_row['open_invoices'], 1)
        self.assertEqual(sum(item['total'] for item in b_row['monthly_sales']), 999.0)

