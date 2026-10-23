from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # A manager's decision, so a field and not a product tag: only sales
    # managers can read or set it, it can run out, and the chatter records who
    # set it. The coach reads it with sudo and only product ids leave.
    dialer_clearance = fields.Boolean(
        'Clearance (push on calls)', index=True, copy=False, tracking=True,
        groups='sales_team.group_sale_manager',
        help='The live coach puts this product forward on calls, with a CLEARANCE mark. '
             'It is a stock fact, never a price: the coach never offers a discount for it.')
    dialer_clearance_until = fields.Date(
        'Clearance until', copy=False, tracking=True, groups='sales_team.group_sale_manager',
        help='Leave empty to keep it until a manager clears it. After this date: no badge.')
