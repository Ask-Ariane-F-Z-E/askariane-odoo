"""The sales rules the dialler applies, as overridable hooks.

Every rule a tenant may want to change lives here, not in the controller.
A tenant module inherits ``ariane.dialer.rules`` and overrides a hook; the
generic answers below are deliberately cautious. All hooks run in the rep's
own environment (no sudo) and are called on the server for every request:
the browser never supplies a price, an availability or a quantity rule.
"""
import logging
import re
import time
import unicodedata
from collections import OrderedDict

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.osv import expression
from odoo.tools import frozendict, plaintext2html

_logger = logging.getLogger(__name__)

# Availability states a hook may return. These three may be quoted. A tenant
# with a compliance engine never answers 'unknown' (ADR-0042 decision 3).
QUOTABLE = ('allowed', 'review', 'unknown')
AVAILABILITY = ('allowed', 'review', 'unknown', 'not_cleared', 'blocked')

# Generic outcomes. ``bucket`` is the cross-tenant disposition stored on the call.
GENERIC_OUTCOMES = [
    {'code': 'interested', 'label': 'Interested', 'bucket': 'interested', 'closes': False, 'needs_followup': False},
    {'code': 'quote_sent', 'label': 'Quote sent', 'bucket': 'quote_sent', 'closes': False, 'needs_followup': False},
    {'code': 'callback', 'label': 'Callback', 'bucket': 'callback', 'closes': False, 'needs_followup': True},
    {'code': 'no_answer', 'label': 'No answer', 'bucket': 'no_answer', 'closes': False, 'needs_followup': False},
    {'code': 'voicemail', 'label': 'Voicemail', 'bucket': 'voicemail', 'closes': False, 'needs_followup': False},
    {'code': 'wrong_number', 'label': 'Wrong number', 'bucket': 'wrong_number', 'closes': True, 'needs_followup': False},
    {'code': 'do_not_call', 'label': 'Do not call', 'bucket': 'do_not_call', 'closes': True, 'needs_followup': False},
    {'code': 'not_qualified', 'label': 'Not qualified', 'bucket': 'not_qualified', 'closes': True, 'needs_followup': False},
]


# What the rep can say while the coach or a search is still working.
DEFAULT_FILLERS = ("Give me a moment to check that for you.", "Let me pull up those details.",
                   "That's a good question, let me confirm.")
FILLER_MAX = 10
FILLER_LEN = 80

# The coach's slow lists, cached per worker process. Keys hold a generation
# number kept in a system parameter, so invalidating a list by name reaches
# every worker (Odoo clears its parameter cache across workers on a write).
# Only product ids are stored, never records or environments.
_CACHE = OrderedDict()
CACHE_MAX = 256
CACHE_TTL = {'hot_by_state': 900, 'new_sellers': 3600}

# "Hot in the customer's state" settings: (parameter, default, lowest, highest).
HOT_SETTINGS = {
    'days': ('ariane_dialer.hot_days', 30, 7, 180),
    'count': ('ariane_dialer.hot_count', 5, 0, 10),
    'min_orders': ('ariane_dialer.hot_min_orders', 3, 1, 1000),
}
# A hot product was bought by at least this many different customers: the strip
# says "Selling in TX", which one shop reordering does not make true.
HOT_MIN_BUYERS = 2
NEW_SELLER_LIMIT = 200
# The names the live coach listens for (brands, vendors, categories of what the
# company sells), signed with the coach list. Bounded: the whole list rides in
# the signed context, and the speech model's hint holds ~224 tokens at most.
VOCAB_MAX = 400
# A name reaches the speech model's hint and the coach's prompt: name
# characters only, a few words, or it is dropped whole (never trimmed).
VOCAB_TERM_LEN = 40
VOCAB_TERM_WORDS = 6
VOCAB_NAME_CHARS = frozenset(" &'-./")
VOCAB_KINDS = ('brand', 'vendor', 'category')
# A brand or vendor carries its products' biggest category leaf names, so the
# coach can tell "loo more candles" (Lumora) from "loo more" said in passing.
VOCAB_CONTEXT = 3
# A name that carries an instruction word, or a number next to a price word,
# reads as an order to the coach ("Lumora ignore rules", "Lumora 5 dollars"):
# dropped whole. Same lists as the coach's own check (arjun-os mentions.py).
VOCAB_INSTRUCTION_WORDS = frozenset('ignore rule rules instruction instructions prompt system assistant say quote '
                                    'tell must never always'.split())
VOCAB_PRICE_WORDS = frozenset('price priced pricing cost costs usd eur gbp dollar dollars buck bucks euro euros '
                              'cent cents apiece'.split())
