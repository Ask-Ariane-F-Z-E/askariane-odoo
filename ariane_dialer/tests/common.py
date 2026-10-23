"""Shared set-up for the generic dialler tests.

These tests check the generic contract. On a tenant's database a tenant module
overrides the rule hooks (its own outcomes, its own availability), so each test
puts the generic hooks back for its own duration. The tenant's own suite tests
the tenant's hooks against its real modules.
"""
from contextlib import contextmanager
from unittest.mock import patch

from odoo.sql_db import Cursor

from ..models import dialer_rules
from ..models.dialer_rules import ArianeDialerRules

GENERIC_HOOKS = [name for name, value in vars(ArianeDialerRules).items()
                 if name.startswith('_dialer_') and callable(value)] + ['_price_from_list']


def new_company(env, name):
    """A second company that any tenant database accepts. Some tenants put every
    new contact in the current company, the company's own contact included, and
    the new company then fails its own company check. A contact with no company
    is valid everywhere."""
    partner = env['res.partner'].create({'name': name})
    partner.company_id = False
    return env['res.company'].create({'name': name, 'partner_id': partner.id})


class GenericRulesMixin:

    def setUp(self):
        super().setUp()
        Rules = self.env.registry['ariane.dialer.rules']
        for name in GENERIC_HOOKS:
            patcher = patch.object(Rules, name, getattr(ArianeDialerRules, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        clear_dialer_cache()


def clear_dialer_cache():
    """The coach's lists are cached per worker, outside the database. A test's
    rows are rolled back after it, so a list cached in one test must not be
    served to the next."""
    getattr(dialer_rules, '_CACHE', {}).clear()


@contextmanager
def queries_on(*tables):
    """Counts the SQL statements run inside the block that name each table,
    and all of them ('all'), in every thread (an HTTP route runs in its own)."""
    counts = dict.fromkeys(tables, 0)
    counts['all'] = 0
    real = Cursor.execute

    def execute(cr, query, params=None, log_exceptions=True):
        text = getattr(query, 'code', query)
        counts['all'] += 1
        for table in tables:
            if table in text:
                counts[table] += 1
        return real(cr, query, params, log_exceptions)

    with patch.object(Cursor, 'execute', execute):
        yield counts
