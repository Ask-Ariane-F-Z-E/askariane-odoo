from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ariane_dialer_api_url = fields.Char(
        'Dialler service URL', config_parameter='ariane_dialer.api_url')
    ariane_dialer_tenant_id = fields.Char(
        'Tenant id', config_parameter='ariane_dialer.tenant_id')
    ariane_dialer_signing_key = fields.Char(
        'Signing key', config_parameter='ariane_dialer.signing_key',
        groups='base.group_system')
    ariane_dialer_price_block_values = fields.Char(
        'Prices that mean "do not quote"', config_parameter='ariane_dialer.price_block_values',
        help='Comma-separated unit prices that are sentinels, not real prices '
             '(for example a blocked-price marker). Cards show "check price" instead.')
    ariane_dialer_filler_phrases = fields.Char(
        'Filler lines', config_parameter='ariane_dialer.filler_phrases',
        help='What the rep can say while the coach or a product search is still working. '
             'One phrase per line (or separated by |), at most 10, each up to 80 characters. '
             'A line with a digit or a currency sign is dropped, so a filler never carries a price. '
             'Empty: the built-in lines.')
    ariane_dialer_hot_days = fields.Integer(
        '"Selling in your state" window (days)', config_parameter='ariane_dialer.hot_days', default=30,
        help='Sales window for the hot-by-state list (7 to 180 days).')
    ariane_dialer_hot_count = fields.Integer(
        '"Selling in your state" products', config_parameter='ariane_dialer.hot_count', default=5,
        help='How many products the hot-by-state list shows (1 to 10; 0 turns it off).')
    ariane_dialer_hot_min_orders = fields.Integer(
        '"Selling in your state" minimum orders', config_parameter='ariane_dialer.hot_min_orders', default=3,
        help='A product needs this many separate orders in the state and window, from at least '
             'two different customers, to count as hot, so one order or one shop is never called a trend.')

    ariane_dialer_transcript_retention_days = fields.Integer(
        'Keep call transcripts (days)', config_parameter='ariane_dialer.transcript_retention_days', default=90,
        help='A daily job removes the transcript text and lines of calls older than this. '
             'The call, its outcome, note and AI summary stay. 0 = keep forever.')
    ariane_dialer_health_retention_days = fields.Integer(
        'Keep call-health timelines (days)', config_parameter='ariane_dialer.health_retention_days', default=30,
        help='A daily job removes the call-health event timeline of calls older than this. '
             'The call-health numbers stay. 0 = keep forever.')

    def action_dialer_refresh_lists(self):
        """Recompute the coach's cached lists (hot by state, new sellers) on the
        next call, in every worker."""
        rules = self.env['ariane.dialer.rules']
        for name in ('hot_by_state', 'new_sellers'):
            rules._dialer_cache_invalidate(name)
        return True