VOCAB_NUMBER_WORDS = frozenset('zero one two three four five six seven eight nine ten eleven twelve thirteen '
                               'fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty '
                               'sixty seventy eighty ninety hundred thousand million dozen'.split())
# The kinds of name the coach hears; a mention of one ranks that kind's match first.
MENTION_KINDS = ('brand', 'vendor', 'category')


def reads_as_instruction(term):
    """True when a name carries an instruction word, or a number beside a price word."""
    words = re.findall(r'\d+|[^\W\d_]+', term.casefold())     # "usd5" is "usd" beside "5"
    if any(w in VOCAB_INSTRUCTION_WORDS for w in words):
        return True
    for i, w in enumerate(words):
        if (w.isdigit() or w in VOCAB_NUMBER_WORDS) and any(
                n in VOCAB_PRICE_WORDS for n in words[max(0, i - 1):i + 2]):
            return True
    return False


def whole_word(query):
    """A matcher for the query as a whole word or words ("Raw" in "Raw Cones",
    never in "Strawberry")."""
    return re.compile(r'(?<![^\W_])' + re.escape(' '.join(query.casefold().split())) + r'(?![^\W_])')


# What may stand either side of a whole word in a name ("Raw Cones", "Raw-Cones", "(Raw)").
WORD_BEFORE = ' -/('
WORD_AFTER = " -/),.'"


def whole_word_domain(field, query):
    """A domain for ``field`` holding the query as a whole word or words, run
    in SQL (=ilike patterns), so a page of "Strawberry" never hides "Raw".
    [] for an empty query (never "every record"). One plain contains-match
    goes first: Postgres runs it before the 40 patterns, so only rows that hold
    the text at all are checked word by word (100k rows: ~1,980 ms -> ~55 ms)."""
    words = ' '.join((query or '').split())
    if not words:
        return []
    q = re.sub(r'([\\%_])', r'\\\1', words)
    patterns = [q] + [q + a + '%' for a in WORD_AFTER] + ['%' + b + q for b in WORD_BEFORE] + [
        '%' + b + q + a + '%' for b in WORD_BEFORE for a in WORD_AFTER]
    return expression.AND([[(field, '=ilike', '%' + q + '%')],
                           expression.OR([[(field, '=ilike', p)] for p in patterns])])


def in_turn(items, key):
    """Items taken one from each group in turn, each group keeping its order:
    [a1, a2, a3, b1] by letter -> [a1, b1, a2, a3]."""
    groups = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    out, queues = [], list(groups.values())
    while queues:
        out += [q.pop(0) for q in queues]
        queues = [q for q in queues if q]
    return out


