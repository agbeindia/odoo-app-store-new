import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

OUTCOME_SELECTION = [
    ('allowed', "Allowed"),
    ('warned', "Warned"),
    ('blocked', "Blocked"),
    ('pending_approval', "Pending Approval"),
    ('approved_proceeded', "Approved - Proceeded"),
    ('rejected', "Rejected"),
    ('blocked_no_approval_path', "Blocked (approval not available for this operation)"),
]


class NegativeInventoryLog(models.Model):
    _name = 'negative.inventory.log'
    _description = "Negative Inventory Audit Log"
    _order = 'id desc'

    product_id = fields.Many2one('product.product', required=True, readonly=True)
    location_id = fields.Many2one('stock.location', required=True, readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)

    quantity_before = fields.Float(string="On Hand Before", readonly=True)
    transaction_quantity = fields.Float(string="Transaction Quantity", readonly=True)
    quantity_after = fields.Float(string="Expected On Hand After", readonly=True)

    policy_applied = fields.Selection(selection='_selection_policy', readonly=True)
    outcome = fields.Selection(OUTCOME_SELECTION, required=True, readonly=True)

    picking_id = fields.Many2one('stock.picking', readonly=True)
    move_id = fields.Many2one('stock.move', readonly=True)
    scrap_id = fields.Many2one('stock.scrap', readonly=True)
    quant_id = fields.Many2one('stock.quant', readonly=True)
    request_id = fields.Many2one('negative.inventory.request', readonly=True)

    user_id = fields.Many2one('res.users', string="User", default=lambda self: self.env.user, readonly=True)
    reason = fields.Text(readonly=True)

    @api.model
    def _selection_policy(self):
        from .negative_inventory_policy import POLICY_SELECTION
        return POLICY_SELECTION

    @api.model
    def _create_standalone(self, vals_list):
        """Persist log entries on their own, independent database cursor.

        Used for outcomes that are immediately followed by a raised exception
        (a blocked attempt): the enforcement error rolls back the whole
        transaction, which would otherwise take the log entry meant to record
        that attempt down with it. Logging failures here are swallowed - an
        audit-log hiccup must never mask or replace the real error the caller
        is about to raise.
        """
        if not vals_list:
            return
        try:
            with self.pool.cursor() as new_cr:
                new_env = api.Environment(new_cr, self.env.uid, self.env.context)
                new_env['negative.inventory.log'].sudo().create(vals_list)
        except Exception:
            _logger.exception("Failed to persist standalone negative-inventory audit log entries")
