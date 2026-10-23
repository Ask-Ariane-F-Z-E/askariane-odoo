"""JSON endpoints for the dialler panel.

Every rep action runs in the rep's own environment (request.env): no sudo(),
so Odoo's access rules and the active company decide what a rep can read or
change. The sudo() reads are of system parameters (the signing key never
leaves the server) and of units sold per product for the live coach (only
product ids leave, read back as the rep). Call rows are written through ``trusted()``: a rep's
direct RPC write to a call is refused by the model. Business rules (price, availability, MOQ, stock,
outcomes, how a call is logged) live in the ``ariane.dialer.rules`` hooks,
which a tenant module overrides; this file holds none.

Errors are raised, never caught and returned: a raised error in a JSON route
rolls back the whole request, so a refused step leaves nothing half-written.
"""
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from odoo import fields, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from odoo.osv import expression
from odoo.tools import SQL

from ..models.call_health import HEALTH_BODY_MAX, health_values
from ..models.dialer_call import trusted
from ..models.dialer_rules import DEFAULT_FILLERS, QUOTABLE, in_turn, whole_word

_logger = logging.getLogger(__name__)

ASSERTION_TTL = 300
AUDIENCE = 'ariane-dialer-api'
CONTEXT_AUDIENCE = 'ariane-dialer-context'
CONTEXT_TTL = 600
CONTEXT_PRODUCTS = 60
# Places on the coach's list, pool by pool, in this order: own, clearance,
# hot (as many as the hot-by-state setting), new seller, featured, top. Places
# a pool cannot fill (too few products this customer may buy) go to the pools
# after it.
OWN_PLACES = 15
CLEARANCE_PLACES = 6
NEW_SELLER_PLACES = 8
FEATURED_PLACES = 14
# Marks a product line may carry, in display order.
BADGES = ('clearance', 'new_seller', 'hot')
# Each pool is read this many times its places at most: every product read
# costs an availability check before the phone rings.
SCAN_FACTOR = 3
MAX_QTY = 100000
QUOTE_KEYS = 20
QUEUE_MAX = 50
# A wrap-up needs a written note of at least this many characters (owner's rule;
# the panel's MIN_NOTE_CHARS in static/src/wrapup_rules.js says the same).
MIN_NOTE_CHARS = 3
# After a reload the panel asks for the rep's own unfinished call. One still
# 'dialing' or 'live' is only handed back once it is this old: a younger one may
# be on the line in another tab.
STALE_CALL_MINUTES = 120


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def sign_assertion(claims, key):
    """Compact HS256 JWT. The dialler service verifies signature, aud, exp and jti."""
    header = _b64url(json.dumps({'alg': 'HS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
    body = _b64url(json.dumps(claims, separators=(',', ':'), sort_keys=True).encode())
    signing_input = f'{header}.{body}'.encode()
    sig = hmac.new(key.encode(), signing_input, hashlib.sha256).digest()
    return f'{header}.{body}.{_b64url(sig)}'


class ArianeDialerController(http.Controller):

    # ---------------------------------------------------------------- helpers
    def _check_user(self):
        if not request.env.user.has_group('ariane_dialer.group_dialer_user'):
            raise AccessError('You are not enabled for the dialler.')

    def _param(self, key, default=''):
        return request.env['ir.config_parameter'].sudo().get_param(key, default) or default

    def _rules(self):
        return request.env['ariane.dialer.rules']

    def _partner(self, partner_id):
        if not partner_id:
            return request.env['res.partner']
        partner = request.env['res.partner'].browse(int(partner_id)).exists()
        partner.check_access('read')
        return partner

    def _own_call(self, call_id):
        call = request.env['ariane.dialer.call'].browse(int(call_id)).exists()
        if not call or call.user_id != request.env.user:
            raise AccessError('Not your call.')
        return trusted(call)

    @staticmethod
    def _utc(value):
        """An ISO datetime from the browser (with Z or an offset) as naive UTC."""
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            raise UserError('Follow-up time is not a valid date and time.')
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    def _last_purchase(self, product, partner):
        if not partner:
            return None
        line = request.env['sale.order.line'].search([
            ('order_partner_id', 'child_of', partner.commercial_partner_id.id),
            ('product_id', '=', product.id),
            ('state', '=', 'sale'),
        ], order='id desc', limit=1)
        if not line:
            return None
        return {'date': fields.Date.to_string(line.order_id.date_order.date()),
                'qty': line.product_uom_qty, 'price_unit': line.price_unit,
                'order': line.order_id.name}

    def _product_card(self, product, partner, ship=None, with_links=True, availability=None):
        rules = self._rules()
        moq = rules._dialer_moq(product)
        card = {
            'id': product.id,
            'name': product.display_name,
            'code': product.default_code or '',
            **rules._dialer_price(partner, product, moq),
            'moq': moq,
            'breaks': rules._dialer_breaks(partner, product),
            'stock': rules._dialer_stock(partner, product),
            'last_purchase': self._last_purchase(product, partner),
            'compliance': availability or rules._dialer_availability(partner, product, ship),
            'checked_at': fields.Datetime.to_string(fields.Datetime.now()),
        }
        card.pop('rule_id', None)
        if with_links:
            card['links'] = self._links(product, partner, ship)
        return card

    def _links(self, product, partner, ship):
        """The website's own product links: accessories = goes with, alternatives = instead of.
        Products this customer may not buy are left out."""
        tmpl = product.product_tmpl_id
        goes_with = request.env['product.product']
        if 'accessory_product_ids' in tmpl._fields:
            goes_with |= tmpl.accessory_product_ids
        if 'optional_product_ids' in tmpl._fields:
            goes_with |= tmpl.optional_product_ids.product_variant_id
        instead_of = tmpl.alternative_product_ids.product_variant_id \
            if 'alternative_product_ids' in tmpl._fields else request.env['product.product']
        out = {}
        for key, products in (('goes_with', goes_with), ('instead_of', instead_of)):
            minis = [self._mini(p, partner, ship) for p in products.filtered('sale_ok')[:8]]
            minis = [m for m in minis if m][:4]
            if minis:
                out[key] = minis
        return out

    def _mini(self, product, partner, ship):
        rules = self._rules()
        if rules._dialer_availability(partner, product, ship)['status'] not in QUOTABLE:
            return None
        return {'id': product.id, 'name': product.display_name,
                **rules._dialer_price(partner, product, rules._dialer_moq(product)),
                'stock': rules._dialer_stock(partner, product)}

    def _lock(self, table, record_id):
        request.env.cr.execute(SQL('SELECT id FROM %s WHERE id = %s FOR UPDATE', SQL.identifier(table), record_id))

    # -------------------------------------------------------------- endpoints
    @http.route('/ariane_dialer/session', type='json', auth='user')
    def session(self):
        """A five-minute signed pass for the dialler service. The user id comes
        from the server session, never from the browser."""
        self._check_user()
        api_url = self._param('ariane_dialer.api_url')
        key = self._param('ariane_dialer.signing_key')
        tenant = self._param('ariane_dialer.tenant_id')
        if not (api_url and key and tenant):
            return {'configured': False}
        now = int(time.time())
        claims = {
            'iss': self._param('database.uuid'),
            'aud': AUDIENCE,
            'tenant_id': tenant,
            'uid': request.env.uid,
            'company_id': request.env.company.id,
            'company_ids': request.env.user.company_ids.ids,
            'jti': str(uuid.uuid4()),
            'iat': now,
            'exp': now + ASSERTION_TTL,
            'scope': ['voice:connect', 'coach:read'],
        }
        return {'configured': True, 'api_url': api_url.rstrip('/'),
                'assertion': sign_assertion(claims, key), 'fillers': self._fillers()}

    def _fillers(self):
        try:
            return self._rules()._dialer_fillers()
        except Exception as e:  # noqa: BLE001 - a bad setting must not stop the dialler
            _logger.warning('dialer fillers failed: %s', type(e).__name__)
            return list(DEFAULT_FILLERS)

    @http.route('/ariane_dialer/lookup', type='json', auth='user')
    def lookup(self, phone=None, partner_id=None, lead_id=None):
        """Screen pop: who is this, what have they bought, what is open."""
        self._check_user()
        env = request.env
        lead = env['crm.lead'].browse(int(lead_id)).exists() if lead_id else env['crm.lead']
        partner = self._partner(partner_id) or lead.partner_id
        if not partner and phone:
            digits = ''.join(c for c in phone if c.isdigit())[-10:]
            if len(digits) >= 7:
                partner = env['res.partner'].search(
                    ['|', ('phone_sanitized', 'like', digits), ('phone', 'like', digits)], limit=1) \
                    if 'phone_sanitized' in env['res.partner']._fields else \
                    env['res.partner'].search([('phone', 'like', digits)], limit=1)
        if not partner:
            return {'partner': None, 'lead': {'id': lead.id, 'name': lead.name} if lead else None}
        rules = self._rules()
        customer = rules._dialer_customer(partner)
        commercial = customer['commercial']
        ship = rules._dialer_ship_partner(partner)
        orders = env['sale.order'].search([('partner_id', 'child_of', commercial.id)],
                                          order='date_order desc', limit=8)
        people = env['res.partner'].search([('id', 'child_of', commercial.id)])
        activities = env['mail.activity'].search([('res_model', '=', 'res.partner'),
                                                  ('res_id', 'in', people.ids)], limit=5)
        pricelist = customer['pricelist']
        return {
            'partner': {
                'id': partner.id,
                'name': partner.display_name,
                'company': commercial.display_name,
                'phone': partner.phone or '',
                'state': customer['state'].code or '',
                'ship_state': ship.state_id.code or '',
                'tier': customer['tier'],
                'warnings': customer['warnings'],
                'salesperson': (partner.user_id or commercial.user_id).name or '',
                'pricelist': pricelist.display_name if pricelist else '',
                'total_due': getattr(commercial, 'total_due', None),
            },
            'lead': {'id': lead.id, 'name': lead.name} if lead else None,
            'orders': [{'id': o.id, 'name': o.name, 'state': o.state,
                        'date': fields.Date.to_string(o.date_order.date()),
                        'amount': o.amount_total, 'currency': o.currency_id.name}
                       for o in orders if o.state == 'sale'][:5],
            'open_quotes': [{'id': o.id, 'name': o.name, 'amount': o.amount_total}
                            for o in orders if o.state in ('draft', 'sent')],
            'activities': [{'summary': a.summary or a.activity_type_id.name,
                            'due': fields.Date.to_string(a.date_deadline)} for a in activities],
            'open_request': rules._dialer_open_request(partner),
        }

    @http.route('/ariane_dialer/products', type='json', auth='user')
    def products(self, query='', partner_id=None, limit=6, kind=''):
        """Catalogue search with this customer's price, stock and the website's links.
        In the order ``_dialer_search_tiers`` gives: what the rep types matches
        a product's name or code first; a name the coach heard (``kind`` brand,
        vendor or category) matches that kind first and a name only as a whole
        word. Products the customer may not buy are hidden and counted.
        Candidates are read in pages, so a page full of hidden products does
        not end the search; at most five pages per tier."""
        self._check_user()
        query = (query or '').strip()
        if len(query) < 2:
            return {'products': [], 'hidden': 0}
        limit = max(1, min(int(limit or 6), 12))
        partner = self._partner(partner_id)
        rules = self._rules()
        ship = rules._dialer_ship_partner(partner)
        word = whole_word(query)
        Product = request.env['product.product']
        found, hidden, seen, page = [], 0, set(), limit * 3
        for tier, whole in rules._dialer_search_tiers(query, kind):
            domain = expression.AND([[('sale_ok', '=', True)], tier])
            for offset in range(0, page * 5, page):
                batch = Product.search(domain, offset=offset, limit=page, order='name, id')
                for product in batch:
                    if product.id in seen or (whole and not word.search((product.name or '').casefold())):
                        continue
                    seen.add(product.id)
                    availability = rules._dialer_availability(partner, product, ship)
                    if availability['status'] not in QUOTABLE:
                        hidden += 1
                    elif len(found) < limit:
                        found.append((product, availability))
                if len(found) >= limit or len(batch) < page:
                    break
            if len(found) >= limit:
                break
        return {'products': [self._product_card(p, partner, ship, with_links=(i == 0), availability=a)
                             for i, (p, a) in enumerate(found)],
                'hidden': hidden}

    @http.route('/ariane_dialer/product', type='json', auth='user')
    def product(self, product_id, partner_id=None):
        self._check_user()
        product = request.env['product.product'].browse(int(product_id)).exists()
        if not product:
            raise UserError('Product not found.')
        partner = self._partner(partner_id)
        return self._product_card(product, partner, self._rules()._dialer_ship_partner(partner))

    @http.route('/ariane_dialer/queue', type='json', auth='user')
    def queue(self, limit=QUEUE_MAX):
        """The rep's own calls due now, for the power dialler. Read as the rep."""
        self._check_user()
        limit = max(1, min(int(limit or QUEUE_MAX), QUEUE_MAX))
        return {'items': self._rules()._dialer_queue(request.env.company, limit=limit)}

    @http.route('/ariane_dialer/outcomes', type='json', auth='user')
    def outcomes(self):
        self._check_user()
        return self._rules()._dialer_outcomes()

    @http.route('/ariane_dialer/coach_context', type='json', auth='user')
    def coach_context(self, call_id):
        """What the live coach may recommend on this call, signed for the dialler
        service: only products cleared ('allowed') for this customer's delivery
        state, at this customer's own price. Review, unknown and unpriced
        products are left out. Bound to this call, its rep, company, customer and
        number, so the browser that carries it cannot reuse it elsewhere."""
        self._check_user()
        key = self._param('ariane_dialer.signing_key')
        tenant = self._param('ariane_dialer.tenant_id')
        if not (key and tenant):
            return {'configured': False}
        call = self._own_call(call_id)
        company = call.company_id
        if company not in request.env.user.company_ids:
            raise AccessError('This call belongs to a company you cannot use.')
        # Rules and prices follow env.company: run them in the CALL's company,
        # never the one the rep happens to have switched to (it is what we sign).
        env = request.env(context=dict(request.env.context, allowed_company_ids=[company.id]))
        partner = env['res.partner'].browse(call.partner_id.id)
        if not partner:
            raise UserError('The live coach needs a customer on the call.')
        partner.check_access('read')
        rules = env['ariane.dialer.rules']
        customer = rules._dialer_customer(partner)
        ship = rules._dialer_ship_partner(partner)
        lines = {}

        def line_for(product):
            # one availability and price check per product, however many pools hold it
            if product.id not in lines:
                lines[product.id] = self._coach_line(rules, partner, product, ship)
            return lines[product.id]

        clearance = self._coach_hook(lambda: rules._dialer_clearance_products(company), 'clearance')
        new_sellers = self._coach_hook(lambda: rules._coach_new_sellers_cached(company), 'new_sellers')
        hot_state = ship.state_id or customer['state']
        hot_candidates = self._coach_hot_candidates(rules, company, hot_state)
        pools = self._coach_pools(partner, call.company_id)
        # Every product the lists below may look at, checked in a few batched
        # reads instead of several queries each (2-4 s per call on staging).
        rules = self._coach_prefetch(rules, partner, ship, [
            (clearance, CLEARANCE_PLACES), (new_sellers, NEW_SELLER_PLACES),
            (hot_candidates or [], len(hot_candidates or [])),
        ] + [(candidates, places) for _name, places, candidates in pools])
        hot, ranks = self._coach_hot(rules, company, hot_state, line_for, hot_candidates)
        at = next((i + 1 for i, pool in enumerate(pools) if pool[0] == 'own'), 0)
        pools[at:at] = [('clearance', CLEARANCE_PLACES, list(clearance)),
                        ('hot', len(ranks), list(rules.env['product.product'].browse(list(ranks)))),
                        ('new_seller', NEW_SELLER_PLACES, list(new_sellers))]
        featured = {p.id for name, _places, candidates in pools if name == 'featured' for p in candidates}
        products, taken = [], set()
        for _pool, places, candidates in pools:
            room = min(places, CONTEXT_PRODUCTS - len(products))
            added = 0
            for product in candidates[:max(room, 0) * SCAN_FACTOR]:
                if added >= room:
                    break
                if product.id in taken:
                    continue
                line = line_for(product)
                if line:
                    products.append(dict(line))
                    taken.add(product.id)
                    added += 1
        # Marks come from the sets, not from the pool that placed a product: a
        # clearance product this customer already buys is still marked.
        marks = {'clearance': set(clearance.ids), 'new_seller': set(new_sellers.ids), 'hot': set(ranks)}
        for line in products:
            line['featured'] = line['id'] in featured
            line['badges'] = [b for b in BADGES if line['id'] in marks[b]]
            if line['id'] in ranks:
                line['hot_rank'] = ranks[line['id']]
        hot['items'] = [{'id': line['id'], 'name': line['name'], 'rank': line['hot_rank'],
                         'badges': line['badges'], 'price': line['price'], 'currency': line['currency']}
                        for line in sorted((ln for ln in products if 'hot_rank' in ln), key=lambda ln: ln['hot_rank'])]
        pricelist = customer['pricelist']
        vocabulary = self._coach_vocabulary(env['ariane.dialer.rules'], company)
        now = int(time.time())
        claims = {
            'iss': self._param('database.uuid'), 'aud': CONTEXT_AUDIENCE, 'tenant_id': tenant,
            'uid': request.env.uid, 'company_id': call.company_id.id, 'call_id': call.id,
            'partner_id': partner.id, 'phone': call.phone, 'iat': now, 'exp': now + CONTEXT_TTL,
            # hot_state: the delivery state the hot ranks were counted in; it can
            # differ from the customer's own state, and the coach must name it.
            'customer': {'name': partner.display_name, 'state': customer['state'].code or '',
                         'hot_state': hot['state'], 'tier': customer['tier'] or '',
                         'pricelist': pricelist.display_name if pricelist else ''},
            'products': products,
            # Names the coach listens for (brand, vendor, category): names only.
            'vocabulary': vocabulary,
        }
        shop = self._shop_links(env['product.product'].browse([p['id'] for p in products]), company)
        return {'configured': True, 'context': sign_assertion(claims, key), 'products': len(products),
                'vocabulary': len(vocabulary), 'links': shop, 'hot': hot}

    @staticmethod
    def _coach_vocabulary(rules, company):
        """The catalogue names the coach listens for, or none when the hook
        fails: a failed hook costs the mentions, never the coach list (the
        savepoint as in ``_coach_hook``)."""
        try:
            with rules.env.cr.savepoint():
                return rules._dialer_vocabulary(company)
        except Exception as e:  # noqa: BLE001
            _logger.warning('dialer coach vocabulary failed: %s', type(e).__name__)
            return []

    @staticmethod
    def _coach_hook(read, name):
        """A mark's product set, or none when its hook fails: a failed hook
        costs the mark, never the call's coach list. The savepoint matters: a
        hook that fails inside Postgres (a timeout, a tenant's raw SQL) would
        otherwise leave the transaction aborted and fail every later query."""
        try:
            with request.env.cr.savepoint():
                return read()
        except Exception as e:  # noqa: BLE001
            _logger.warning('dialer coach hook %s failed: %s', name, type(e).__name__)
            return request.env['product.product']

    @staticmethod
    def _coach_hot_candidates(rules, company, state):
        """The state's best sellers to rank, or None when there is no state,
        ranking is off or the hook failed (``_coach_hot`` says which)."""
        if not state or not rules._coach_setting('count'):
            return None
        try:
            with rules.env.cr.savepoint():  # see _coach_hook
                return rules._coach_hot_cached(company, state)
        except Exception as e:  # noqa: BLE001
            _logger.warning('dialer coach hook hot failed: %s', type(e).__name__)
            return None

    @staticmethod
    def _coach_prefetch(rules, partner, ship, pools):
        """Rules primed for the products the coach lists may check: each
        pool's first places x SCAN_FACTOR, as far as the lists scan. A failed
        prefetch costs only speed: the checks then run one product at a time."""
        ids = list(dict.fromkeys(p.id for candidates, places in pools
                                 for p in list(candidates)[:places * SCAN_FACTOR]))
        try:
            with rules.env.cr.savepoint():
                return rules._dialer_coach_prefetch(partner, rules.env['product.product'].browse(ids), ship)
        except Exception as e:  # noqa: BLE001
            _logger.warning('dialer coach prefetch failed: %s', type(e).__name__)
            return rules

    def _coach_hot(self, rules, company, state, line_for, candidates):
        """The "selling in this state" block and {product id: rank}. Ranks are
        1..count among products this customer may buy, at their price. The
        block is display only; the same ranks ride in the signed product list.
        ``candidates``: from ``_coach_hot_candidates`` (None: no ranks)."""
        days = rules._coach_setting('days')
        hot = {'status': 'no_state', 'state': state.code or '', 'days': days, 'items': []}
        if not state:
            return hot, {}
        count = rules._coach_setting('count')
        if not count or candidates is None:
            hot['status'] = 'off'
            return hot, {}
        ranks = {}
        for product in candidates:
            if len(ranks) >= count:
                break
            if line_for(product):
                ranks[product.id] = len(ranks) + 1
        hot['status'] = 'ok' if ranks else 'thin'
        return hot, ranks

    def _shop_links(self, products, company):
        """Each product's own page on the company's website, for the rep to open or send.
        Browser only: the coach never sees these, so it cannot invent a link. Products
        not published on the website get no link; no website app means no links."""
        if 'website_url' not in products._fields or 'website' not in request.env:
            return {}
        default = request.env['website'].sudo().search([('company_id', '=', company.id)], limit=1)
        out = {}
        for product in products:
            tmpl = product.product_tmpl_id
            site = tmpl.website_id or default
            if site and tmpl.is_published and product.website_url:
                out[str(product.id)] = site.get_base_url().rstrip('/') + product.website_url
        return out

    def _coach_line(self, rules, partner, product, ship):
        """One product as the coach reads it, or None when the coach may not
        recommend it: only 'allowed' for this customer, at a real price."""
        availability = rules._dialer_availability(partner, product, ship)
        if availability['status'] != 'allowed':
            return None
        price = rules._dialer_price(partner, product, rules._dialer_moq(product))
        if price.get('price') is None:
            return None
        stock = rules._dialer_stock(partner, product) or {}
        facts = rules._dialer_product_facts(product)
        return {'id': product.id, 'name': product.display_name,
                'code': product.default_code or '', 'price': price['price'],
                'currency': price.get('currency') or '',
                'availability': availability.get('text') or 'allowed',
                'stock': stock.get('free'),
                **{k: str(facts.get(k) or '')[:60] for k in ('category', 'brand', 'ships')}}

    def _coach_pools(self, partner, company, limit=150):
        """Where the coach's products come from, in order, with each pool's
        places: (name, places, products). First what this customer already
        buys, then what the company is putting forward, then the best sellers
        of the last 180 days taken in turn from each category, so one big
        category cannot fill the list. Rules are applied by the caller."""
        # A salesperson sees only their own orders, so as the rep "best sellers"
        # meant "this rep's own sales". Units sold are counted with sudo, in the
        # call's company only; nothing leaves but product ids, read back as the rep.
        Line = partner.env['sale.order.line'].sudo()
        since = fields.Datetime.subtract(fields.Datetime.now(), days=180)
        base = [('state', '=', 'sale'), ('company_id', '=', company.id), ('product_id.sale_ok', '=', True)]
        own = Line._read_group(base + [('order_partner_id', 'child_of', partner.commercial_partner_id.id)],
                               ['product_id'], ['product_uom_qty:sum'],
                               order='product_uom_qty:sum desc', limit=30)
        top = Line._read_group(base + [('order_id.date_order', '>=', since)],
                               ['product_id'], ['product_uom_qty:sum'],
                               order='product_uom_qty:sum desc', limit=limit)
        Product = partner.env['product.product']
        featured = partner.env['ariane.dialer.rules']._dialer_featured_products(partner, company)
        return [('own', OWN_PLACES, list(Product.browse([p.id for p, _qty in own]))),
                ('featured', FEATURED_PLACES, list(featured)),
                ('top', CONTEXT_PRODUCTS, in_turn(list(Product.browse([p.id for p, _qty in top])),
                                                  key=lambda p: p.categ_id.id))]

    @http.route('/ariane_dialer/call/start', type='json', auth='user')
    def call_start(self, phone, partner_id=None, lead_id=None, direction='out', external_call_id=None,
                   preview=False):
        """``external_call_id`` is accepted for v0.1 panels and ignored: only a
        signed push from the dialler service binds a provider call id."""
        self._check_user()
        partner = self._partner(partner_id)
        lead = request.env['crm.lead'].browse(int(lead_id)).exists() if lead_id else request.env['crm.lead']
        if lead:
            lead.check_access('read')
        call = trusted(request.env['ariane.dialer.call']).create({
            'phone': (phone or '')[:32],
            'partner_id': partner.id or False,
            'lead_id': lead.id or False,
            'direction': 'in' if direction == 'in' else 'out',
            'state': 'preview' if preview else 'dialing',
        })
        return {'call_id': call.id}

    @http.route('/ariane_dialer/call/update', type='json', auth='user')
    def call_update(self, call_id, state=None, external_call_id=None):
        self._check_user()
        call = self._own_call(call_id)
        vals = {}
        if state in ('live', 'wrapup') and call.state != 'done':
            vals['state'] = state
        if state == 'wrapup' and not call.ended_at:
            vals['ended_at'] = fields.Datetime.now()
        if vals:
            call.write(vals)
        return True

    @http.route('/ariane_dialer/call/pending', type='json', auth='user')
    def call_pending(self):
        """The rep's OWN newest call that was never wrapped up, so a reloaded
        panel goes back to its wrap-up; None when there is none. A call in
        wrap-up always counts; one still dialling or live (preview included)
        only once it is STALE_CALL_MINUTES old."""
        self._check_user()
        stale = fields.Datetime.subtract(fields.Datetime.now(), minutes=STALE_CALL_MINUTES)
        call = request.env['ariane.dialer.call'].search([
            ('user_id', '=', request.env.uid),
            ('outcome_code', '=', False),
            '|', ('state', '=', 'wrapup'),
            '&', ('state', 'in', ('dialing', 'live', 'preview')), ('started_at', '<=', stale),
        ], order='started_at desc, id desc', limit=1)
        if not call:
            return None
        return {'call_id': call.id, 'phone': call.phone, 'partner_id': call.partner_id.id or False,
                'lead_id': call.lead_id.id or False, 'state': call.state}

    @http.route('/ariane_dialer/ping', type='json', auth='user')
    def ping(self):
        """The panel times this during a call ("API connection"). Reads nothing."""
        self._check_user()
        return {'ok': True}

    @http.route('/ariane_dialer/call/health', type='json', auth='user', max_content_length=HEALTH_BODY_MAX)
    def call_health(self, call_id, summary=None):
        """The panel's call-health summary, stored once per call. Only the
        call's own rep may send it (a manager reads it, never writes it). A
        second send is answered "already_saved" and changes nothing. Values
        are checked and capped in models/call_health.py; raw samples are
        never stored."""
        self._check_user()
        call = self._own_call(call_id)
        call._lock()
        if call.health_saved_at:
            return {'ok': True, 'code': 'already_saved'}
        if not isinstance(summary, dict):
            raise UserError('The call health summary is not readable.')
        call.write({**health_values(summary), 'health_saved_at': fields.Datetime.now()})
        return {'ok': True, 'code': 'saved'}

    @http.route('/ariane_dialer/call/wrapup', type='json', auth='user')
    def call_wrapup(self, call_id, outcome=None, note='', follow_up_at=None, closure_reason='',
                    disposition=None, callback_date=None, expected_request=None):
        """Log the call where the tenant counts work. Runs once per call: a
        retry returns the first result. ``disposition``/``callback_date`` are the
        v0.1 names and still work. ``expected_request`` is the tracked request the
        panel showed (0 = none); the tenant refuses to complete a different one.
        A panel that does not send it (v0.1) is not checked (-1)."""
        self._check_user()
        call = self._own_call(call_id)
        call._lock()
        if call.state == 'done' and call.wrapup_result:
            return call._stored('wrapup_result')
        rules = self._rules()
        chosen = rules._dialer_outcome(outcome or disposition)
        follow_up = self._utc(follow_up_at) or (
            fields.Datetime.to_datetime(f'{fields.Date.to_date(callback_date)} 10:00:00') if callback_date else False)
        if chosen['closes']:
            follow_up = False  # a closed request has no next step
        if chosen['needs_followup'] and not follow_up:
            raise UserError('Pick a follow-up date and time.')
        if follow_up and follow_up <= fields.Datetime.now():
            raise UserError('The follow-up must be in the future.')
        note = (note or '')[:4000]
        closure_reason = (closure_reason or '').strip()[:500] if chosen['closes'] else ''
        if chosen.get('needs_closure_reason') and not closure_reason:
            raise UserError('Say why this is closed.')
        if len(note.strip()) < MIN_NOTE_CHARS:
            raise UserError('Write a note to finish: say what happened on the call.')
        call.write({'disposition': chosen['bucket'], 'outcome_code': chosen['code'],
                    'outcome_label': chosen['label'], 'note': note, 'follow_up_at': follow_up,
                    'closure_reason': closure_reason or False, 'state': 'done',
                    'ended_at': call.ended_at or fields.Datetime.now(),
                    'expected_request': -1 if expected_request is None else int(expected_request or 0)})
        logged = rules._dialer_log_call(call, chosen, note, follow_up, closure_reason) or {}
        dnc = None
        if chosen['bucket'] == 'do_not_call' and 'phone.blacklist' in request.env and call.phone:
            try:
                with request.env.cr.savepoint():
                    request.env['phone.blacklist'].add(call.phone, message='Said on a dialler call')
                dnc = 'blocked'
            except AccessError:
                dnc = 'needs_manager'
        result = {'ok': True, 'do_not_call': dnc, **logged}
        call.wrapup_result = json.dumps(result)
        return result

    @http.route('/ariane_dialer/quote/add', type='json', auth='user')
    def quote_add(self, partner_id, product_id, qty=None, order_id=None, call_id=None, add_key=None):
        """Add one line to a DRAFT quotation. The browser sends product and
        quantity only; Odoo prices the line. Every rule is checked against the
        order itself: its shipping address, its pricelist and its company."""
        self._check_user()
        env = request.env
        rules = self._rules()
        partner = self._partner(partner_id)
        if not partner:
            raise UserError('Pick a customer first.')
        call = self._own_call(call_id) if call_id else env['ariane.dialer.call']
        add_key = str(add_key)[:64] if add_key else ''
        if call:
            call._lock()
            done = call._stored('quote_add_result') or {}
            if add_key and isinstance(done, dict) and add_key in done:
                return done[add_key]
            if call.state == 'done':
                raise UserError('This call is finished. Add to the quotation from the order itself.')
            if call.partner_id and call.partner_id.commercial_partner_id != partner.commercial_partner_id:
                raise UserError('This call is with another customer.')
            if not order_id and call.sale_order_id.state in ('draft', 'sent'):
                # One quotation per call: a second click before the first answer
                # came back must not open a second quotation.
                order_id = call.sale_order_id.id
        product = env['product.product'].browse(int(product_id)).exists()
        if not product or not product.sale_ok:
            raise UserError('This product cannot be sold.')
        moq = rules._dialer_moq(product)
        try:
            qty = float(moq if qty is None else qty)
        except (TypeError, ValueError):
            raise UserError('Quantity out of range.')
        if not 0 < qty <= MAX_QTY:
            raise UserError('Quantity out of range.')
        if qty < moq:
            raise UserError(f'The minimum order for this product is {moq:g}.')
        if order_id:
            order = env['sale.order'].browse(int(order_id)).exists()
            if order:
                order.check_access('write')
                self._lock('sale_order', order.id)
                order.invalidate_recordset()
            if not order or order.state not in ('draft', 'sent') or \
                    order.partner_id.commercial_partner_id != partner.commercial_partner_id or \
                    (call and order.company_id != call.company_id):
                raise UserError('That quotation cannot take new lines.')
            pricelist, ship, company = order.pricelist_id, order.partner_shipping_id, order.company_id
        else:
            order = env['sale.order']
            pricelist = rules._dialer_pricelist(partner)
            ship = rules._dialer_ship_partner(partner)
            company = call.company_id if call else env.company
        if pricelist.company_id and pricelist.company_id != company:
            raise UserError(f'This customer is priced in {pricelist.company_id.name}. Switch company first.')
        # Tenant rules (e.g. a per-company state ban) are asked about the order's company.
        rules = rules.with_company(company)
        availability = rules._dialer_availability(partner, product, ship)
        if availability['status'] not in QUOTABLE:
            raise UserError(availability.get('text') or 'This product cannot be sold to this customer.')
        if rules._dialer_price(partner, product, qty, pricelist=pricelist,
                               date=order.date_order if order else False)['price'] is None:
            raise UserError('This product has no price for this customer. Check before quoting.')
        if not order:
            order = env['sale.order'].with_company(company).create({
                'partner_id': partner.id, 'partner_shipping_id': ship.id, 'user_id': env.uid,
                'company_id': company.id, 'pricelist_id': pricelist.id or False})
        before = order.order_line
        line = env['sale.order.line'].create({'order_id': order.id, 'product_id': product.id,
                                              'product_uom_qty': qty})
        order.invalidate_recordset(['order_line'])
        rules._dialer_check_order_lines(order, order.order_line - before)
        result = {'order_id': order.id, 'order_name': order.name, 'line_price': line.price_unit,
                  'line_subtotal': line.price_subtotal, 'order_total': order.amount_total,
                  'currency': order.currency_id.name}
        if call:
            vals = {'sale_order_id': call.sale_order_id.id or order.id}
            if add_key:
                done = call._stored('quote_add_result')
                done = done if isinstance(done, dict) else {}
                done[add_key] = result
                vals.update(quote_add_key=add_key,
                            quote_add_result=json.dumps(dict(list(done.items())[-QUOTE_KEYS:])))
            call.write(vals)
        return result
