from odoo import fields, models

from .negative_inventory_policy import POLICY_SELECTION


class ProductCategory(models.Model):
    _inherit = 'product.category'

    negative_stock_policy = fields.Selection(
        POLICY_SELECTION,
        string="Negative Stock Policy",
        help="Policy applied when stock of products in this category would go "
             "negative. Leave empty to inherit from a parent category, the "
             "product, the location, or the company default.",
    )
