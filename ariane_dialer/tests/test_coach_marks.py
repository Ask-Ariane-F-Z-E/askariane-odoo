"""The coach's marks: clearance, hot in the customer's state, and the filler
lines (design 2026-10-21 "Dialler live coach: new sellers, hot by state,
clearance, filler lines", tests O1-O9).

Everything runs in a company made for the test, so a real database copy's own
sales cannot push the test's products off a "hot" list. Every denial has a
positive control in the same test.
"""
import base64
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, tagged, new_test_user

from .common import GenericRulesMixin, clear_dialer_cache, new_company


def _b64d(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


@tagged('post_install', '-at_install')
class TestCoachMarks(GenericRulesMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.home = new_company(cls.env, 'Marks Home Co')
        cls.other = new_company(cls.env, 'Marks Other Co')
        cls.rep = new_test_user(cls.env, login='markrep', password='markrep1pass',  # pragma: allowlist secret
                                groups='ariane_dialer.group_dialer_user',
                                company_id=cls.home.id, company_ids=[(6, 0, [cls.home.id])])
        cls.manager = new_test_user(cls.env, login='markmgr', password='markmgr1pass',  # pragma: allowlist secret
                                    groups='sales_team.group_sale_manager,ariane_dialer.group_dialer_user',
                                    company_id=cls.home.id, company_ids=[(6, 0, [cls.home.id])])
        # may edit products, but is no sales manager
        cls.stock_mgr = new_test_user(cls.env, login='markstock', password='markstock1pass',  # pragma: allowlist secret
                                      groups='stock.group_stock_manager',
                                      company_id=cls.home.id, company_ids=[(6, 0, [cls.home.id])])
        us = cls.env.ref('base.us')
        State = cls.env['res.country.state']
        cls.tx = State.search([('country_id', '=', us.id), ('code', '=', 'TX')])
        cls.ca = State.search([('country_id', '=', us.id), ('code', '=', 'CA')])
        cls.pricelist = cls.env['product.pricelist'].create({'name': 'Marks Trade', 'company_id': False})
        cls.partner = cls._customer('Marks Corner Shop')
        cls.dock = cls.env['res.partner'].create({
            'name': 'Marks Dock', 'parent_id': cls.partner.id, 'type': 'delivery',
            'state_id': cls.tx.id, 'country_id': us.id})
        icp = cls.env['ir.config_parameter'].sudo()
        icp.set_param('ariane_dialer.tenant_id', 'tenant-x')
        icp.set_param('ariane_dialer.signing_key', 'k' * 32)
        # a real database may hold its own values: the tests use the defaults
        for key, value in (('ariane_dialer.hot_days', '30'), ('ariane_dialer.hot_count', '5'),
                           ('ariane_dialer.hot_min_orders', '3'), ('ariane_dialer.filler_phrases', '')):
            icp.set_param(key, value)

    @classmethod
    def _customer(cls, name, state=None):
        partner = cls.env['res.partner'].create({
            'name': name, 'is_company': True, 'state_id': state.id if state else False,
            'country_id': cls.env.ref('base.us').id})
        partner.company_id = False
        partner.with_company(cls.home).property_product_pricelist = cls.pricelist
        return partner

    def rpc(self, route, **params):
        return self.make_jsonrpc_request(route, params)

    # ------------------------------------------------------------ helpers
    def _product(self, name, **vals):
        return self.env['product.product'].create({'name': name, 'list_price': 20.0, 'sale_ok': True, **vals})

    def _flag(self, product, until=None):
        product.product_tmpl_id.sudo().write({'dialer_clearance': True, 'dialer_clearance_until': until or False})

    def _sell(self, product, customer, units=1.0, company=None, days_ago=1, state='sale', orders=1):
        """``orders`` confirmed (or draft) orders of ``units`` each, shipped to the
        customer's own address. State and date are set in SQL: a tenant's checkout
        rules are not what these tests are about."""
        company = company or self.home
        for _n in range(orders):
            order = self.env['sale.order'].with_company(company).create({
                'partner_id': customer.id, 'partner_shipping_id': customer.id, 'company_id': company.id,
                'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': units})]})
            order.flush_recordset()
            self.env.cr.execute(
                "UPDATE sale_order SET state = %s, date_order = (now() at time zone 'utc') - %s * interval '1 day' "
                "WHERE id = %s", [state, days_ago, order.id])
            self.env.cr.execute("UPDATE sale_order_line SET state = %s WHERE order_id = %s", [state, order.id])
        self.env.invalidate_all()

    def _fill(self, count=65):
        """Best sellers that fill the coach's list, sold to another customer."""
        buyer = self._customer('Marks Big Buyer')
        categ = self.env['product.category'].create({'name': 'Marks Chargers'})
        products = self.env['product.product'].create([
            {'name': f'Marks Charger {i}', 'list_price': 10.0, 'sale_ok': True, 'categ_id': categ.id}
            for i in range(count)])
        order = self.env['sale.order'].with_company(self.home).create({
            'partner_id': buyer.id, 'company_id': self.home.id,
            'order_line': [(0, 0, {'product_id': p.id, 'product_uom_qty': 5000}) for p in products]})
        order.flush_recordset()
        self.env.cr.execute("UPDATE sale_order SET state = 'sale' WHERE id = %s", [order.id])
        self.env.cr.execute("UPDATE sale_order_line SET state = 'sale' WHERE order_id = %s", [order.id])
        self.env.invalidate_all()
        return products

    def _context(self, partner=None, blocked=()):
        """(response, signed claims, {product id: line}) for a call to ``partner``."""
        self.authenticate('markrep', 'markrep1pass')
        call = self.rpc('/ariane_dialer/call/start', phone='+14155550101',
                        partner_id=(partner or self.partner).id)['call_id']
        Rules = self.env.registry['ariane.dialer.rules']

        def availability(rules, partner, product, ship_partner=None):
            return {'status': 'blocked' if product.id in blocked else 'allowed', 'text': ''}

        with patch.object(Rules, '_dialer_availability', availability):
            out = self.rpc('/ariane_dialer/coach_context', call_id=call)
        claims = json.loads(_b64d(out['context'].split('.')[1]))
        return out, claims, {p['id']: p for p in claims['products']}

    # ------------------------------------------------------------ clearance
    def test_clearance_flag_puts_the_product_on_the_coach_list_with_its_badge(self):
        chargers = self._fill()
        flagged, plain = self._product('Marks Flagged Pipe'), self._product('Marks Plain Pipe')
        bought = self._product('Marks Bought Flagged')
        self._sell(bought, self.partner)
        for product in (flagged, bought):
            self._flag(product)
        _out, claims, by_id = self._context()
        # never sold, yet on a full list, with its badge
        self.assertEqual(len(claims['products']), 60)
        self.assertEqual(by_id.get(flagged.id, {}).get('badges'), ['clearance'], list(by_id)[:8])
        # control: the same kind of product, not flagged, does not get a place
        self.assertNotIn(plain.id, by_id)
        # placed by what this customer buys, and still marked: the badge is membership
        self.assertEqual(by_id[bought.id]['badges'], ['clearance'])
        # control: every other line has no badge
        others = [p for p in claims['products'] if p['id'] not in (flagged.id, bought.id)]
        self.assertTrue(others and all(p['badges'] == [] for p in others))
        self.assertTrue(set(chargers.ids) & set(by_id))

    def test_expired_clearance_gives_no_badge(self):
        self._fill()
        today = fields.Date.context_today(self.env['res.partner'])
        expired, last_day, open_ended = (self._product(f'Marks Clear {n}') for n in ('expired', 'today', 'open'))
        bought_expired = self._product('Marks Bought Expired')
        self._sell(bought_expired, self.partner)
        self._flag(expired, today - timedelta(days=1))
        self._flag(bought_expired, today - timedelta(days=1))
        self._flag(last_day, today)
        self._flag(open_ended)
        _out, _claims, by_id = self._context()
        self.assertNotIn(expired.id, by_id)
        # on the list because the customer buys it, but the flag has run out
        self.assertEqual(by_id[bought_expired.id]['badges'], [])
        # controls: the last day still counts, and no end date means until cleared
        self.assertEqual(by_id.get(last_day.id, {}).get('badges'), ['clearance'])
        self.assertEqual(by_id.get(open_ended.id, {}).get('badges'), ['clearance'])

    def test_a_rep_cannot_read_or_write_the_clearance_flag(self):
        tmpl = self._product('Marks Guarded').product_tmpl_id
        for field in ('dialer_clearance', 'dialer_clearance_until'):
            with self.assertRaises(AccessError):
                tmpl.with_user(self.rep).read([field])
        # a user who may edit products, but is not a sales manager
        tmpl.with_user(self.stock_mgr).write({'name': 'Marks Guarded 2'})  # control: may edit the product
        with self.assertRaises(AccessError):
            tmpl.with_user(self.stock_mgr).write({'dialer_clearance': True})
        self.assertFalse(tmpl.sudo().dialer_clearance)
        # positive control: a sales manager can set and read it
        tmpl.with_user(self.manager).write({'dialer_clearance': True,
                                            'dialer_clearance_until': fields.Date.today() + timedelta(days=9)})
        self.assertTrue(tmpl.with_user(self.manager).read(['dialer_clearance'])[0]['dialer_clearance'])

    def test_a_clearance_product_this_customer_may_not_buy_stays_off(self):
        banned, allowed = self._product('Marks Banned Clear'), self._product('Marks Allowed Clear')
        self._flag(banned)
        self._flag(allowed)
        out, _claims, by_id = self._context(blocked={banned.id})
        self.assertNotIn(banned.id, by_id)
        self.assertNotIn(banned.id, [i['id'] for i in out['hot']['items']])
        # control: the same flag on a product the customer may buy
        self.assertEqual(by_id.get(allowed.id, {}).get('badges'), ['clearance'])

    # ------------------------------------------------------------ hot in the state
    def _shops(self, count, state, prefix):
        return [self._customer(f'{prefix} {i}', state) for i in range(count)]

    def test_hot_counts_only_confirmed_orders_in_the_calls_company_state_and_window(self):
        hot = self._product('Marks Hot')
        controls = {name: self._product(f'Marks Not Hot {name}')
                    for name in ('other_company', 'other_state', 'draft', 'too_old')}
        tx_shops, ca_shops = self._shops(3, self.tx, 'Marks TX'), self._shops(3, self.ca, 'Marks CA')
        for shop in tx_shops:
            self._sell(hot, shop)
            self._sell(controls['other_company'], shop, company=self.other)
            self._sell(controls['draft'], shop, state='draft')
            self._sell(controls['too_old'], shop, days_ago=40)
        for shop in ca_shops:
            self._sell(controls['other_state'], shop)
        out, _claims, by_id = self._context()
        self.assertEqual((out['hot']['status'], out['hot']['state'], out['hot']['days']), ('ok', 'TX', 30))
        self.assertEqual([(i['id'], i['rank']) for i in out['hot']['items']], [(hot.id, 1)])
        self.assertEqual(out['hot']['items'][0]['badges'], ['hot'])
        self.assertEqual((by_id[hot.id]['hot_rank'], by_id[hot.id]['badges']), (1, ['hot']))
        # each control differs from the hot product in one thing only
        for name, product in controls.items():
            self.assertNotIn('hot_rank', by_id.get(product.id, {}), name)
            self.assertNotIn('hot', by_id.get(product.id, {}).get('badges', []), name)

    def test_signed_customer_carries_the_state_hot_was_ranked_in(self):
        # Billed in CA, delivered to TX: hot is ranked by the DELIVERY state, and
        # the coach names that state from the signed customer.hot_state.
        hot = self._product('Marks Split Hot')
        for shop in self._shops(3, self.tx, 'Marks Split TX'):
            self._sell(hot, shop)
        split = self._customer('Marks Split Shop', self.ca)
        self.env['res.partner'].create({
            'name': 'Marks Split Dock', 'parent_id': split.id, 'type': 'delivery',
            'state_id': self.tx.id, 'country_id': self.env.ref('base.us').id})
        out, claims, by_id = self._context(partner=split)
        self.assertEqual((out['hot']['status'], out['hot']['state']), ('ok', 'TX'))
        self.assertEqual(by_id[hot.id]['hot_rank'], 1)
        self.assertEqual(claims['customer']['state'], 'CA')         # the customer's own state is unchanged
        self.assertEqual(claims['customer']['hot_state'], 'TX')
        # control: no delivery state at all signs an empty hot_state
        _out, claims, _by_id = self._context(partner=self._customer('Marks Split None'))
        self.assertEqual(claims['customer']['hot_state'], '')

    def test_one_big_buyer_cannot_make_a_product_hot(self):
        spread, pair, duo, loyal, bulk = (self._product(f'Marks {n}')
                                          for n in ('Spread', 'Pair', 'Duo', 'Loyal', 'Bulk'))
        shops = self._shops(3, self.tx, 'Marks Shop')
        # three buyers, three orders, three units
        for shop in shops:
            self._sell(spread, shop, units=1)
        # two buyers, three orders (the smallest hot product) and ten times the
        # units of spread: ranked by units first, pair would come out on top
        self._sell(pair, shops[0], units=10, orders=2)
        self._sell(pair, shops[1], units=10)
        # two different buyers, but two orders: under the minimum of three
        # orders. Only the minimum-orders rule keeps it off (it has the buyers).
        self._sell(duo, shops[0], units=1000)
        self._sell(duo, shops[2], units=1000)
        # one buyer, many orders and far more units: "selling to shops in TX" would be false
        self._sell(loyal, shops[0], units=1000, orders=5)
        # one buyer, two huge orders: under the minimum of three orders as well
        self._sell(bulk, shops[1], units=50000, orders=2)
        out, _claims, by_id = self._context()
        # ranked by distinct buyers first: units cannot lift pair over spread
        self.assertEqual([(i['id'], i['rank']) for i in out['hot']['items']], [(spread.id, 1), (pair.id, 2)])
        for product in (duo, loyal, bulk):
            self.assertNotIn('hot_rank', by_id.get(product.id, {}), product.name)
            self.assertNotIn('hot', by_id.get(product.id, {}).get('badges', []), product.name)
        # positive control: one more order takes duo to the minimum, and it is hot,
        # above pair (same buyers, more units) and below spread (more buyers)
        self._sell(duo, shops[2], units=1)
        clear_dialer_cache()
        out, _claims, by_id = self._context()
        self.assertEqual([(i['id'], i['rank']) for i in out['hot']['items']],
                         [(spread.id, 1), (duo.id, 2), (pair.id, 3)])
        self.assertEqual(by_id[duo.id]['badges'], ['hot'])

    def test_a_hook_that_fails_in_sql_costs_its_mark_not_the_coach_list(self):
        # A hook that fails inside Postgres (a statement timeout on the hot
        # read, a tenant override's raw SQL) aborts the transaction unless it
        # ran in its own savepoint; the next query would then fail and the
        # whole coach list would be lost, not just that hook's mark.
        bought = self._product('Marks Sql Bought')
        self._sell(bought, self.partner)
        # a new seller's product this customer also buys: on the list either way,
        # so a lost badge is the mark lost, not the line
        fresh = self._product('Marks Sql New Seller')
        self._sell(fresh, self.partner)
        flagged, hot = self._product('Marks Sql Flagged'), self._product('Marks Sql Hot')
        self._flag(flagged)
        for shop in self._shops(3, self.tx, 'Marks Sql TX'):
            self._sell(hot, shop)
        Rules = self.env.registry['ariane.dialer.rules']

        def broken(rules, *args, **kwargs):
            rules.env.cr.execute('SELECT 1/0', log_exceptions=False)

        def new_sellers(rules, company, limit=200):
            # the generic hook has none; a tenant's marketplace puts its own here
            return rules.env['product.product'].browse(fresh.id)

        marks = {'_dialer_clearance_products': (flagged, 'clearance'),
                 '_dialer_new_seller_products': (fresh, 'new_seller'),
                 '_dialer_hot_products': (hot, 'hot')}
        with patch.object(Rules, '_dialer_new_seller_products', new_sellers, create=True):
            # positive control: nothing broken, every mark is there
            clear_dialer_cache()
            out, _claims, by_id = self._context()
            self.assertEqual((by_id[flagged.id]['badges'], by_id[hot.id]['hot_rank'], out['hot']['status'],
                              by_id[fresh.id]['badges']), (['clearance'], 1, 'ok', ['new_seller']))
            for hook in marks:
                with self.subTest(hook=hook):
                    clear_dialer_cache()
                    with patch.object(Rules, hook, broken, create=True):
                        out, claims, by_id = self._context()
                    self.assertEqual(len(out['context'].split('.')), 3)
                    self.assertIn(bought.id, by_id)                    # the coach list survives
                    for name, (product, mark) in marks.items():
                        badges = by_id.get(product.id, {}).get('badges', [])
                        if name == hook:                               # only the broken hook's mark is lost
                            self.assertNotIn(mark, badges)
                        elif mark != 'hot':
                            self.assertIn(mark, badges)
                    self.assertIn(fresh.id, by_id)
                    if hook == '_dialer_hot_products':
                        self.assertEqual((out['hot']['status'], out['hot']['items']), ('off', []))
                    else:                                               # the other marks are untouched
                        self.assertEqual((out['hot']['status'], by_id[hot.id]['hot_rank']), ('ok', 1))

    def test_no_delivery_state_gives_no_hot_block_and_the_list_is_still_signed(self):
        stateless = self._customer('Marks No State')
        seller = self._product('Marks Seller')
        self._sell(seller, self._customer('Marks Someone', self.tx))
        self._flag(seller)
        Rules = self.env.registry['ariane.dialer.rules']
        asked = []

        def hot(rules, company, state, days, limit, min_orders=3):
            asked.append(state)
            return rules.env['product.product'].browse(seller.id)

        with patch.object(Rules, '_dialer_hot_products', hot, create=True):
            out, claims, by_id = self._context(partner=stateless)
        self.assertEqual(out['hot'], {'status': 'no_state', 'state': '', 'days': 30, 'items': []})
        self.assertEqual(asked, [])
        self.assertEqual(len(out['context'].split('.')), 3)
        self.assertEqual(by_id[seller.id]['badges'], ['clearance'])
        # positive control: the same call to the customer with a delivery state asks
        with patch.object(Rules, '_dialer_hot_products', hot, create=True):
            out, _claims, _by_id = self._context()
        self.assertEqual(asked, [self.tx])
        self.assertEqual(out['hot']['status'], 'ok')

    def test_hot_cache_hits_then_invalidate_by_name_recomputes(self):
        Rules = self.env.registry['ariane.dialer.rules']
        product = self._product('Marks Cached')
        calls = []

        def hot(rules, company, state, days, limit, min_orders=3):
            calls.append((company.id, state.id, days, min_orders))
            return rules.env['product.product'].browse(product.id)

        clear_dialer_cache()
        rules = self.env['ariane.dialer.rules'].with_company(self.home)
        with patch.object(Rules, '_dialer_hot_products', hot, create=True):
            first = rules._coach_hot_cached(self.home, self.tx)
            second = rules._coach_hot_cached(self.home, self.tx)
            self.assertEqual((len(calls), first.ids, second.ids), (1, [product.id], [product.id]))
            self.assertEqual(calls[0], (self.home.id, self.tx.id, 30, 3))
            # control: invalidating another list leaves this one cached
            rules._dialer_cache_invalidate('new_sellers')
            rules._coach_hot_cached(self.home, self.tx)
            self.assertEqual(len(calls), 1)
            rules._dialer_cache_invalidate('hot_by_state')
            rules._coach_hot_cached(self.home, self.tx)
            self.assertEqual(len(calls), 2)
            # the Settings button refreshes it too
            self.env['res.config.settings'].create({}).action_dialer_refresh_lists()
            rules._coach_hot_cached(self.home, self.tx)
            self.assertEqual(len(calls), 3)
            # another company or state is its own entry
            rules._coach_hot_cached(self.other, self.tx)
            rules._coach_hot_cached(self.home, self.ca)
            self.assertEqual(len(calls), 5)

    # ------------------------------------------------------------ fillers
    def test_fillers_default_when_unset_tenant_list_when_set_and_price_lines_dropped(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('ariane_dialer.api_url', 'https://dialer.example/')
        self.authenticate('markrep', 'markrep1pass')
        defaults = ["Give me a moment to check that for you.", "Let me pull up those details.",
                    "That's a good question, let me confirm."]
        self.assertEqual(self.rpc('/ariane_dialer/session')['fillers'], defaults)
        icp.set_param('ariane_dialer.filler_phrases', '\n'.join([
            'Hold on one sec', 'It is $20 today', 'We have 3 left', '', '  One moment please  ',
            'Let me look, that is £5 off', 'x' * 81, 'Hold on one sec']))
        self.assertEqual(self.rpc('/ariane_dialer/session')['fillers'], ['Hold on one sec', 'One moment please'])
        # a list where every line carries a price falls back to the defaults
        icp.set_param('ariane_dialer.filler_phrases', 'Only 5 left\n€ off today')
        self.assertEqual(self.rpc('/ariane_dialer/session')['fillers'], defaults)
        # at most ten
        icp.set_param('ariane_dialer.filler_phrases', '\n'.join(f'Phrase {chr(65 + i)}' for i in range(12)))
        self.assertEqual(self.rpc('/ariane_dialer/session')['fillers'],
                         [f'Phrase {chr(65 + i)}' for i in range(10)])
