from odoo import fields, models

from .negative_inventory_policy import POLICY_SELECTION


class ResCompany(models.Model):
    _inherit = 'res.company'

    negative_stock_policy = fields.Selection(
        POLICY_SELECTION,
        string="Negative Stock Policy",
        required=True,
        default='allow',
        help="Default policy applied when a stock operation would push on-hand "
             "quantity below zero, for any product, category, or location that "
             "doesn't have a more specific policy of its own. Defaults to "
             "Allow so installing this module does not change existing "
             "behaviour until you configure it.",
    )
