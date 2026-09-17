from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .negative_inventory_policy import POLICY_SELECTION, build_log_vals, format_entries_message


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    effective_negative_stock_policy = fields.Selection(
        POLICY_SELECTION, compute='_compute_effective_negative_stock_policy',
        string="Negative Stock Policy",
        help="The policy that actually applies to this exact product/location "
             "combination, after resolving the full Product / Category / "
             "Location / Company hierarchy (strictest wins). This is the one "
             "place product and location are always paired, so it's the only "
             "reliable place to show what policy is really in effect - a "
             "policy set on a product or location alone can't say that on "
             "its own, since the other side of the pair can always override it.")

    @api.depends('product_id', 'location_id')
    def _compute_effective_negative_stock_policy(self):
        for quant in self:
            quant.effective_negative_stock_policy = quant.location_id._get_negative_inventory_policy(quant.product_id) or 'allow'

    def _apply_inventory(self, date=None):
        """Inventory adjustments have no picking and call _action_done()
        directly (see Round 1's source investigation), bypassing
        stock.picking._pre_action_done_hook() entirely - this is their own
        equivalent pre-validation gate.

        Only quants whose count would *decrease* on-hand quantity are ever a
        negative-stock risk; increasing quants proceed untouched. A Blocked
        quant fails the whole call (consistent with the picking-level
        all-or-nothing batch semantics - see stock_picking.py); quants
        needing Approval are pulled out of the batch, leaving their
        inventory_quantity in place (untouched, unapplied) so re-calling this
        after approval picks up exactly where it left off; everything else
        proceeds normally, with Warn/Allow logging handled automatically by
        the stock.move-level backstop once the resulting moves complete.

        :return: the negative.inventory.request record(s) created or reused
                 for any quant that needed approval (empty recordset if none).
        """
        if self.env.context.get('skip_negative_inventory_check'):
            return super()._apply_inventory(date=date)

        entries_by_quant = {}
        for quant in self:
            if quant.product_uom_id.compare(quant.inventory_diff_quantity, 0) >= 0:
                continue
            decrement = -quant.inventory_diff_quantity
            entry = quant.location_id._project_negative_inventory_entry(quant.product_id, decrement)
            if entry:
                entries_by_quant[quant] = entry

        blocked = {quant: entry for quant, entry in entries_by_quant.items() if entry['policy'] == 'block'}
        if blocked:
            self.env['negative.inventory.log']._create_standalone([
                quant._negative_inventory_log_vals(entry, 'blocked') for quant, entry in blocked.items()
            ])
            raise UserError(format_entries_message(
                self.env, list(blocked.values()), _("This operation is blocked by the Negative Stock Policy configured for the following product(s)/location(s):")))

        approval = {quant: entry for quant, entry in entries_by_quant.items() if entry['policy'] == 'approval'}
        approval_quants = self.browse().union(*approval) if approval else self.browse()
        proceed_quants = self - approval_quants

        if proceed_quants:
            super(StockQuant, proceed_quants)._apply_inventory(date=date)

        requests = self.env['negative.inventory.request']
        for quant, entry in approval.items():
            requests |= quant._action_open_negative_inventory_request(entry)
        return requests

    def action_apply_inventory(self, date=None):
        if self.filtered(lambda quant: quant.is_outdated):
            # Core's own data-conflict wizard takes priority.
            return super().action_apply_inventory(date=date)

        requests = self._apply_inventory(date)
        self.inventory_quantity_set = False
        if not requests:
            return True

        view = self.env.ref('negative_inventory_control.negative_inventory_request_view_form')
        action = {
            'name': _("Negative Stock - Approval Required"),
            'type': 'ir.actions.act_window',
            'res_model': 'negative.inventory.request',
        }
        if len(requests) == 1:
            action.update(view_mode='form', views=[(view.id, 'form')], view_id=view.id, target='new', res_id=requests.id)
        else:
            action.update(view_mode='list,form', domain=[('id', 'in', requests.ids)])
        return action

    def _negative_inventory_log_vals(self, entry, outcome):
        self.ensure_one()
        return build_log_vals(entry, outcome, self.company_id or self.env.company, quant_id=self.id)

    def _action_open_negative_inventory_request(self, entry):
        self.ensure_one()
        Request = self.env['negative.inventory.request']
        existing = Request.search([
            ('quant_id', '=', self.id),
            ('state', 'in', ('draft', 'to_approve', 'approved')),
        ], limit=1)
        if existing:
            return existing

        request = Request.create({
            'quant_id': self.id,
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
        return request
