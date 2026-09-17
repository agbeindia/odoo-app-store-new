from odoo import _, models
from odoo.exceptions import UserError

from .negative_inventory_policy import build_log_vals, format_entries_message


class StockScrap(models.Model):
    _inherit = 'stock.scrap'

    def action_validate(self):
        """Scrap has no picking, so it never goes through
        stock.picking._pre_action_done_hook() - this is its own equivalent
        pre-validation gate, run before core's own (unrelated, informational)
        check_available_qty()/insufficient-qty wizard.
        """
        self.ensure_one()
        if self.env.context.get('skip_negative_inventory_check'):
            return super().action_validate()

        entry = self._get_negative_inventory_entry()
        if entry:
            if entry['policy'] == 'block':
                self.env['negative.inventory.log']._create_standalone(
                    [self._negative_inventory_log_vals(entry, 'blocked')])
                raise UserError(format_entries_message(
                    self.env, [entry], _("This operation is blocked by the Negative Stock Policy configured for the following product(s)/location(s):")))
            if entry['policy'] == 'approval':
                return self._action_open_negative_inventory_request(entry)
            outcome = 'warned' if entry['policy'] == 'warn' else 'allowed'
            self.env['negative.inventory.log'].sudo().create(self._negative_inventory_log_vals(entry, outcome))

        return super().action_validate()

    def _get_negative_inventory_entry(self):
        self.ensure_one()
        if not self.product_id.is_storable:
            return None
        quantity = self.product_uom_id._compute_quantity(self.scrap_qty, self.product_id.uom_id)
        return self.location_id._project_negative_inventory_entry(self.product_id, quantity)

    def _negative_inventory_log_vals(self, entry, outcome):
        return build_log_vals(entry, outcome, self.company_id, scrap_id=self.id)

    def _action_open_negative_inventory_request(self, entry):
        Request = self.env['negative.inventory.request']
        existing = Request.search([
            ('scrap_id', '=', self.id),
            ('state', 'in', ('draft', 'to_approve', 'approved')),
        ], limit=1)
        if existing:
            if existing.state != 'draft':
                raise UserError(_(
                    "This scrap requires approval and is already pending review (%s). "
                    "It will complete automatically once approved.", existing.name))
            request = existing
        else:
            request = Request.create({
                'scrap_id': self.id,
                'line_ids': [(0, 0, {
                    'product_id': entry['product'].id,
                    'location_id': entry['location'].id,
                    'quantity_before': entry['on_hand'],
                    'quantity_requested': entry['decrement'],
                    'quantity_expected_after': entry['projected'],
                })],
            })
            self.env['negative.inventory.log'].sudo().create({
                **self._negative_inventory_log_vals(entry, 'pending_approval'),
                'request_id': request.id,
            })

        view = self.env.ref('negative_inventory_control.negative_inventory_request_view_form')
        return {
            'name': _("Negative Stock - Approval Required"),
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'negative.inventory.request',
            'views': [(view.id, 'form')],
            'view_id': view.id,
            'target': 'new',
            'res_id': request.id,
        }
