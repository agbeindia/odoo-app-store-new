from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    negative_stock_policy = fields.Selection(
        related='company_id.negative_stock_policy',
        readonly=False,
    )
