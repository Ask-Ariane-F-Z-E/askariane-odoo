"""The "Ask Ariane > Connection" screen: status, company picker, Connect and
Disconnect. Both buttons check base.group_system on the server; the view's
groups are only a convenience."""
from odoo import api, fields, models

from .connect_service import check_system


class AskArianeConnectPanel(models.TransientModel):
    _name = 'askariane.connect.panel'
    _description = 'Ask Ariane connection'

    @api.model
    def _default_companies(self):
        return self.env.company

    company_ids = fields.Many2many(
        'res.company', 'askariane_connect_panel_company_rel', 'panel_id', 'company_id',
        string='Companies to connect', default=_default_companies,
        domain=lambda self: [('id', 'in', self.env.user.company_ids.ids)],
        help='Only these companies are read for the diagnostic (1 to 10).')
    connected = fields.Boolean(compute='_compute_status')
    tenant_id = fields.Char(compute='_compute_status', string='Ask Ariane tenant')
    account_email = fields.Char(compute='_compute_status', string='Ask Ariane account')
    approved_company_names = fields.Char(compute='_compute_status', string='Approved companies')
    pinned_origin = fields.Char(compute='_compute_status', string='Pinned Odoo address')
    key_expires_at = fields.Char(compute='_compute_status', string='Key expires')
    origin_mismatch = fields.Boolean(compute='_compute_status')

    def _compute_status(self):
        service = self.env['askariane.connect']
        conn = service._connection() if self.env.user.has_group('base.group_system') else None
        names = ''
        if conn:
            companies = self.env['res.company'].sudo().browse(conn['company_ids']).exists()
            names = ', '.join(companies.sorted('id').mapped('name'))
        for panel in self:
            panel.connected = bool(conn)
            panel.tenant_id = conn['tenant_id'] if conn else False
            panel.account_email = conn['account_email'] if conn else False
            panel.approved_company_names = names or False
            panel.pinned_origin = conn['origin'] if conn else False
            panel.key_expires_at = conn['expires_at'] if conn else False
            panel.origin_mismatch = bool(conn) and service._current_origin() != conn['origin']

    def action_connect(self):
        check_system(self.env)
        self.ensure_one()
        url = self.env['askariane.connect.enrolment']._start(self.company_ids)
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'self'}

    def action_disconnect(self):
        check_system(self.env)
        revoked = self.env['askariane.connect']._disconnect()
        if revoked:
            title, message, kind = 'Disconnected', 'Ask Ariane confirmed the disconnect. The key is deleted.', 'success'
        else:
            title, kind = 'Disconnected here', 'warning'
            message = ('The key is deleted from this Odoo, but Ask Ariane did not confirm the '
                       'disconnect. You can also disconnect from your account on askariane.ai.')
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': kind,
                'sticky': not revoked,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }
