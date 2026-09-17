from odoo import _, models
from odoo.exceptions import UserError

from .negative_inventory_policy import format_entries_message


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def button_validate(self):
        """Warn-tier entries proceed on their own (nothing here pauses them -
        see _pre_action_done_hook for Block/Approval, which do), but a
        chatter note alone is easy to miss. Show an actual on-screen
        notification too, the same way Block/Approval already interrupt with
        something visible, instead of leaving Warn as a log entry someone has
        to go looking for.

        Detection can't be pre-computed before calling super() here: core's
        button_validate() auto-fills each draft move's done quantity as its
        *first* step, so a check run before that call would still see zero
        and never see the entries at all. Instead, read back what the
        move-level backstop (stock.move._check_negative_inventory_policy)
        already detected and logged correctly, after quantities were real.
        """
        skip = self.env.context.get('skip_negative_inventory_check')
        Log = self.env['negative.inventory.log']
        last_id_before = 0 if skip else (Log.search([], order='id desc', limit=1).id or 0)

        res = super().button_validate()

        if skip:
            return res
        new_warn_logs = Log.search([
            ('id', '>', last_id_before),
            ('outcome', '=', 'warned'),
            ('picking_id', 'in', self.ids),
        ])
        if not new_warn_logs:
            return res

        entries = [{
            'product': log.product_id,
            'location': log.location_id,
            'on_hand': log.quantity_before,
            'decrement': log.transaction_quantity,
            'projected': log.quantity_after,
            'policy': 'warn',
        } for log in new_warn_logs]

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Negative Stock Warning"),
                'message': format_entries_message(
                    self.env, entries, _("The following will result in negative on-hand quantity:")),
                'type': 'warning',
                'sticky': True,
                'next': res if isinstance(res, dict) else {'type': 'ir.actions.act_window_close'},
            },
        }

    def _pre_action_done_hook(self):
        res = super()._pre_action_done_hook()
        if res is not True:
            # Core's own pre-validation wizard (e.g. backorder confirmation)
            # takes priority and must run to completion first.
            return res
        if self.env.context.get('skip_negative_inventory_check'):
            return True

        blocked_entries = []
        blocked_log_vals = []
        pickings_needing_approval = {}
        for picking in self:
            entries = picking.move_ids._get_negative_inventory_entries()
            if not entries:
                continue
            picking_blocked = [e for e in entries if e['policy'] == 'block']
            if picking_blocked:
                blocked_entries += picking_blocked
                blocked_log_vals += [
                    picking.move_ids._negative_inventory_log_vals(e, 'blocked')
                    for e in picking_blocked
                ]
            approval_entries = [e for e in entries if e['policy'] == 'approval']
            if approval_entries:
                pickings_needing_approval[picking] = approval_entries

        if blocked_entries:
            # Written on its own cursor: the UserError below rolls back this
            # transaction, which would otherwise erase the very record of the
            # blocked attempt we want to keep.
            self.env['negative.inventory.log']._create_standalone(blocked_log_vals)
            raise UserError(format_entries_message(
                self.env, blocked_entries, _("This operation is blocked by the Negative Stock Policy configured for the following product(s)/location(s):")))

        if pickings_needing_approval:
            return self._action_open_negative_inventory_requests(pickings_needing_approval)

        return True

    def _action_open_negative_inventory_requests(self, pickings_needing_approval):
        """Create (or reuse) one draft/pending negative-stock request per
        picking, then open them for the user to fill in a reason and submit.
        """
        Request = self.env['negative.inventory.request']
        requests = Request

        for picking, entries in pickings_needing_approval.items():
            existing = Request.search([
                ('picking_id', '=', picking.id),
                ('state', 'in', ('draft', 'to_approve', 'approved')),
            ], limit=1)
            if existing:
                requests |= existing
                continue
            new_request = Request.create({
                'picking_id': picking.id,
                'line_ids': [(0, 0, {
                    'product_id': entry['product'].id,
                    'location_id': entry['location'].id,
                    'quantity_before': entry['on_hand'],
                    'quantity_requested': entry['decrement'],
                    'quantity_expected_after': entry['projected'],
                }) for entry in entries],
            })
            requests |= new_request
            self.env['negative.inventory.log'].sudo().create([
                {**picking.move_ids._negative_inventory_log_vals(entry, 'pending_approval'),
                 'request_id': new_request.id}
                for entry in entries
            ])

        pending = requests.filtered(lambda r: r.state in ('to_approve', 'approved'))
        if pending and pending == requests:
            # every picking already has a request in flight - nothing new to fill in
            raise UserError(_(
                "This operation requires approval and is already pending review (%s). "
                "It will complete automatically once approved.",
                ", ".join(pending.mapped('name')),
            ))

        draft_requests = requests.filtered(lambda r: r.state == 'draft')
        action = self.env['ir.actions.act_window']._for_xml_id(
            'negative_inventory_control.negative_inventory_request_action')
        action['domain'] = [('id', 'in', requests.ids)]
        if len(draft_requests) == 1 and len(requests) == 1:
            action['view_mode'] = 'form'
            action['views'] = [(False, 'form')]
            action['res_id'] = draft_requests.id
            action['target'] = 'new'
        return action
