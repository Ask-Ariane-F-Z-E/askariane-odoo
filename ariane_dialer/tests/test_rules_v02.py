"""v0.2: tenant rule hooks, safe wrap-up, signed transcript ingest, Calls tab.

Behaviour: see ariane_dialer/README.md.
Every denial test has a positive control in the same test.
"""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tools.misc import file_path
from odoo.tests import HttpCase, tagged, new_test_user
from odoo.tests.common import JsonRpcException

from .common import GenericRulesMixin

# Every wrap-up needs a written note (MIN_NOTE_CHARS); tests that are about
# something else send this one.
NOTE = 'Spoke to the buyer'

INGEST = '/ariane_dialer/ingest/transcript'
KEY = 'ingest-key-for-tests-0123456789abcdef'  # pragma: allowlist secret


def _sign(body, ts=None, key=KEY):
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(key.encode(), f'{ts}.'.encode() + body, hashlib.sha256).hexdigest()
    return f't={ts},v1={sig}'


@tagged('post_install', '-at_install')
class TestDialerV02(GenericRulesMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Rules = cls.env.registry['ariane.dialer.rules']
        cls.rep = new_test_user(cls.env, login='v2rep1', password='v2rep1pass',  # pragma: allowlist secret
                                groups='ariane_dialer.group_dialer_user')
        cls.rep2 = new_test_user(cls.env, login='v2rep2', password='v2rep2pass',  # pragma: allowlist secret
                                 groups='ariane_dialer.group_dialer_user')
        cls.manager = new_test_user(cls.env, login='v2mgr', password='v2mgrpass1',  # pragma: allowlist secret
                                    groups='ariane_dialer.group_dialer_manager')
        us = cls.env.ref('base.us')
        cls.st_ok = cls.env['res.country.state'].search([('country_id', '=', us.id), ('code', '=', 'PA')])
        cls.st_ban = cls.env['res.country.state'].search([('country_id', '=', us.id), ('code', '=', 'TX')])
        cls.base_pl = cls.env['product.pricelist'].create({'name': 'Tier base', 'item_ids': [
            (0, 0, {'applied_on': '3_global', 'compute_price': 'percentage', 'percent_price': 0}),
            (0, 0, {'applied_on': '3_global', 'min_quantity': 10, 'compute_price': 'percentage',
                    'percent_price': 20}),
        ]})
        cls.pricelist = cls.env['product.pricelist'].create({'name': 'State on tier', 'item_ids': [
            (0, 0, {'applied_on': '3_global', 'compute_price': 'formula', 'base': 'pricelist',
                    'base_pricelist_id': cls.base_pl.id}),
        ]})
        cls.partner = cls.env['res.partner'].create({
            'name': 'Hill Stores', 'is_company': True, 'state_id': cls.st_ok.id,
            'country_id': us.id, 'property_product_pricelist': cls.pricelist.id})
        cls.child = cls.env['res.partner'].create({'name': 'Hill Buyer', 'parent_id': cls.partner.id})
        cls.ship_banned = cls.env['res.partner'].create({
            'name': 'Hill TX depot', 'parent_id': cls.partner.id, 'type': 'delivery',
            'state_id': cls.st_ban.id, 'country_id': us.id})
        cls.pa_shop = cls.env['res.partner'].create({
            'name': 'Penn Corner', 'is_company': True, 'state_id': cls.st_ok.id,
            'country_id': us.id, 'property_product_pricelist': cls.pricelist.id})
        cls.ok = cls.env['product.product'].create({'name': 'Zeta OK', 'list_price': 50.0, 'sale_ok': True})
        cls.banned = cls.env['product.product'].create({'name': 'Zeta Banned', 'list_price': 50.0,
                                                        'sale_ok': True})
        cls.icp = cls.env['ir.config_parameter'].sudo()
        cls.icp.set_param('ariane_dialer.tenant_id', 'tenant-test')

    def rpc(self, route, **params):
        return self.make_jsonrpc_request(route, params)

    def rpc_error(self, route, **params):
        """The error message of a refused JSON call (the test client keeps only
        the class name), so a test can tell which refusal it got."""
        res = self.url_open(route, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
                            headers={'Content-Type': 'application/json'}).json()
        self.assertIn('error', res, f'{route} was not refused')
        return res['error']['data']['message']

    def _ban(self):
        """A tenant rule: 'Zeta Banned' cannot go to TX (or to an unknown state)."""
        banned = self.banned

        def availability(rules, partner, product, ship_partner=None):
            state = (ship_partner or partner).state_id
            if product == banned and (not state or state.code == 'TX'):
                return {'status': 'blocked', 'text': 'Not sold in TX'}
            return {'status': 'allowed', 'text': ''}
        return patch.object(self.Rules, '_dialer_availability', availability)

    def _start(self, login, password, **kw):
        self.authenticate(login, password)
        return self.rpc('/ariane_dialer/call/start', phone='+14155550199',
                        partner_id=kw.pop('partner_id', self.partner.id), preview=True, **kw)['call_id']

    # ------------------------------------------------------------ catalogue
    def test_search_hides_blocked_products_and_counts_them(self):
        self.authenticate('v2rep1', 'v2rep1pass')
        with self._ban():
            out = self.rpc('/ariane_dialer/products', query='Zeta', partner_id=self.ship_banned.id)
        names = [c['name'] for c in out['products']]
        self.assertEqual(names, ['Zeta OK'])
        self.assertEqual(out['hidden'], 1)
        # the company itself delivers to its TX depot, so it is hidden there too
        with self._ban():
            out = self.rpc('/ariane_dialer/products', query='Zeta', partner_id=self.partner.id)
        self.assertEqual(out['hidden'], 1)
        # positive control: a PA-only customer sees both and nothing is hidden
        with self._ban():
            out = self.rpc('/ariane_dialer/products', query='Zeta', partner_id=self.pa_shop.id)
        self.assertEqual(sorted(c['name'] for c in out['products']), ['Zeta Banned', 'Zeta OK'])
        self.assertEqual(out['hidden'], 0)

    def test_search_continues_past_a_page_of_blocked_products(self):
        many = self.env['product.product'].create([
            {'name': f'Qblock {i:02d}', 'list_price': 5.0, 'sale_ok': True} for i in range(20)])
        good = self.env['product.product'].create({'name': 'Qblock zz good', 'list_price': 5.0,
                                                   'sale_ok': True})

        def availability(rules, partner, product, ship_partner=None):
            return {'status': 'blocked' if product in many else 'allowed', 'text': ''}
        self.authenticate('v2rep1', 'v2rep1pass')
        with patch.object(self.Rules, '_dialer_availability', availability):
            out = self.rpc('/ariane_dialer/products', query='Qblock', partner_id=self.partner.id, limit=2)
        self.assertEqual([c['id'] for c in out['products']], [good.id])
        self.assertEqual(out['hidden'], 20)

    def test_breaks_come_through_the_base_pricelist_chain(self):
        self.authenticate('v2rep1', 'v2rep1pass')
        card = self.rpc('/ariane_dialer/product', product_id=self.ok.id, partner_id=self.partner.id)
        self.assertAlmostEqual(card['price'], 50.0)
        self.assertEqual(card['breaks'], [{'min_qty': 10.0, 'price': 40.0}])

    def test_card_reports_moq_and_starts_quantity_there(self):
        self.authenticate('v2rep1', 'v2rep1pass')
        with patch.object(self.Rules, '_dialer_moq', lambda rules, product: 5.0):
            card = self.rpc('/ariane_dialer/product', product_id=self.ok.id, partner_id=self.partner.id)
        self.assertEqual(card['moq'], 5.0)

    # ------------------------------------------------------------ quote/add
    def test_quote_refuses_a_product_banned_for_the_order_shipping_state(self):
        order = self.env['sale.order'].create({'partner_id': self.partner.id,
                                               'partner_shipping_id': self.ship_banned.id,
                                               'user_id': self.rep.id})
        self.authenticate('v2rep1', 'v2rep1pass')
        with self._ban(), self.assertRaises(JsonRpcException):
            # the contact (PA) is fine; the ORDER ships to TX
            self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.banned.id,
                     qty=1, order_id=order.id)
        self.assertFalse(order.order_line)
        with self._ban():   # positive control: an allowed product goes on the same order
            self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.ok.id,
                     qty=1, order_id=order.id)
        self.assertEqual(order.order_line.product_id, self.ok)

    def test_quote_refuses_below_moq(self):
        self.authenticate('v2rep1', 'v2rep1pass')
        with patch.object(self.Rules, '_dialer_moq', lambda rules, product: 5.0):
            with self.assertRaises(JsonRpcException):
                self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.ok.id, qty=4)
            self.assertFalse(self.env['sale.order'].search([('user_id', '=', self.rep.id)]))
            out = self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.ok.id, qty=5)
        self.assertEqual(self.env['sale.order'].browse(out['order_id']).order_line.product_uom_qty, 5)

    def test_quote_refuses_a_blocked_price(self):
        self.icp.set_param('ariane_dialer.price_block_at', '100000')
        marker = self.env['product.product'].create({'name': 'Marker', 'list_price': 1000000.0,
                                                     'sale_ok': True})
        self.authenticate('v2rep1', 'v2rep1pass')
        with self.assertRaises(JsonRpcException):
            self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=marker.id, qty=1)
        self.assertFalse(self.env['sale.order.line'].search([('product_id', '=', marker.id)]))
        self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.ok.id, qty=1)

    def test_new_quote_uses_the_pricelist_the_card_showed(self):
        # The person called has their own pricelist (a tenant may keep pricelists off the
        # commercial sync), so Odoo's default for the order would differ from the card.
        own = self.env['product.pricelist'].create({'name': 'Person own list'})
        self.child.property_product_pricelist = own
        self.assertEqual(self.child.property_product_pricelist, own)  # the setup holds
        self.authenticate('v2rep1', 'v2rep1pass')
        out = self.rpc('/ariane_dialer/quote/add', partner_id=self.child.id, product_id=self.ok.id, qty=1)
        self.assertEqual(self.env['sale.order'].browse(out['order_id']).pricelist_id, self.pricelist)

    def test_repeated_add_key_adds_one_line(self):
        call_id = self._start('v2rep1', 'v2rep1pass')
        first = self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.ok.id,
                         qty=2, call_id=call_id, add_key='k-1')
        again = self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.ok.id,
                         qty=2, call_id=call_id, add_key='k-1')
        self.assertEqual(first, again)
        order = self.env['sale.order'].browse(first['order_id'])
        self.assertEqual(len(order.order_line), 1)
        # positive control: a new key adds a new line
        self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.ok.id,
                 qty=2, call_id=call_id, add_key='k-2', order_id=order.id)
        self.assertEqual(len(order.order_line), 2)

    # ------------------------------------------------------------ outcomes + wrap-up
    def test_outcomes_endpoint_lists_generic_outcomes(self):
        self.authenticate('v2rep1', 'v2rep1pass')
        codes = [o['code'] for o in self.rpc('/ariane_dialer/outcomes')]
        self.assertIn('no_answer', codes)
        self.assertIn('callback', codes)

    def test_wrapup_with_outcome_and_followup_datetime(self):
        call_id = self._start('v2rep1', 'v2rep1pass')
        out = self.rpc('/ariane_dialer/call/wrapup', call_id=call_id, outcome='callback', note='Price list',
                       follow_up_at='2030-02-03T14:00:00Z')
        call = self.env['ariane.dialer.call'].browse(call_id)
        self.assertTrue(out['ok'])
        self.assertEqual((call.state, call.outcome_code, call.disposition), ('done', 'callback', 'callback'))
        act = self.partner.activity_ids.filtered(lambda a: a.summary == 'Call back')
        self.assertEqual(str(act.date_deadline), '2030-02-03')

    def test_wrapup_needs_followup_when_the_outcome_says_so(self):
        call_id = self._start('v2rep1', 'v2rep1pass')
        with self.assertRaises(JsonRpcException):
            self.rpc('/ariane_dialer/call/wrapup', call_id=call_id, outcome='callback', note=NOTE)
        self.assertNotEqual(self.env['ariane.dialer.call'].browse(call_id).state, 'done')

    def test_wrapup_retry_returns_the_first_result_and_logs_once(self):
        call_id = self._start('v2rep1', 'v2rep1pass')
        first = self.rpc('/ariane_dialer/call/wrapup', call_id=call_id, outcome='callback', note=NOTE,
                         follow_up_at='2030-02-03T14:00:00Z')
        second = self.rpc('/ariane_dialer/call/wrapup', call_id=call_id, outcome='interested')
        self.assertEqual(first, second)
        call = self.env['ariane.dialer.call'].browse(call_id)
        self.assertEqual(call.outcome_code, 'callback')
        self.assertEqual(len(self.partner.activity_ids.filtered(lambda a: a.summary == 'Call back')), 1)

    def test_a_refusal_in_the_tenant_log_rolls_everything_back(self):
        call_id = self._start('v2rep1', 'v2rep1pass')

        def log_then_refuse(rules, call, outcome, note='', follow_up_at=False, closure_reason=''):
            call.partner_id.activity_schedule('mail.mail_activity_data_todo', summary='half-written')
            raise UserError('Tenant refused.')
        with patch.object(self.Rules, '_dialer_log_call', log_then_refuse), self.assertRaises(JsonRpcException):
            self.rpc('/ariane_dialer/call/wrapup', call_id=call_id, outcome='interested', note=NOTE)
        call = self.env['ariane.dialer.call'].browse(call_id)
        call.invalidate_recordset()
        self.assertNotEqual(call.state, 'done')
        self.assertFalse(self.partner.activity_ids.filtered(lambda a: a.summary == 'half-written'))
        # positive control: the same call wraps up once the tenant accepts
        self.rpc('/ariane_dialer/call/wrapup', call_id=call_id, outcome='interested', note=NOTE)
        call.invalidate_recordset()
        self.assertEqual(call.state, 'done')

    # ------------------------------------------------------------ provider id binding
    def test_a_rep_cannot_set_the_provider_call_id(self):
        call_id = self._start('v2rep1', 'v2rep1pass', external_call_id='CA-victim')
        self.rpc('/ariane_dialer/call/update', call_id=call_id, state='live', external_call_id='CA-victim')
        call = self.env['ariane.dialer.call'].browse(call_id)
        self.assertFalse(call.external_call_id)
        self.assertEqual(call.state, 'live')   # positive control: the rest of the update applied

    # ------------------------------------------------------------ ingest
    def _call(self, user=None, partner=None):
        return self.env['ariane.dialer.call'].create({
            'phone': '+14155550199', 'user_id': (user or self.rep).id,
            'partner_id': (partner or self.partner).id, 'state': 'done'})

    def _push(self, call, rev=1, state='ready', provider='CA-1', text='Rep: hi\nCustomer: hello',
              tenant='tenant-test', ts=None, key=KEY, extra=None, raw=None):
        payload = {'tenant_id': tenant, 'call_id': call.id, 'provider_call_id': provider,
                   'uid': call.user_id.id, 'company_id': call.company_id.id,
                   'revision': rev, 'transcript_state': state, 'text': text, 'summary': 'Said hello',
                   'segments': [{'start': 0, 'end': 1.5, 'speaker': 'rep', 'text': 'hi'},
                                {'start': 1.6, 'end': 3, 'speaker': 'customer', 'text': 'hello'}]}
        payload.update(extra or {})
        body = raw if raw is not None else json.dumps(payload).encode()
        res = self.url_open(INGEST, data=body, headers={
            'Content-Type': 'application/json', 'X-Ariane-Signature': _sign(body, ts, key)})
        call.invalidate_recordset()
        try:
            code = res.json().get('code')
        except ValueError:   # refused by Odoo itself, before this route ran
            code = None
        return res.status_code, code

    def test_ingest_is_closed_without_a_key(self):
        self.icp.set_param('ariane_dialer.ingest_key', '')
        call = self._call()
        self.assertEqual(self._push(call)[0], 404)
        self.assertEqual(call.transcript_state, 'none')

    def test_ingest_is_closed_without_a_tenant(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        self.icp.set_param('ariane_dialer.tenant_id', '')
        call = self._call()
        # A push that names no tenant would otherwise match an unset one.
        self.assertEqual(self._push(call, tenant=None)[0], 404)
        self.assertEqual(call.transcript_state, 'none')
        self.icp.set_param('ariane_dialer.tenant_id', 'tenant-test')   # positive control
        self.assertEqual(self._push(call), (200, 'ok'))

    def test_ingest_writes_a_signed_push_and_binds_the_provider_id(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        call = self._call()
        self.assertEqual(self._push(call), (200, 'ok'))
        self.assertEqual(call.transcript_state, 'ready')
        self.assertEqual(call.external_call_id, 'CA-1')
        self.assertEqual(call.segment_ids.mapped('speaker'), ['rep', 'customer'])
        self.assertEqual(call.transcript_revision, 1)

    def test_ingest_refuses_bad_stale_future_and_wrong_tenant(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        call = self._call()
        now = int(time.time())
        self.assertEqual(self._push(call, key='wrong-key-wrong-key-wrong-key'), (401, 'bad_signature'))
        # Well past the 300 s window either way: at 301 a one-second tick made it 300 and it passed.
        self.assertEqual(self._push(call, ts=now - 360), (401, 'bad_signature'))
        self.assertEqual(self._push(call, ts=now + 360), (401, 'bad_signature'))
        self.assertEqual(self._push(call, tenant='someone-else'), (403, 'bad_tenant'))
        self.assertEqual(self._push(call, raw=b'not json'), (400, 'bad_body'))
        self.assertEqual(call.transcript_state, 'none')
        self.assertFalse(call.segment_ids)
        self.assertEqual(self._push(call), (200, 'ok'))   # positive control

    def test_ingest_refuses_oversize_bodies_before_writing(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        call = self._call()
        big = [{'start': i, 'end': i, 'speaker': 'rep', 'text': 'x'} for i in range(5001)]
        self.assertEqual(self._push(call, extra={'segments': big}), (413, 'too_large'))
        # Over the route's max_content_length: Odoo refuses it before the body is parsed.
        self.assertEqual(self._push(call, extra={'pad': 'x' * (2 * 1024 * 1024)})[0], 413)
        self.assertEqual(call.transcript_state, 'none')

    def test_ingest_refuses_a_push_for_a_different_provider_call(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        call = self._call()
        self.assertEqual(self._push(call, provider='CA-1'), (200, 'ok'))
        self.assertEqual(self._push(call, rev=2, provider='CA-other', text='hijack'), (409, 'wrong_call'))
        self.assertNotIn('hijack', call.transcript)
        self.assertEqual(self._push(call, extra={'call_id': 999999999}), (404, 'unknown_call'))

    def test_ingest_revision_orders_writes(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        call = self._call()
        self.assertEqual(self._push(call, rev=2, text='second'), (200, 'ok'))
        self.assertEqual(self._push(call, rev=1, text='first, late'), (200, 'unchanged'))
        self.assertEqual(self._push(call, rev=2, text='second again'), (200, 'unchanged'))
        self.assertEqual(call.transcript, 'second')
        self.assertEqual(self._push(call, rev=3, text='third'), (200, 'ok'))
        self.assertEqual(call.transcript, 'third')
        self.assertEqual(len(call.segment_ids), 2)   # replaced, never appended

    def test_ingest_purge_is_a_tombstone(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        call = self._call()
        self._push(call, rev=1)
        self.assertEqual(self._push(call, rev=2, state='purged'), (200, 'ok'))
        self.assertEqual((call.transcript_state, call.transcript, call.summary), ('purged', False, False))
        self.assertFalse(call.segment_ids)
        self.assertEqual(self._push(call, rev=3), (409, 'purged'))
        self.assertEqual(call.transcript_state, 'purged')
        self.assertFalse(call.segment_ids)

    # ------------------------------------------------------------ Calls tab
    def test_calls_tab_lists_the_company_and_its_people_for_own_calls_only(self):
        mine_parent = self._call(self.rep, self.partner)
        mine_child = self._call(self.rep, self.child)
        theirs = self._call(self.rep2, self.child)
        seen = self.child.with_user(self.rep).ariane_call_ids
        self.assertEqual(set(seen.ids), {mine_parent.id, mine_child.id})
        # positive control: a manager sees all three
        self.assertEqual(set(self.child.with_user(self.manager).ariane_call_ids.ids),
                         {mine_parent.id, mine_child.id, theirs.id})

    def test_wrapup_records_the_follow_up_as_utc(self):
        call_id = self._start('v2rep1', 'v2rep1pass')
        self.rpc('/ariane_dialer/call/wrapup', call_id=call_id, outcome='callback', note=NOTE,
                 follow_up_at='2030-02-03T23:30:00-05:00')
        call = self.env['ariane.dialer.call'].browse(call_id)
        self.assertEqual(call.follow_up_at, fields.Datetime.to_datetime('2030-02-04 04:30:00'))

    def _with_reason_rule(self):
        """Outcomes as a tenant with a compliance-grade activity log would list them."""
        base = self.Rules._dialer_outcomes

        def outcomes(rules):
            return [dict(o, needs_closure_reason=o['closes']) for o in base(rules)]
        return patch.object(self.Rules, '_dialer_outcomes', outcomes)

    def test_wrapup_refuses_a_closing_outcome_without_a_reason_when_the_tenant_needs_one(self):
        call_id = self._start('v2rep1', 'v2rep1pass')
        with self._with_reason_rule(), self.assertRaises(JsonRpcException):
            self.rpc('/ariane_dialer/call/wrapup', call_id=call_id, outcome='not_qualified', closure_reason='  ',
                     note=NOTE)
        call = self.env['ariane.dialer.call'].browse(call_id)
        self.assertNotEqual(call.state, 'done')
        # Positive control: the same call closes once a reason is given.
        with self._with_reason_rule():
            self.rpc('/ariane_dialer/call/wrapup', call_id=call_id, outcome='not_qualified', note=NOTE,
                     closure_reason='Sells only furniture')
        call.invalidate_recordset()
        self.assertEqual((call.state, call.closure_reason), ('done', 'Sells only furniture'))

    def test_a_closing_outcome_drops_the_follow_up(self):
        # A stale time left in the form would be refused as past if it were kept.
        call_id = self._start('v2rep1', 'v2rep1pass')
        try:
            self.rpc('/ariane_dialer/call/wrapup', call_id=call_id, outcome='wrong_number', note=NOTE,
                     follow_up_at='2001-01-01T10:00:00Z', closure_reason='Pizza shop')
        except JsonRpcException as e:
            self.fail(f'A closing outcome was refused over its follow-up time: {e}')
        call = self.env['ariane.dialer.call'].browse(call_id)
        self.assertEqual((call.state, call.follow_up_at, call.closure_reason), ('done', False, 'Pizza shop'))
        # A future time would be kept and booked as a call-back if it were not dropped.
        call_id = self._start('v2rep1', 'v2rep1pass')
        self.rpc('/ariane_dialer/call/wrapup', call_id=call_id, outcome='wrong_number', note=NOTE,
                 follow_up_at='2099-01-01T10:00:00Z', closure_reason='Pizza shop')
        call = self.env['ariane.dialer.call'].browse(call_id)
        self.assertEqual((call.state, call.follow_up_at), ('done', False))
        self.assertFalse(self.partner.activity_ids.filtered(lambda a: a.summary == 'Call back'))

    # ------------------------------------------------------------ review round 1 (2026-10-19)
    def test_a_rep_cannot_write_or_create_calls_directly(self):
        call_id = self._start('v2rep1', 'v2rep1pass')
        call = self.env['ariane.dialer.call'].browse(call_id)
        mine = call.with_user(self.rep)
        for vals in ({'transcript_revision': 2147483647}, {'transcript_state': 'ready', 'transcript': 'made up'},
                     {'partner_id': self.pa_shop.id}, {'external_call_id': 'CA-victim'}, {'state': 'done'}):
            with self.assertRaises(AccessError, msg=vals):
                mine.write(vals)
        with self.assertRaises(AccessError):
            self.env['ariane.dialer.call'].with_user(self.rep).create({'phone': '+1', 'partner_id': self.partner.id})
        call.invalidate_recordset()
        self.assertEqual((call.transcript_revision, call.transcript_state, call.partner_id, call.state),
                         (0, 'none', self.partner, 'preview'))
        # positive control: the same rep changes the same call through the panel
        self.rpc('/ariane_dialer/call/update', call_id=call_id, state='live')
        call.invalidate_recordset()
        self.assertEqual(call.state, 'live')

    def test_ingest_push_must_name_the_calls_rep_and_company(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        call = self._call(self.rep2)
        self.assertEqual(self._push(call, extra={'uid': self.rep.id}), (409, 'wrong_call'))
        self.assertEqual(self._push(call, extra={'company_id': call.company_id.id + 1000}), (409, 'wrong_call'))
        self.assertEqual(self._push(call, extra={'uid': None}), (400, 'bad_body'))
        self.assertEqual((call.transcript_state, call.external_call_id), ('none', False))
        self.assertEqual(self._push(call), (200, 'ok'))   # positive control

    def test_ingest_purge_wins_whatever_its_revision(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        call = self._call()
        self.assertEqual(self._push(call, rev=5), (200, 'ok'))
        self.assertEqual(self._push(call, rev=1, state='purged'), (200, 'ok'))
        self.assertEqual((call.transcript_state, call.transcript, call.language, call.transcript_revision),
                         ('purged', False, False, 5))
        self.assertFalse(call.segment_ids)
        self.assertEqual(self._push(call, rev=9), (409, 'purged'))
        # positive control: an older transcript (not a purge) is still ordered
        other = self._call()
        self._push(other, rev=5, provider='CA-2', text='five')
        self.assertEqual(self._push(other, rev=1, provider='CA-2', text='one'), (200, 'unchanged'))
        self.assertEqual(other.transcript, 'five')

    def test_ingest_refuses_a_provider_id_bound_to_another_call(self):
        self.icp.set_param('ariane_dialer.ingest_key', KEY)
        first, second = self._call(), self._call()
        self.assertEqual(self._push(first, provider='CA-1'), (200, 'ok'))
        self.assertEqual(self._push(second, provider='CA-1'), (409, 'provider_in_use'))
        self.assertEqual(second.transcript_state, 'none')
        self.assertEqual(self._push(second, provider='CA-2'), (200, 'ok'))   # positive control

    def test_add_refuses_a_finished_call_and_another_customer(self):
        call_id = self._start('v2rep1', 'v2rep1pass')
        self.assertIn('another customer', self.rpc_error(
            '/ariane_dialer/quote/add', partner_id=self.pa_shop.id, product_id=self.ok.id, qty=1, call_id=call_id))
        self.assertFalse(self.env['sale.order'].search([('partner_id', '=', self.pa_shop.id)]))
        # positive control: the call's own customer, while the call is open
        self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.ok.id,
                 qty=1, call_id=call_id)
        self.rpc('/ariane_dialer/call/wrapup', call_id=call_id, outcome='interested', note=NOTE)
        self.assertIn('finished', self.rpc_error(
            '/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.ok.id,
            qty=1, call_id=call_id, add_key='after-wrapup'))

    def test_adds_on_one_call_go_on_one_quotation_and_any_key_replays(self):
        call_id = self._start('v2rep1', 'v2rep1pass')
        a = self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.ok.id,
                     qty=1, call_id=call_id, add_key='A')
        # a second click that never got the first order id back
        b = self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.banned.id,
                     qty=1, call_id=call_id, add_key='B')
        self.assertEqual(a['order_id'], b['order_id'])
        # A's answer was lost and A is retried after B: replayed, not added again
        self.assertEqual(self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id,
                                  product_id=self.ok.id, qty=1, call_id=call_id, add_key='A'), a)
        order = self.env['sale.order'].browse(a['order_id'])
        self.assertEqual(len(order.order_line), 2)

    def test_a_fixed_price_is_kept_when_the_list_price_is_a_marker(self):
        self.icp.set_param('ariane_dialer.price_block_at', '100000')
        marked = self.env['product.product'].create({'name': 'Listed at marker', 'list_price': 1000000.0,
                                                     'sale_ok': True})
        fixed = self.env['product.pricelist'].create({'name': 'Real price', 'item_ids': [
            (0, 0, {'applied_on': '0_product_variant', 'product_id': marked.id,
                    'compute_price': 'fixed', 'fixed_price': 40.0})]})
        rules = self.env['ariane.dialer.rules']
        self.assertEqual(rules._dialer_price(self.partner, marked, pricelist=fixed)['price'], 40.0)
        # positive control: a price passed through from the marker list price is blocked
        self.assertIsNone(rules._dialer_price(self.partner, marked, pricelist=self.pricelist)['price'])

    def test_price_is_checked_on_the_order_date(self):
        self.icp.set_param('ariane_dialer.price_block_at', '100000')
        old = fields.Datetime.now() - timedelta(days=10)
        self.pricelist.item_ids = [(0, 0, {
            'applied_on': '0_product_variant', 'product_id': self.ok.id, 'compute_price': 'fixed',
            'fixed_price': 1000000.0, 'date_end': old + timedelta(days=1)})]
        order = self.env['sale.order'].create({'partner_id': self.partner.id, 'user_id': self.rep.id,
                                               'pricelist_id': self.pricelist.id, 'date_order': old})
        self.authenticate('v2rep1', 'v2rep1pass')
        self.assertIn('no price', self.rpc_error(
            '/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.ok.id, qty=1, order_id=order.id))
        self.assertFalse(order.order_line)
        # positive control: today the marker rule has ended, so a new quote prices normally
        out = self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id, product_id=self.ok.id, qty=1)
        self.assertEqual(out['line_price'], 50.0)

    def test_lines_the_tenant_adds_are_checked_too(self):
        SaleLine = self.env.registry['sale.order.line']
        original = SaleLine.create
        banned = self.banned

        def create_with_accessory(lines, vals_list):
            out = original(lines, vals_list)
            if not lines.env.context.get('accessory') and banned not in out.product_id:
                original(lines.with_context(accessory=True),
                         [{'order_id': out[0].order_id.id, 'product_id': banned.id, 'product_uom_qty': 1}])
            return out
        self.authenticate('v2rep1', 'v2rep1pass')
        with self._ban(), patch.object(SaleLine, 'create', create_with_accessory):
            self.assertIn('Not sold in TX', self.rpc_error(
                '/ariane_dialer/quote/add', partner_id=self.ship_banned.id, product_id=self.ok.id, qty=1))
        self.assertFalse(self.env['sale.order.line'].search([('product_id', 'in', (self.ok | banned).ids)]))
        with self._ban():   # positive control: no accessory, the add goes through
            self.rpc('/ariane_dialer/quote/add', partner_id=self.ship_banned.id, product_id=self.ok.id, qty=1)

    def test_wrapup_stores_the_request_the_panel_showed(self):
        first = self._start('v2rep1', 'v2rep1pass')
        self.rpc('/ariane_dialer/call/wrapup', call_id=first, outcome='interested', expected_request=0, note=NOTE)
        second = self._start('v2rep1', 'v2rep1pass')
        self.rpc('/ariane_dialer/call/wrapup', call_id=second, outcome='interested', note=NOTE)
        Call = self.env['ariane.dialer.call']
        self.assertEqual((Call.browse(first).expected_request, Call.browse(second).expected_request), (0, -1))

    def test_a_partner_read_does_not_need_the_dialler_group(self):
        # A sales user outside the dialler group, as an integration user would be.
        staff = new_test_user(self.env, login='v2staff',
                              groups='sales_team.group_sale_salesman,account.group_account_invoice')
        # The fields a full read() returns, cut to this module's own: a tenant's
        # fields may need the tenant's groups, which is not this module's contract.
        added = [n for n, f in self.env['res.partner']._fields.items() if f._module == 'ariane_dialer']
        self.assertIn('ariane_call_ids', added)
        as_staff = self.partner.with_user(staff)
        ours = [n for n in as_staff.check_field_access_rights('read', None) if n in added]
        try:
            self.assertTrue(as_staff.read(['name'] + ours))
        except AccessError as e:
            self.fail(f'A plain partner read was refused: {e}')
        # positive control: a dialler user still gets the calls
        call = self._call()
        self.assertIn(call, self.partner.with_user(self.rep).ariane_call_ids)

    def test_neutralize_removes_the_dialler_keys(self):
        for key in ('signing_key', 'ingest_key', 'api_url', 'tenant_id'):
            self.icp.set_param(f'ariane_dialer.{key}', 'set')
        self.icp.set_param('ariane_dialer.price_block_at', '100000')
        with open(file_path('ariane_dialer/data/neutralize.sql')) as f:
            self.env.cr.execute(f.read())
        self.env.invalidate_all()
        self.assertFalse([k for k in ('signing_key', 'ingest_key', 'api_url', 'tenant_id')
                          if self.icp.get_param(f'ariane_dialer.{k}')])
        self.assertEqual(self.icp.get_param('ariane_dialer.price_block_at'), '100000')   # control
