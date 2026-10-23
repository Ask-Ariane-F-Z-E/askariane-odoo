import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta

from unittest.mock import patch

from odoo import fields
from odoo.tests import HttpCase, tagged, new_test_user
from odoo.tests.common import JsonRpcException

from ..controllers.main import ArianeDialerController
from .common import GenericRulesMixin, new_company, queries_on

# Every wrap-up needs a written note (MIN_NOTE_CHARS); tests that are about
# something else send this one.
NOTE = 'Spoke to the buyer'


def _b64d(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


@tagged('post_install', '-at_install')
class TestDialer(GenericRulesMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company2 = new_company(cls.env, 'Other Co')
        cls.rep = new_test_user(cls.env, login='rep1', password='rep1pass1',  # pragma: allowlist secret
                                groups='ariane_dialer.group_dialer_user')
        cls.rep2 = new_test_user(cls.env, login='rep2', password='rep2pass2',  # pragma: allowlist secret
                                 groups='ariane_dialer.group_dialer_user')
        cls.outsider = new_test_user(cls.env, login='plain', password='plainpass1',  # pragma: allowlist secret
                                     groups='sales_team.group_sale_salesman')
        cls.pricelist = cls.env['product.pricelist'].create({'name': 'Trade', 'item_ids': [
            (0, 0, {'applied_on': '3_global', 'compute_price': 'percentage', 'percent_price': 10}),
        ]})
        cls.partner = cls.env['res.partner'].create({
            'name': 'Corner Shop', 'phone': '+1 415 555 0101',
            'property_product_pricelist': cls.pricelist.id})
        # shared by both companies, as on a tenant that files new contacts in its own
        cls.partner.company_id = False
        cls.product = cls.env['product.product'].create({
            'name': 'Widget Pro', 'default_code': 'WP-1', 'list_price': 100.0, 'sale_ok': True})
        cls.sentinel = cls.env['product.product'].create({
            'name': 'Widget Blocked', 'list_price': 1000000.0, 'sale_ok': True})
        cls.other_order = cls.env['sale.order'].with_company(cls.company2).create({
            'partner_id': cls.partner.id, 'company_id': cls.company2.id})

    def rpc(self, route, **params):
        return self.make_jsonrpc_request(route, params)

    # ------------------------------------------------------------ session
    def test_session_not_configured_returns_flag(self):
        # a real database (a staging copy) may already hold these: clear them,
        # one at a time, so each missing setting is shown to switch it off
        icp = self.env['ir.config_parameter'].sudo()
        full = {'ariane_dialer.api_url': 'https://dialer.example/',
                'ariane_dialer.tenant_id': 'tenant-x', 'ariane_dialer.signing_key': 'k' * 32}
        self.authenticate('rep1', 'rep1pass1')
        for missing in full:
            for key, value in full.items():
                icp.set_param(key, '' if key == missing else value)
            self.assertEqual(self.rpc('/ariane_dialer/session'), {'configured': False}, missing)
        for key, value in full.items():
            icp.set_param(key, value)
        # positive control: with all three set, the same call is configured
        self.assertTrue(self.rpc('/ariane_dialer/session')['configured'])

    def test_session_assertion_is_signed_and_names_the_server_user(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('ariane_dialer.api_url', 'https://dialer.example/')
        icp.set_param('ariane_dialer.tenant_id', 'tenant-x')
        icp.set_param('ariane_dialer.signing_key', 'k' * 32)
        self.authenticate('rep1', 'rep1pass1')
        out = self.rpc('/ariane_dialer/session')
        self.assertTrue(out['configured'])
        self.assertEqual(out['api_url'], 'https://dialer.example')
        self.assertNotIn('signing_key', json.dumps(out))
        header, body, sig = out['assertion'].split('.')
        expect = hmac.new(b'k' * 32, f'{header}.{body}'.encode(), hashlib.sha256).digest()
        self.assertEqual(_b64d(sig), expect)
        claims = json.loads(_b64d(body))
        self.assertEqual(claims['uid'], self.rep.id)
        self.assertEqual(claims['aud'], 'ariane-dialer-api')
        self.assertEqual(claims['tenant_id'], 'tenant-x')
        self.assertLessEqual(claims['exp'] - int(time.time()), 300)
        # a second pass is a different one-time id
        again = json.loads(_b64d(self.rpc('/ariane_dialer/session')['assertion'].split('.')[1]))
        self.assertNotEqual(again['jti'], claims['jti'])

    def test_user_without_dialer_group_is_refused(self):
        self.authenticate('plain', 'plainpass1')
        with self.assertRaises(JsonRpcException):
            self.rpc('/ariane_dialer/products', query='Widget', partner_id=self.partner.id)
        # positive control: a dialler rep gets results from the same call
        self.authenticate('rep1', 'rep1pass1')
        self.assertTrue(self.rpc('/ariane_dialer/products', query='Widget', partner_id=self.partner.id)['products'])

    # ------------------------------------------------------------ catalogue
    def test_product_card_uses_customer_pricelist_and_hides_sentinel(self):
        self.env['ir.config_parameter'].sudo().set_param('ariane_dialer.price_block_values', '1000000, 1')
        self.authenticate('rep1', 'rep1pass1')
        cards = {c['name']: c for c in self.rpc('/ariane_dialer/products', query='Widget',
                                                 partner_id=self.partner.id)['products']}
        self.assertAlmostEqual(cards['[WP-1] Widget Pro']['price'], 90.0)
        # the sentinel sits on the product; the 10% pricelist must not turn it into 900,000
        self.assertIsNone(cards['Widget Blocked']['price'])
        self.assertEqual(cards['Widget Blocked']['price_note'], 'Check price before quoting')
        self.assertNotIn('900000', json.dumps(cards['Widget Blocked']))
        self.assertEqual(cards['[WP-1] Widget Pro']['compliance']['status'], 'unknown')

    def test_lookup_matches_partner_by_phone(self):
        self.authenticate('rep1', 'rep1pass1')
        out = self.rpc('/ariane_dialer/lookup', phone='(415) 555-0101')
        self.assertEqual(out['partner']['id'], self.partner.id)
        self.assertIsNone(self.rpc('/ariane_dialer/lookup', phone='+1 212 000 0000')['partner'])

    # ------------------------------------------------------------ quotes
    def test_add_to_quote_is_priced_by_odoo_not_the_browser(self):
        self.authenticate('rep1', 'rep1pass1')
        out = self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id,
                       product_id=self.product.id, qty=3)
        order = self.env['sale.order'].browse(out['order_id'])
        self.assertEqual(order.state, 'draft')
        self.assertEqual(order.user_id, self.rep)
        # the price Odoo gives the same line when nobody sends one (90 here;
        # a tenant's own pricing rules may give another)
        control = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'pricelist_id': order.pricelist_id.id,
            'order_line': [(0, 0, {'product_id': self.product.id, 'product_uom_qty': 3})]})
        expected = control.order_line.price_unit
        self.assertNotAlmostEqual(expected, 0.01)
        self.assertAlmostEqual(order.order_line.price_unit, expected)
        self.assertEqual(order.order_line.product_uom_qty, 3)
        # a price sent by the browser is ignored: Odoo prices the line
        again = self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id,
                         product_id=self.product.id, qty=1, price_unit=0.01, order_id=order.id)
        self.assertAlmostEqual(again['line_price'], expected)
        self.assertNotIn(0.01, order.order_line.mapped('price_unit'))

    def test_add_to_quote_refuses_another_company_order(self):
        self.authenticate('rep1', 'rep1pass1')
        with self.assertRaises(JsonRpcException):
            self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id,
                     product_id=self.product.id, qty=1, order_id=self.other_order.id)
        self.assertFalse(self.other_order.order_line)
        # positive control: the rep's own draft takes the line
        own = self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id,
                       product_id=self.product.id, qty=1)
        again = self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id,
                         product_id=self.product.id, qty=2, order_id=own['order_id'])
        self.assertEqual(again['order_id'], own['order_id'])

    def test_add_to_quote_rejects_bad_quantity(self):
        self.authenticate('rep1', 'rep1pass1')
        for bad in (0, -5, 10 ** 9):
            with self.assertRaises(JsonRpcException):
                self.rpc('/ariane_dialer/quote/add', partner_id=self.partner.id,
                         product_id=self.product.id, qty=bad)
        self.assertFalse(self.env['sale.order'].search([('user_id', '=', self.rep.id)]))

    # ------------------------------------------------------------ calls
    def test_wrapup_logs_on_customer_and_schedules_callback(self):
        self.authenticate('rep1', 'rep1pass1')
        call = self.rpc('/ariane_dialer/call/start', phone='+14155550101',
                        partner_id=self.partner.id, preview=True)
        self.rpc('/ariane_dialer/call/wrapup', call_id=call['call_id'], disposition='callback',
                 note='Wants the price list', callback_date='2030-01-15')
        rec = self.env['ariane.dialer.call'].browse(call['call_id'])
        self.assertEqual(rec.disposition, 'callback')
        self.assertEqual(rec.state, 'done')
        self.assertIn('Wants the price list', self.partner.message_ids[0].body)
        act = self.partner.activity_ids.filtered(lambda a: a.summary == 'Call back')
        self.assertEqual(str(act.date_deadline), '2030-01-15')
        self.assertEqual(act.user_id, self.rep)

    def test_rep_cannot_wrap_up_another_reps_call(self):
        self.authenticate('rep2', 'rep2pass2')
        theirs = self.rpc('/ariane_dialer/call/start', phone='+14155550101', preview=True)
        self.authenticate('rep1', 'rep1pass1')
        with self.assertRaises(JsonRpcException):
            self.rpc('/ariane_dialer/call/wrapup', call_id=theirs['call_id'], disposition='interested', note=NOTE)
        self.assertFalse(self.env['ariane.dialer.call'].browse(theirs['call_id']).disposition)

    def test_unknown_disposition_is_refused(self):
        self.authenticate('rep1', 'rep1pass1')
        call = self.rpc('/ariane_dialer/call/start', phone='+14155550101', preview=True)
        with self.assertRaises(JsonRpcException):
            self.rpc('/ariane_dialer/call/wrapup', call_id=call['call_id'], disposition='sold_it', note=NOTE)

    def test_links_come_from_the_website_product_settings(self):
        tmpl = self.product.product_tmpl_id
        if 'accessory_product_ids' not in tmpl._fields:
            self.skipTest('website_sale not installed: no product links to read')
        dispenser = self.env['product.product'].create({'name': 'Dispenser', 'list_price': 20, 'sale_ok': True})
        cheaper = self.env['product.product'].create({'name': 'Widget Lite', 'list_price': 50, 'sale_ok': True})
        not_for_sale = self.env['product.product'].create({'name': 'Internal Part', 'sale_ok': False})
        tmpl.write({'accessory_product_ids': [(6, 0, [dispenser.id, not_for_sale.id])],
                    'alternative_product_ids': [(6, 0, [cheaper.product_tmpl_id.id])]})
        self.authenticate('rep1', 'rep1pass1')
        card = self.rpc('/ariane_dialer/product', product_id=self.product.id, partner_id=self.partner.id)
        self.assertEqual([l['name'] for l in card['links']['goes_with']], ['Dispenser'])
        self.assertAlmostEqual(card['links']['goes_with'][0]['price'], 18.0)
        self.assertEqual([l['name'] for l in card['links']['instead_of']], ['Widget Lite'])

    # ------------------------------------------------------------ live coach context
    def _context_setup(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('ariane_dialer.tenant_id', 'tenant-x')
        icp.set_param('ariane_dialer.signing_key', 'k' * 32)
        self.rep.write({'company_ids': [(4, self.company2.id)]})
        self.authenticate('rep1', 'rep1pass1')
        call = self.rpc('/ariane_dialer/call/start', phone='+14155550101', partner_id=self.partner.id)
        return self.env['ariane.dialer.call'].browse(call['call_id'])

    def test_coach_context_runs_rules_in_the_calls_company_not_the_switched_one(self):
        call = self._context_setup()
        home = call.company_id
        seen = {}
        Rules = self.env.registry['ariane.dialer.rules']

        def candidates(ctrl, partner, company, limit=150):
            seen['request'] = ctrl_env_company()
            return [('top', 60, partner.env['product.product'].browse(self.product.id))]

        def ctrl_env_company():
            from odoo.http import request
            return request.env.company.id

        def availability(rules, partner, product, ship_partner=None):
            seen['rules'] = rules.env.company.id
            return {'status': 'allowed', 'text': 'allowed'}

        # The rep's company moves on after the call started. (Odoo 18 never
        # reads the browser's `cids` cookie on the server: a JSON route runs in
        # the user's main company unless the call sends a company context.)
        self.rep.company_id = self.company2
        self.env.flush_all()
        with patch.object(ArianeDialerController, '_coach_pools', candidates), \
                patch.object(Rules, '_dialer_availability', availability):
            out = self.rpc('/ariane_dialer/coach_context', call_id=call.id)
        # guard: the switch really happened, or this test proves nothing
        self.assertEqual(seen['request'], self.company2.id)
        self.assertEqual(seen['rules'], home.id)
        claims = json.loads(_b64d(out['context'].split('.')[1]))
        self.assertEqual(claims['company_id'], home.id)
        self.assertEqual([p['id'] for p in claims['products']], [self.product.id])

    def _coach_products(self, n, fixed_list):
        """n products; on ``fixed_list`` every other one has a fixed price."""
        products = self.env['product.product'].create([
            {'name': f'Coach Widget {i}', 'list_price': 50.0 + i, 'sale_ok': True} for i in range(n)])
        fixed_list.item_ids = [(0, 0, {'applied_on': '0_product_variant', 'product_id': p.id,
                                       'compute_price': 'fixed', 'fixed_price': 7.0 + i})
                               for i, p in enumerate(products) if i % 2]
        return products

    def _coach_products_and_prices(self, call, products):
        Rules = self.env.registry['ariane.dialer.rules']

        def pools(ctrl, partner, company, limit=150):
            return [('top', 60, partner.env['product.product'].browse(products.ids))]

        def availability(rules, partner, product, ship_partner=None):
            return {'status': 'allowed', 'text': ''}

        with patch.object(ArianeDialerController, '_coach_pools', pools), \
                patch.object(Rules, '_dialer_availability', availability):
            self.rpc('/ariane_dialer/coach_context', call_id=call.id)  # warm, as a running server is
            with queries_on('product_pricelist_item') as counts:
                out = self.rpc('/ariane_dialer/coach_context', call_id=call.id)
        claims = json.loads(_b64d(out['context'].split('.')[1]))
        return counts, {p['id']: p['price'] for p in claims['products']}

    def test_coach_context_prices_its_products_in_a_few_queries_however_many(self):
        """Staging, 22 Oct 2026: 179 pricelist queries for one coach list (a rule
        search for each price, then again for "is it the list price")."""
        call = self._context_setup()
        trade = self.env['product.pricelist'].create({'name': 'Coach Trade', 'item_ids': [
            (0, 0, {'applied_on': '3_global', 'compute_price': 'percentage', 'percent_price': 10})]})
        self.partner.property_product_pricelist = trade
        few = self._coach_products(10, trade)
        small, prices_few = self._coach_products_and_prices(call, few)
        many = few | self._coach_products(30, trade)
        big, prices_many = self._coach_products_and_prices(call, many)
        # control: every product is on the list, at the price the quote uses
        rules = self.env['ariane.dialer.rules'].with_user(self.rep)
        expected = {p.id: rules._dialer_price(self.partner, p, 1.0)['price'] for p in many}
        self.assertEqual(prices_many, expected)
        self.assertEqual(prices_few, {k: v for k, v in expected.items() if k in few.ids})
        self.assertEqual(expected[many[1].id], 8.0)  # a fixed rule
        self.assertEqual(expected[many[0].id], 45.0)  # 50 less 10%
        self.assertLessEqual(big['product_pricelist_item'], small['product_pricelist_item'] + 1,
                             f'pricelist queries grew with the products: 10 -> {small}, 40 -> {big}')

    def test_prefetched_prices_are_the_answers_the_quote_gets(self):
        """The coach's batched prices equal one-by-one prices: fixed rules,
        a list-price discount reached through two based-on pricelists, a
        quantity break at a minimum order, a marker list price."""
        p0, p1, p2, p3 = self.env['product.product'].create([
            {'name': f'Chain Widget {i}', 'list_price': 100.0 + i, 'sale_ok': True} for i in range(4)])
        base = self.env['product.pricelist'].create({'name': 'Chain Base', 'item_ids': [
            (0, 0, {'applied_on': '0_product_variant', 'product_id': p0.id, 'compute_price': 'fixed', 'fixed_price': 61}),
            (0, 0, {'applied_on': '0_product_variant', 'product_id': p3.id, 'min_quantity': 5,
                    'compute_price': 'fixed', 'fixed_price': 33}),
            (0, 0, {'applied_on': '3_global', 'compute_price': 'percentage', 'percent_price': 20})]})
        mid = self.env['product.pricelist'].create({'name': 'Chain Mid', 'item_ids': [
            (0, 0, {'applied_on': '3_global', 'compute_price': 'formula', 'base': 'pricelist',
                    'base_pricelist_id': base.id})]})
        top = self.env['product.pricelist'].create({'name': 'Chain Top', 'item_ids': [
            (0, 0, {'applied_on': '0_product_variant', 'product_id': p1.id, 'compute_price': 'fixed', 'fixed_price': 72}),
            (0, 0, {'applied_on': '3_global', 'compute_price': 'formula', 'base': 'pricelist',
                    'base_pricelist_id': mid.id})]})
        self.partner.property_product_pricelist = top
        self.env['ir.config_parameter'].sudo().set_param('ariane_dialer.price_block_at', '100000')
        products = p0 | p1 | p2 | p3 | self.sentinel
        Rules = self.env.registry['ariane.dialer.rules']
        rules = self.env['ariane.dialer.rules'].with_user(self.rep)
        with patch.object(Rules, '_dialer_moq', lambda r, product: 5.0 if product == p3 else 1.0):
            want = {p.id: rules._dialer_price(self.partner, p, rules._dialer_moq(p)) for p in products}
            fast = rules._dialer_coach_prefetch(self.partner, products)
            with queries_on('product_pricelist_item') as counts:
                got = {p.id: fast._dialer_price(self.partner, p, fast._dialer_moq(p)) for p in products}
            # control: a question that was not prefetched (another quantity) is checked afresh
            self.assertEqual(fast._dialer_price(self.partner, p3, 1.0), rules._dialer_price(self.partner, p3, 1.0))
        self.assertEqual(got, want)
        self.assertEqual(counts['product_pricelist_item'], 0, 'the prefetched prices were not used')
        self.assertEqual([(want[p.id]['price'], want[p.id].get('from_list')) for p in (p0, p1, p2, p3)],
                         [(61.0, False), (72.0, False), (81.6, True), (33.0, False)])
        self.assertIsNone(want[self.sentinel.id]['price'])

    def test_coach_context_refuses_a_rep_without_the_calls_company(self):
        call = self._context_setup()
        # positive control: the call's own rep gets a signed context
        self.assertTrue(self.rpc('/ariane_dialer/coach_context', call_id=call.id)['configured'])
        self.rep.write({'company_id': self.company2.id, 'company_ids': [(6, 0, [self.company2.id])]})
        self.authenticate('rep1', 'rep1pass1')
        with self.assertRaises(JsonRpcException):
            self.rpc('/ariane_dialer/coach_context', call_id=call.id)

    def test_coach_context_gives_the_browser_each_published_products_shop_page(self):
        if 'website_url' not in self.env['product.product']._fields:
            self.skipTest('website_sale not installed: no shop pages to link')
        call = self._context_setup()
        unlisted = self.env['product.product'].create({'name': 'Unlisted', 'list_price': 30, 'sale_ok': True})
        self.product.product_tmpl_id.is_published = True
        unlisted.product_tmpl_id.is_published = False
        Rules = self.env.registry['ariane.dialer.rules']

        def candidates(ctrl, partner, company, limit=150):
            return [('top', 60, partner.env['product.product'].browse([self.product.id, unlisted.id]))]

        def availability(rules, partner, product, ship_partner=None):
            return {'status': 'allowed', 'text': 'allowed'}

        with patch.object(ArianeDialerController, '_coach_pools', candidates), \
                patch.object(Rules, '_dialer_availability', availability):
            out = self.rpc('/ariane_dialer/coach_context', call_id=call.id)
        claims = json.loads(_b64d(out['context'].split('.')[1]))
        # control: the coach may recommend both, so a missing link is the website's call
        self.assertEqual({p['id'] for p in claims['products']}, {self.product.id, unlisted.id})
        self.assertEqual(set(out.get('links', {})), {str(self.product.id)})
        url = out['links'][str(self.product.id)]
        self.assertTrue(url.startswith('http') and url.endswith(self.product.website_url), url)
        # the links go to the browser only, never into what the coach reads
        self.assertNotIn(self.product.website_url, json.dumps(claims))

    def _big_sellers(self, groups):
        """Products that out-sell everything on a real database copy, sold to
        another customer (so the caller's own-purchases pool stays empty).
        groups: [(category name, how many, units each)]. Returns {name: products}."""
        buyer = self.env['res.partner'].create({'name': 'Big Buyer'})
        out, lines = {}, []
        for name, count, units in groups:
            categ = self.env['product.category'].create({'name': name})
            out[name] = self.env['product.product'].create([
                {'name': f'{name} {i}', 'list_price': 10.0, 'sale_ok': True, 'categ_id': categ.id}
                for i in range(count)])
            lines += [(0, 0, {'product_id': p.id, 'product_uom_qty': units}) for p in out[name]]
        order = self.env['sale.order'].create({'partner_id': buyer.id, 'order_line': lines})
        # Marked sold in SQL: a tenant's own checkout rules and automations
        # (delivery state, compliance, payment-link mails) are not what these
        # tests are about, and a write would set them off.
        order.flush_recordset()
        self.env.cr.execute("UPDATE sale_order SET state = 'sale' WHERE id = %s", [order.id])
        self.env.cr.execute("UPDATE sale_order_line SET state = 'sale' WHERE order_id = %s", [order.id])
        self.env.invalidate_all()
        return out

    def _coach_claims(self, call, blocked=()):
        Rules = self.env.registry['ariane.dialer.rules']

        def availability(rules, partner, product, ship_partner=None):
            return {'status': 'blocked' if product.id in blocked else 'allowed', 'text': ''}

        with patch.object(Rules, '_dialer_availability', availability):
            out = self.rpc('/ariane_dialer/coach_context', call_id=call.id)
        return json.loads(_b64d(out['context'].split('.')[1]))

    def test_coach_list_keeps_places_for_the_companys_featured_products(self):
        call = self._context_setup()
        self._big_sellers([('Chargers', 65, 2000000)])
        new = self.env['product.product'].create([
            {'name': f'New Brand {i}', 'list_price': 25.0, 'sale_ok': True} for i in range(3)])
        Rules = self.env.registry['ariane.dialer.rules']

        def featured(rules, partner, company, limit=120):
            return new

        # create=True: so the same test runs, and fails, on code without the hook
        with patch.object(Rules, '_dialer_featured_products', featured, create=True):
            claims = self._coach_claims(call, blocked={new[2].id})
        by_id = {p['id']: p for p in claims['products']}
        # never sold, yet on the list, marked for the coach
        self.assertTrue(by_id.get(new[0].id, {}).get('featured'), list(by_id)[:5])
        self.assertTrue(by_id.get(new[1].id, {}).get('featured'))
        # a featured product this customer may not buy stays off
        self.assertNotIn(new[2].id, by_id)
        # control: best sellers are still there, unmarked, and the list is capped
        self.assertGreater(sum(1 for p in claims['products'] if not p['featured']), 40)
        self.assertLessEqual(len(claims['products']), 60)
        self.assertEqual(by_id[new[0].id].get('category'), new[0].categ_id.name)

    def test_coach_list_takes_best_sellers_from_every_category_in_turn(self):
        call = self._context_setup()
        sold = self._big_sellers([('Chargers', 65, 2000000), ('Glassware', 2, 1500000)])
        ids = {p['id'] for p in self._coach_claims(call)['products']}
        # Glassware sells less than all 65 Chargers, yet still gets on the list
        self.assertTrue(set(sold['Glassware'].ids) <= ids)
        # control: the list is full, so something was left out to make room
        self.assertEqual(len(ids), 60)

    # ------------------------------------------------------------ live call on the contact
    def test_ai_rep_demo_group_is_given_to_nobody_by_default(self):
        demo = self.env.ref('ariane_dialer.group_dialer_demo')
        # a live database may have granted it by hand; what must hold is that no
        # dialler user ever gets it just by being a dialler user
        dialler = self.env.ref('ariane_dialer.group_dialer_user')
        self.assertNotIn(demo, dialler.trans_implied_ids)
        self.assertFalse(self.rep.has_group('ariane_dialer.group_dialer_demo'),
                         'the AI-rep button must be granted by hand')
        # positive control: granting it makes the same user a member, and a dialler user
        self.rep2.groups_id = [(4, demo.id)]
        self.assertTrue(self.rep2.has_group('ariane_dialer.group_dialer_demo'))
        self.assertTrue(self.rep2.has_group('ariane_dialer.group_dialer_user'))

    def test_contact_calls_tab_shows_the_live_call(self):
        arch = self.env['res.partner'].with_user(self.rep).get_views([(False, 'form')])['views']['form']['arch']
        page = arch[arch.index('name="ariane_calls"'):]
        self.assertIn('name="ariane_live_transcript"', page[:page.index('</page>')])
        # a user outside the dialler never gets this tab (the widget itself only
        # ever shows the viewer's own call, so another module's tab may carry it too)
        plain = self.env['res.partner'].with_user(self.outsider).get_views([(False, 'form')])['views']['form']['arch']
        self.assertNotIn('name="ariane_calls"', plain)

    # ------------------------------------------------------------ power dialler list
    def _call_todo(self, record, user, days, summary, kind='mail.mail_activity_data_call'):
        return self.env['mail.activity'].create({
            'res_model_id': self.env['ir.model']._get_id(record._name), 'res_id': record.id,
            'activity_type_id': self.env.ref(kind).id, 'summary': summary, 'user_id': user.id,
            'date_deadline': fields.Date.today() + timedelta(days=days)})

    def test_call_list_is_the_reps_own_calls_due_now_one_row_per_customer(self):
        late = self.env['res.partner'].create({'name': 'Late Deli', 'mobile': '+1 415 555 0199'})
        buyer = self.env['res.partner'].create({'name': 'Shop Buyer', 'parent_id': self.partner.id})
        # the rep's own lead: a salesman reads only their own leads
        lead = self.env['crm.lead'].create({'name': 'New cafe', 'phone': '+1 415 555 0177', 'user_id': self.rep.id})
        self._call_todo(self.partner, self.rep, 0, 'Reorder chat')
        self._call_todo(buyer, self.rep, 0, 'Second person, same shop')
        self._call_todo(late, self.rep, -2, 'Missed callback')
        self._call_todo(lead, self.rep, 0, 'First call')
        # on its own customer, so one-row-per-customer cannot hide it
        self._call_todo(self.env['res.partner'].create({'name': 'Next Week Bar', 'phone': '+1 415 555 0188'}),
                        self.rep, 1, 'Tomorrow')
        self._call_todo(late, self.rep2, -5, 'Not mine')
        self._call_todo(late, self.rep, -9, 'Send a catalogue', kind='mail.mail_activity_data_todo')
        self.authenticate('rep1', 'rep1pass1')
        items = self.rpc('/ariane_dialer/queue')['items']
        # control: the list is not empty, and holds what is due for this rep
        self.assertEqual([i['reason'] for i in items], ['Missed callback', 'Reorder chat', 'First call'])
        self.assertTrue(items[0]['overdue'] and not items[1]['overdue'])
        self.assertEqual(items[0]['phone'], '+1 415 555 0199')  # mobile when there is no phone
        self.assertEqual((items[1]['partner_id'], items[1]['phone']), (self.partner.id, '+1 415 555 0101'))
        self.assertEqual((items[2]['lead_id'], items[2]['partner_id']), (lead.id, False))
        # another rep sees only their own
        self.authenticate('rep2', 'rep2pass2')
        self.assertEqual([i['reason'] for i in self.rpc('/ariane_dialer/queue')['items']], ['Not mine'])
        # a user outside the dialler gets nothing
        self.authenticate('plain', 'plainpass1')
        with self.assertRaises(JsonRpcException):
            self.rpc('/ariane_dialer/queue')

    def test_call_list_is_not_crowded_out_by_one_busy_customer(self):
        busy = self.env['res.partner'].create({'name': 'Busy Bar', 'phone': '+1 415 555 0166'})
        for n in range(9):
            self._call_todo(busy, self.rep, -3, f'Busy call {n}')
        self._call_todo(self.partner, self.rep, 0, 'Reorder chat')
        self.authenticate('rep1', 'rep1pass1')
        names = [i['name'] for i in self.rpc('/ariane_dialer/queue', limit=2)['items']]
        # control: the busy customer is first (earliest); the second customer still gets its row
        self.assertEqual(names, ['Busy Bar', 'Corner Shop'])

    def test_call_list_leaves_out_customers_filed_in_another_company(self):
        # Its own shared address. A tenant's defaults may stamp the current
        # company on any new partner, so the stamp is cleared in SQL.
        address = self.env['res.partner'].create({'name': 'Sister Co', 'is_company': True})
        address.flush_recordset()
        self.env.cr.execute("UPDATE res_partner SET company_id = NULL WHERE id = %s", [address.id])
        self.env.invalidate_all()
        other = self.env['res.company'].create({'name': 'Sister Co', 'partner_id': address.id})
        self.rep.write({'company_ids': [(4, other.id)]})
        theirs = self.env['res.partner'].create({'name': 'Sister Shop', 'phone': '+1 415 555 0155', 'company_id': other.id})
        self._call_todo(theirs, self.rep, -1, 'Sister call')
        self._call_todo(self.partner, self.rep, 0, 'Reorder chat')
        self.authenticate('rep1', 'rep1pass1')
        names = [i['name'] for i in self.rpc('/ariane_dialer/queue')['items']]
        # the rep can read Sister Shop, so only the company rule keeps it off this list
        self.assertTrue(theirs.with_user(self.rep).has_access('read'))
        self.assertEqual(names, ['Corner Shop'])

    def test_call_list_skips_a_record_the_rep_cannot_open(self):
        # the rep's own call, but on a lead another salesman owns (a salesman reads only their own)
        hidden = self.env['crm.lead'].create({'name': 'Their lead', 'phone': '+1 415 555 0144', 'user_id': self.rep2.id})
        self._call_todo(hidden, self.rep, -1, 'Hidden call')
        self._call_todo(self.partner, self.rep, 0, 'Reorder chat')
        self.assertFalse(hidden.with_user(self.rep).has_access('read'))
        self.authenticate('rep1', 'rep1pass1')
        try:
            items = self.rpc('/ariane_dialer/queue')['items']
        except Exception as e:  # noqa: BLE001 - the list must load, not fail
            self.fail(f'the call list failed to load: {e}')
        self.assertEqual([i['name'] for i in items], ['Corner Shop'])
