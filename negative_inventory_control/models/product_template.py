from odoo import fields, models

from .negative_inventory_policy import POLICY_SELECTION


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    negative_stock_policy = fields.Selection(
        POLICY_SELECTION,
        string="Negative Stock Policy",
        help="Policy applied when this product's stock would go negative. "
             "Leave empty to inherit from its category, the location, or the "
             "company default.",
    )