class ArianeDialerRules(models.AbstractModel):
    _name = 'ariane.dialer.rules'
    _description = 'Dialler sales rules (tenant hooks)'

    # ------------------------------------------------------------ settings
    @api.model
    def _param(self, key, default=''):
        return self.env['ir.config_parameter'].sudo().get_param(key, default) or default

    @api.model
    def _blocked_prices(self):
        """Exact unit prices that mean 'do not quote' (e.g. 0 and 1 placeholders)."""
        out = set()
        for part in self._param('ariane_dialer.price_block_values').split(','):
            try:
                out.add(round(float(part.strip()), 2))
            except ValueError:
                continue
        return out

    @api.model
    def _block_at(self):
        """Prices at or above this mean 'not sold here' (e.g. a 1,000,000 marker). 0 = off."""
        try:
            return float(self._param('ariane_dialer.price_block_at', '0') or 0)
        except ValueError:
            return 0.0

    @api.model
    def _is_marker_price(self, price):
        at = self._block_at()
        return round(price, 2) in self._blocked_prices() or (at and price >= at)

    # ------------------------------------------------------------ customer
    @api.model
    def _dialer_customer(self, partner):
        """Who we are selling to. ``commercial`` prices and owns the history;
        ``state`` is where the goods go."""
        commercial = partner.commercial_partner_id
        return {
            'commercial': commercial,
            'pricelist': commercial.property_product_pricelist,
            'state': partner.state_id or commercial.state_id,
            'tier': '',
            'warnings': [],
        }

    # ------------------------------------------------------------ price
    @api.model
    def _dialer_pricelist(self, partner):
        """The pricelist a quote for this customer must use. The card and the
        order both use this one, so what the rep says is what the order says."""
        return self._dialer_customer(partner)['pricelist'] if partner else self.env['product.pricelist']

    @api.model
    def _dialer_price(self, partner, product, qty=1.0, pricelist=None, date=False):
        """{'price', 'price_status': 'ok', 'currency', 'pricelist', 'rule_id',
        'from_list'} or {'price': None, 'price_status', 'price_note'}.
        ``date`` is the order's date when there is an order: Odoo prices the line
        on that date, so the check must too."""
        pricelist = pricelist if pricelist is not None else self._dialer_pricelist(partner)
        if not pricelist:
            return {'price': None, 'price_status': 'no_pricelist', 'price_note': 'No pricelist on this customer'}
        qty = qty or 1.0
        known = self._dialer_known_price(pricelist, product, qty, date)
        if known:
            price, rule_id, from_list = known
        else:
            price, rule_id = pricelist._get_product_price_rule(product, qty, date=date or False)
            from_list = self._price_from_list(pricelist, product, qty, date, rule_id=rule_id)
        # A marker on the product itself can be discounted by the customer's
        # pricelist (1,000,000 less 10% = 900,000), so a price that comes from the
        # list price is blocked when the list price is a marker. A fixed rule
        # price is the customer's real price and is kept.
        if self._is_marker_price(price) or (from_list and self._is_marker_price(product.lst_price)):
            return {'price': None, 'price_status': 'blocked', 'price_note': 'Check price before quoting'}
        return {'price': price, 'price_status': 'ok', 'currency': pricelist.currency_id.name,
                'pricelist': pricelist.display_name, 'rule_id': rule_id or False, 'from_list': from_list}

    @api.model
    def _price_from_list(self, pricelist, product, qty, date=False, depth=5, rule_id=None):
        """True when the winning price is the product's list price, as is or
        adjusted: no rule matched, or the rule chain ends on the list price.
        False when a fixed price set it. ``rule_id``: the pricelist's winning
        rule when the caller already has it (False: none matched)."""
        rules = None if rule_id is None else {product.id: rule_id}
        return self._price_from_list_many(pricelist, product, qty, date, depth, rules)[product.id]

    @api.model
    def _price_from_list_many(self, pricelist, products, qty, date=False, depth=5, rules=None):
        """``_price_from_list`` for many products at one quantity: {product id:
        bool}. One rule pass per pricelist in the chain, not one per product.
        ``rules``: {product id: winning rule id} on ``pricelist``, when known."""
        if rules is None:
            found = pricelist._compute_price_rule(products, qty, date=date or False, compute_price=False)
            rules = {pid: rule_id for pid, (_price, rule_id) in found.items()}
        Item = self.env['product.pricelist.item']
        out, deeper = {}, {}
        for product in products:
            rule = Item.browse(rules[product.id] or [])
            if not rule:
                out[product.id] = True
            elif rule.compute_price == 'fixed':
                out[product.id] = False
            elif rule.base == 'pricelist' and rule.base_pricelist_id and depth:
                deeper.setdefault(rule.base_pricelist_id, []).append(product.id)
            else:
                out[product.id] = rule.base == 'list_price'
        for base, ids in deeper.items():
            out.update(self._price_from_list_many(base, products.browse(ids), qty, date, depth - 1))
        return out

    # ------------------------------------------------------------ coach batch
    @api.model
    def _dialer_coach_prefetch(self, partner, products, ship=None):
        """These rules, set up to answer the coach's per-product checks for
        ``products`` from batched reads. The answers are the same; only the
        number of queries changes. Generic: each product's price at its minimum
        order, from one pass of Odoo's own price engine per quantity
        (``_get_product_price_rule`` is that engine for one product). A tenant
        adds its own (availability). Anything not prefetched is checked as before."""
        pricelist = self._dialer_pricelist(partner)
        if not pricelist or not products:
            return self
        by_qty = {}
        for product in products:
            by_qty.setdefault(float(self._dialer_moq(product) or 1.0), []).append(product.id)
        known = {}
        for qty, ids in by_qty.items():
            batch = self.env['product.product'].browse(ids)
            found = pricelist._compute_price_rule(batch, qty)
            from_list = self._price_from_list_many(
                pricelist, batch, qty, rules={pid: rule_id for pid, (_price, rule_id) in found.items()})
            for pid, (price, rule_id) in found.items():
                known[pid] = (qty, price, rule_id or False, from_list[pid])
        return self.with_context(ariane_dialer_known_prices=(pricelist.id, frozendict(known)))

    @api.model
    def _dialer_known_price(self, pricelist, product, qty, date=False):
        """(price, rule id, from_list) from ``_dialer_coach_prefetch``, or None
        when this exact question (pricelist, product, quantity, today) was not
        prefetched."""
        known = self.env.context.get('ariane_dialer_known_prices')
        if not known or date or known[0] != pricelist.id:
            return None
        entry = known[1].get(product.id)
        if not entry or entry[0] != float(qty):
            return None
        return entry[1:]

    @api.model
    def _pricelist_chain(self, pricelist, depth=5):
        """The pricelist and every pricelist its rules are based on."""
        seen = self.env['product.pricelist']
        todo = pricelist
        while todo and depth:
            seen |= todo
            todo = todo.item_ids.filtered(lambda i: i.base == 'pricelist').base_pricelist_id - seen
            depth -= 1
        return seen

    @api.model
    def _dialer_breaks(self, partner, product):
        """Candidate quantities come from the whole rule chain; Odoo's own engine
        prices each one, so precedence, dates, variants and categories are Odoo's."""
        pricelist = self._dialer_pricelist(partner)
        if not pricelist:
            return []
        items = self.env['product.pricelist.item'].search([
            ('pricelist_id', 'in', self._pricelist_chain(pricelist).ids),
            ('min_quantity', '>', 1),
            '|', '|', '|',
            ('applied_on', '=', '3_global'),
            ('product_id', '=', product.id),
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
            ('categ_id', 'parent_of', product.categ_id.id),
        ], limit=50)
        return self._price_breaks(partner, product, sorted({i.min_quantity for i in items}))

    @api.model
    def _price_breaks(self, partner, product, quantities):
        """Price each break quantity through the full rule chain; drop markers and
        breaks that are not cheaper than one unit."""
        base = self._dialer_price(partner, product, 1.0)
        if base.get('price') is None:
            return []
        out = []
        for qty in quantities[:8]:
            p = self._dialer_price(partner, product, qty)
            if p.get('price') is not None and p['price'] < base['price']:
                out.append({'min_qty': qty, 'price': p['price']})
        return out

    # ------------------------------------------------------------ availability
    @api.model
    def _dialer_ship_partner(self, partner):
        """Where the goods would go: the customer's delivery address, else the customer."""
        if not partner or partner.type == 'delivery':
            return partner
        found = partner.commercial_partner_id.address_get(['delivery']).get('delivery')
        return self.env['res.partner'].browse(found) if found else partner

    @api.model
    def _dialer_availability(self, partner, product, ship_partner=None):
        """May this product be sold for delivery to ship_partner (default: the
        customer's delivery address)? {'status': one of AVAILABILITY, 'text'}.
        The generic module has no compliance engine, so it says 'unknown' and
        the panel shows "check before pitching". A tenant with an engine must
        never answer 'allowed' without a positive rule."""
        return {'status': 'unknown', 'text': 'Check compliance before pitching'}

    # ------------------------------------------------------------ call list
    @api.model
    def _dialer_queue(self, company, limit=50):
        """This rep's calls due today or overdue, earliest first, one row per
        customer: the power dialler's list. Generic: the rep's own Call
        activities on contacts and leads. Only the rep's own work is listed."""
        today = fields.Date.context_today(self)
        domain = [
            ('user_id', '=', self.env.uid), ('activity_type_id.category', '=', 'phonecall'),
            ('res_model', 'in', ('res.partner', 'crm.lead')), ('date_deadline', '<=', today),
        ]
        rows, seen = [], set()
        for activity, record in self._dialer_paged(domain, 'date_deadline, id', limit):
            # a contact or lead filed in another company is not this company's call
            if not record or (record.company_id and record.company_id != company):
                continue
            partner = record if record._name == 'res.partner' else record.partner_id
            lead = record if record._name == 'crm.lead' else self.env['crm.lead']
            who = ('partner', partner.commercial_partner_id.id) if partner else ('lead', lead.id)
            if who in seen:
                continue
            seen.add(who)
            rows.append(self._dialer_queue_row(
                partner, lead, activity.summary or activity.activity_type_id.name,
                fields.Date.to_string(activity.date_deadline), activity.date_deadline < today,
                f'mail.activity,{activity.id}'))
            if len(rows) >= limit:
                break
        return rows

    @api.model
    def _dialer_paged(self, domain, order, limit, pages=5, keep=None):
        """(activity, the record it is on, if the rep may read it) for the rep's
        activities matching domain, page by page, so a customer with many due
        calls cannot crowd the others off a list of ``limit`` rows. Each page's
        records are read together, not one by one. The walk stops after
        ``pages`` pages (10 x limit activities), so one customer with more due
        calls than that can still fill the list; that is the price of a bound.

        ``keep`` (activities -> the ones to keep, in order) is a filter a domain
        cannot say cheaply. The pages are then the pages of the KEPT activities,
        exactly as if the filter were in the domain: a stretch where nothing is
        kept never ends the walk early."""
        size = limit * 2
        if keep is None:
            Activity = self.env['mail.activity']
            batches = (Activity.search(domain, order=order, offset=page * size, limit=size)
                       for page in range(pages))
        else:
            batches = self._dialer_kept_pages(domain, order, size, pages, keep)
        for batch in batches:
            readable = {}
            for model in set(batch.mapped('res_model')):
                ids = {a.res_id for a in batch if a.res_model == model}
                for record in self.env[model].browse(ids).exists()._filtered_access('read'):
                    readable[(model, record.id)] = record
            for activity in batch:
                yield activity, readable.get((activity.res_model, activity.res_id), self.env[activity.res_model])
            if len(batch) < size:
                return

    @api.model
    def _dialer_kept_pages(self, domain, order, size, pages, keep):
        """Up to ``pages`` pages of ``size`` of the activities matching domain
        that ``keep`` keeps, in order. One search, then ``keep`` is asked a
        chunk at a time and only as far as the pages need. The last page is
        short (maybe empty) when the activities run out."""
        found = self.env['mail.activity'].search(domain, order=order)
        page = found.browse()
        for start in range(0, len(found), size):
            page += keep(found[start:start + size])
            while len(page) >= size:
                yield page[:size]
                page = page[size:]
                pages -= 1
                if not pages:
                    return
        yield page

    @api.model
    def _dialer_queue_row(self, partner, lead, reason, due, overdue, key, request_id=0):
        """One row of the call list. ``due`` is an ISO date or UTC datetime."""
        named = partner or lead
        phone = (partner.phone or partner.mobile or partner.commercial_partner_id.phone) if partner else \
            (lead.phone or lead.mobile)
        return {
            'key': key, 'partner_id': partner.id or False, 'lead_id': lead.id or False,
            'name': named.display_name, 'phone': phone or '', 'reason': reason or '',
            'due': due, 'overdue': bool(overdue), 'request_id': request_id,
        }

    # ------------------------------------------------------------ live coach
    @api.model
    def _dialer_featured_products(self, partner, company, limit=120):
        """Products the company wants the coach to put forward (a new range,
        other brands), best first. They get their own share of the coach's
        list, so the biggest sellers cannot take every place. Generic: none."""
        return self.env['product.product']

    @api.model
    def _dialer_clearance_products(self, company, limit=60):
        """Products a sales manager flagged as clearance (push them now), for
        this company, flag not run out. Read with sudo (reps cannot read the
        flag); only ids leave, read back as the rep. Newest flag first."""
        today = fields.Date.context_today(self)
        found = self.env['product.product'].sudo().search([
            ('product_tmpl_id.dialer_clearance', '=', True),
            '|', ('product_tmpl_id.dialer_clearance_until', '=', False),
            ('product_tmpl_id.dialer_clearance_until', '>=', today),
            ('sale_ok', '=', True), ('company_id', 'in', [False, company.id]),
        ], order='id desc', limit=limit)
        return self.env['product.product'].browse(found.ids)

    @api.model
    def _dialer_new_seller_products(self, company, limit=NEW_SELLER_LIMIT):
        """Products of sellers who joined recently, newest first. A tenant with
        a marketplace overrides this. Generic: none."""
        return self.env['product.product']

    @api.model
    def _dialer_hot_products(self, company, state, days, limit, min_orders=3):
        """Best sellers to shops shipping to ``state`` in the last ``days`` days,
        in this company only, best first, before any per-customer check.
        Confirmed orders only. A product needs ``min_orders`` separate orders
        AND at least HOT_MIN_BUYERS different buyers, so one shop reordering
        cannot make a product "selling to shops in the state"; ranked by how
        many buyers, then units. Counted with sudo (a salesperson sees only
        their own orders); only ids leave, read back as the rep."""
        since = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        rows = self.env['sale.order.line'].sudo()._read_group(
            [('state', '=', 'sale'), ('company_id', '=', company.id),
             ('order_id.date_order', '>=', since),
             ('order_id.partner_shipping_id.state_id', '=', state.id),
             ('product_id.sale_ok', '=', True)],
            ['product_id'], ['order_partner_id:count_distinct', 'product_uom_qty:sum'],
            having=[('order_id:count_distinct', '>=', min_orders),
                    ('order_partner_id:count_distinct', '>=', HOT_MIN_BUYERS)],
            order='order_partner_id:count_distinct desc, product_uom_qty:sum desc, product_id',
            limit=limit)
        return self.env['product.product'].browse([product.id for product, _buyers, _qty in rows])

    @api.model
    def _dialer_fillers(self):
        """The filler lines for the rep, from Settings: one per line (or split by
        |), at most 10, each at most 80 characters. A line with a digit or a
        currency sign is dropped, so a filler can never carry a price or a
        count. Nothing usable: the built-in lines."""
        raw = self._param('ariane_dialer.filler_phrases')
        out = []
        for line in raw.replace('|', '\n').splitlines():
            line = ' '.join(line.split())
            if not line or len(line) > FILLER_LEN or line in out:
                continue
            if any(c.isdigit() or unicodedata.category(c) == 'Sc' for c in line):
                continue
            out.append(line)
        return out[:FILLER_MAX] or list(DEFAULT_FILLERS)

    # ------------------------------------------------------------ coach lists, cached
    @api.model
    def _dialer_cache_key(self, name, key):
        gen = self._param(f'ariane_dialer.cache_gen.{name}', '0')
        return (name, self.env.cr.dbname, self._param('database.uuid'), gen) + tuple(key)

    @api.model
    def _dialer_cache_get(self, name, key):
        """The cached ids under name/key, or None."""
        full = self._dialer_cache_key(name, key)
        hit = _CACHE.get(full)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        _CACHE.pop(full, None)
        return None

    @api.model
    def _dialer_cache_put(self, name, key, ids):
        _CACHE[self._dialer_cache_key(name, key)] = (time.monotonic() + CACHE_TTL.get(name, 900), tuple(ids))
        while len(_CACHE) > CACHE_MAX:
            _CACHE.popitem(last=False)

    @api.model
    def _dialer_cache_invalidate(self, name):
        """Every worker recomputes the named list on its next read."""
        icp = self.env['ir.config_parameter'].sudo()
        key = f'ariane_dialer.cache_gen.{name}'
        try:
            gen = int(icp.get_param(key, '0') or 0)
        except ValueError:
            gen = 0
        icp.set_param(key, str(gen + 1))

    @api.model
    def _coach_setting(self, name):
        key, default, low, high = HOT_SETTINGS[name]
        try:
            value = int(self._param(key, str(default)))
        except ValueError:
            value = default
        return max(low, min(high, value))

    @api.model
    def _coach_hot_cached(self, company, state):
        """The hook's hot list for this company and state, through the cache,
        long enough that the per-customer checks still leave ``count`` places.
        Read as the rep."""
        days, count, min_orders = (self._coach_setting(n) for n in ('days', 'count', 'min_orders'))
        key = (company.id, state.id, days, min_orders, count)
        ids = self._dialer_cache_get('hot_by_state', key)
        if ids is None:
            started = time.monotonic()
            ids = self._dialer_hot_products(company, state, days, max(count, 1) * 3, min_orders).ids
            self._dialer_cache_put('hot_by_state', key, ids)
            _logger.info('dialer cache MISS name=hot_by_state company=%s ids=%s ms=%d',
                         company.id, len(ids), (time.monotonic() - started) * 1000)
        return self.env['product.product'].browse(ids)

    @api.model
    def _coach_new_sellers_cached(self, company):
        key = (company.id,)
        ids = self._dialer_cache_get('new_sellers', key)
        if ids is None:
            started = time.monotonic()
            ids = self._dialer_new_seller_products(company, NEW_SELLER_LIMIT).ids
            self._dialer_cache_put('new_sellers', key, ids)
            _logger.info('dialer cache MISS name=new_sellers company=%s ids=%s ms=%d',
                         company.id, len(ids), (time.monotonic() - started) * 1000)
        return self.env['product.product'].browse(ids)

    # ------------------------------------------------------------ catalogue vocabulary
    @api.model
    def _dialer_name_domain(self, query):
        """A product named or coded like the query: what the rep types, ranked first."""
        return ['|', ('name', 'ilike', query), ('default_code', 'ilike', query)]

    @api.model
    def _dialer_category_domain(self, query):
        """A product whose category is named like the query."""
        return [('categ_id.name', 'ilike', query)]

    @api.model
    def _dialer_owner_domain(self, query, whole=False):
        """A product whose brand or vendor is named like the query (as a whole
        word when ``whole``). Generic Odoo has no brand field: a tenant adds
        its own. [] means none (never "every product")."""
        return []

    @api.model
    def _dialer_search_domain(self, query):
        """Every product.product the catalogue search may match: its name or
        code, its category's name, or its brand or vendor
        (``_dialer_owner_domain``). Searched as the rep, so a name the rep
        cannot read matches nothing."""
        parts = [self._dialer_name_domain(query), self._dialer_category_domain(query),
                 self._dialer_owner_domain(query)]
        return expression.OR([d for d in parts if d])

    @api.model
    def _dialer_search_tiers(self, query, kind=''):
        """The catalogue search in order: [(domain, whole_word)], each run only
        while the ones before leave room; a product already seen is skipped.
        What the rep types: name or code first, then the rest. A name the coach
        heard (``kind`` brand, vendor or category): that kind's own match
        first, then products named with it, then the rest, each as a whole
        word in SQL, so "Raw" the brand finds Raw's products, never
        "Strawberry" or a "Straws" category, however many sort first."""
        named = self._dialer_name_domain(query)
        if kind not in MENTION_KINDS:
            return [(named, False), (expression.AND([self._dialer_search_domain(query), ['!', *named]]), False)]
        owner = self._dialer_owner_domain(query, whole=True)
        categ = whole_word_domain('categ_id.name', query)
        first, last = (categ, owner) if kind == 'category' else (owner, categ)
        tiers = [(first, False), (whole_word_domain('name', query), True), (last, False)]
        return [(d, whole) for d, whole in tiers if d]

    @api.model
    def _dialer_vocabulary_domain(self, company):
        """The product templates whose names the coach listens for: for sale,
        in this company (or shared). Searched as the rep, so record rules apply."""
        return [('sale_ok', '=', True), ('company_id', 'in', [False, company.id])]

    @api.model
    def _dialer_vocabulary_brands(self, company, domain):
        """[(brand name, product count)], biggest first. Generic: no brand field."""
        return []

    @api.model
    def _dialer_vocabulary_vendors(self, company, domain):
        """[(vendor name, product count)], biggest first: the partners on the
        products' vendor lines (product.supplierinfo), read as the rep. Only the
        name leaves; a vendor the rep cannot read is left out."""
        try:
            rows = self.env['product.supplierinfo']._read_group(
                [('product_tmpl_id', 'any', domain), ('company_id', 'in', [False, company.id])],
                ['partner_id'], ['product_tmpl_id:count_distinct'])
        except AccessError:
            return []
        readable = self.env['res.partner'].browse([p.id for p, _n in rows if p])._filtered_access('read')
        out = {}
        for partner, count in rows:
            if partner and partner in readable:
                name = partner.commercial_company_name or partner.name or ''
                out[name] = out.get(name, 0) + count
        return sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))

    @api.model
    def _dialer_vocabulary_categories(self, company, domain):
        """[(category leaf name, product count)], biggest first, read as the rep."""
        try:
            rows = self.env['product.template']._read_group(domain, ['categ_id'], ['__count'])
        except AccessError:
            return []
        readable = self.env['product.category'].browse([c.id for c, _n in rows if c])._filtered_access('read')
        out = {}
        for categ, count in rows:
            if categ and categ in readable and categ.name:
                out[categ.name] = out.get(categ.name, 0) + count
        return sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))

    @api.model
    def _dialer_vocabulary_context(self, company, domain):
        """{(kind, name casefolded): [category leaf names]} for brands and
        vendors: the categories of their products, biggest first. Generic: the
        vendor lines' partners. One grouped read (one row per vendor and
        product) and one batched category read, as the rep."""
        try:
            rows = self.env['product.supplierinfo']._read_group(
                [('product_tmpl_id', 'any', domain), ('company_id', 'in', [False, company.id])],
                ['partner_id', 'product_tmpl_id'], [])
        except AccessError:
            return {}
        rows = [(p, t) for p, t in rows if p and t]
        self.env['product.template'].browse({t.id for _p, t in rows}).mapped('categ_id')   # one read
        return self._dialer_context_rows(
            [('vendor', p.commercial_company_name or p.name, p, t.categ_id, 1) for p, t in rows],
            self.env['res.partner'])

    @api.model
    def _dialer_context_rows(self, rows, owners):
        """[(kind, name, owner, category, count)] -> {(kind, name casefolded):
        [leaf names, biggest first]}; an owner or category the rep cannot read
        is left out."""
        readable = owners.browse({r[2].id for r in rows})._filtered_access('read')
        categs = self.env['product.category'].browse({r[3].id for r in rows if r[3]})._filtered_access('read')
        counts = {}
        for kind, name, owner, categ, count in rows:
            if owner in readable and categ and categ in categs and name and categ.name:
                per = counts.setdefault((kind, ' '.join(name.split()).casefold()), {})
                per[categ.name] = per.get(categ.name, 0) + count
        return {k: [n for n, _c in sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))]
                for k, per in counts.items()}

    @api.model
    def _dialer_vocabulary_term(self, name):
        """The name as the coach may hear and repeat it, or '' to drop it:
        letters, digits, spaces and & ' - . / only; at most VOCAB_TERM_WORDS
        words and VOCAB_TERM_LEN characters; no instruction word and no number
        beside a price word (``reads_as_instruction``)."""
        term = ' '.join(str(name or '').split())
        if (not term or len(term) > VOCAB_TERM_LEN or len(term.split()) > VOCAB_TERM_WORDS
                or not any(ch.isalnum() for ch in term)
                or not all(ch.isalnum() or ch in VOCAB_NAME_CHARS for ch in term)
                or reads_as_instruction(term)):
            return ''
        return term

    @api.model
    def _dialer_vocabulary(self, company, limit=VOCAB_MAX):
        """The names the live coach listens for on this company's calls:
        [{'term', 'kind'}], brands first, then vendors, then categories, each
        biggest first; a brand or vendor also has 'context', its products' main
        category names. Names only: no price, no customer, nothing the rep
        cannot read. Same name twice (any case) is kept once, under its first
        kind. A name that is not a plain name is dropped
        (``_dialer_vocabulary_term``). Bounded by ``limit``; what is dropped is
        logged by kind."""
        domain = self._dialer_vocabulary_domain(company)
        found = {'brand': self._dialer_vocabulary_brands(company, domain),
                 'vendor': self._dialer_vocabulary_vendors(company, domain),
                 'category': self._dialer_vocabulary_categories(company, domain)}
        context = self._dialer_vocabulary_context(company, domain)
        out, seen = [], set()
        dropped = {kind: 0 for kind in VOCAB_KINDS}
        for kind in VOCAB_KINDS:
            for name, _count in found[kind]:
                raw = ' '.join(str(name or '').split())
                if not raw or raw.casefold() in seen:
                    continue
                term = self._dialer_vocabulary_term(raw)
                if not term or len(out) >= limit:
                    dropped[kind] += 1
                    continue
                seen.add(term.casefold())
                item = {'term': term, 'kind': kind}
                if kind != 'category':
                    words = [self._dialer_vocabulary_term(c) for c in context.get((kind, term.casefold()), ())]
                    item['context'] = [c for c in words if c][:VOCAB_CONTEXT]
                out.append(item)
        if any(dropped.values()):
            _logger.info('dialer vocabulary company=%s kept=%d dropped brand=%d vendor=%d category=%d',
                         company.id, len(out), dropped['brand'], dropped['vendor'], dropped['category'])
        return out

    @api.model
    def _dialer_product_facts(self, product):
        """Short facts the coach reads beside each product, so it can match a
        need it hears to a product: {'category', 'brand', 'ships'}."""
        return {'category': product.categ_id.name or '', 'brand': '', 'ships': ''}

    # ------------------------------------------------------------ stock / moq
    @api.model
    def _dialer_stock(self, partner, product):
        try:
            return {'free': product.free_qty, 'on_hand': product.qty_available,
                    'uom': product.uom_id.name, 'warehouses': []}
        except AccessError:
            return None

    @api.model
    def _dialer_moq(self, product):
        return 1.0

    # ------------------------------------------------------------ outcomes / log
    @api.model
    def _dialer_outcomes(self):
        return [dict(o) for o in GENERIC_OUTCOMES]

    @api.model
    def _dialer_outcome(self, code):
        for o in self._dialer_outcomes():
            if o['code'] == code:
                return o
        raise UserError('Unknown outcome.')

    @api.model
    def _dialer_open_request(self, partner):
        """The tracked request this rep's call will complete, shown before the call
        is saved: {'id', 'summary', 'due'} or None. Generic: none."""
        return None

    @api.model
    def _dialer_check_order_lines(self, order, new_lines):
        """Re-check lines the add put on the order, including lines the tenant's
        own hooks added (e.g. an automatic accessory), as Odoo priced them.
        Raises, so the whole add rolls back."""
        ship = order.partner_shipping_id
        for line in new_lines.filtered(lambda l: l.product_id and l.product_id.type != 'service'):
            status = self._dialer_availability(order.partner_id, line.product_id, ship)
            if status['status'] not in QUOTABLE:
                raise UserError(f'{line.product_id.display_name}: '
                                f"{status.get('text') or 'cannot be sold to this customer.'}")
            if self._is_marker_price(line.price_unit):
                raise UserError(f'{line.product_id.display_name} has no price for this customer. '
                                'Check before quoting.')

    @api.model
    def _dialer_log_call(self, call, outcome, note='', follow_up_at=False, closure_reason=''):
        """Record the call where this tenant counts work. Generic: one chatter
        note, plus a Call activity when a follow-up is set."""
        target = call.lead_id or call.partner_id
        if not target:
            return {}
        mins = f' · {call.duration // 60}m{call.duration % 60:02d}s' if call.duration else ''
        phone = f' ({call.phone})' if call.phone else ''
        body = f"Call: {outcome['label']}{mins}{phone}"
        if closure_reason:
            body += f'\nReason: {closure_reason}'
        if note:
            body += f'\n{note}'
        target.message_post(body=plaintext2html(body), message_type='comment', subtype_xmlid='mail.mt_note')
        if follow_up_at:
            target.activity_schedule('mail.mail_activity_data_call',
                                     date_deadline=fields.Datetime.to_datetime(follow_up_at).date(),
                                     summary='Call back', user_id=self.env.uid)
        return {'logged_on': f'{target._name},{target.id}'}
