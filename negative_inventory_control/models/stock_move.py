import logging
from collections import defaultdict

from odoo import _, models
from odoo.exceptions import UserError

from .negative_inventory_policy import build_log_vals, format_entries_message, format_entries_message_html

_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _action_done(self, cancel_backorder=False):
        self._check_negative_inventory_policy()
        return super()._action_done(cancel_backorder=cancel_backorder)

    def _get_negative_inventory_entries(self):
        """Detect every (product, location) pair among self's move lines that
        would go negative if processed, with the resolved policy for each.

        Pure detection - never raises, never writes anything. Shared by the
        move-level enforcement backstop (`_check_negative_inventory_policy`)
        and `stock.picking`'s pre-validation hook, so both always agree on
        what "would go negative" means (both delegate the actual detection to
        `stock.location._project_negative_inventory_entry`).

        Each entry also carries `picking_id`/`move_id`, set only when every
        move line contributing to that (product, location) pair belongs to
        the same picking/move - left False when a single call spans several
        pickings sharing the same product+location, so the log never
        misattributes an aggregate to an arbitrary "first" one.

        :return: list of dicts with keys product, location, on_hand,
                 decrement, projected, policy, picking_id, move_id.
        """
        moves_todo = self.filtered(
            lambda m: m.state != 'cancel' and m.picked and (
                m.product_uom.compare(m.quantity, 0.0) > 0 or m.is_inventory
            )
        )
        move_lines = moves_todo.move_line_ids.filtered(
            lambda ml: ml.picked and not ml.product_uom_id.is_zero(ml.quantity)
        )
        if not move_lines:
            return []

        decrements = defaultdict(float)
        contributing_lines = defaultdict(lambda: self.env['stock.move.line'])
        for move_line in move_lines:
            key = (move_line.product_id, move_line.location_id)
            decrements[key] += move_line.quantity_product_uom
            contributing_lines[key] |= move_line

        entries = []
        for key, decrement in decrements.items():
            product, location = key
            entry = location._project_negative_inventory_entry(product, decrement)
            if entry:
                lines = contributing_lines[key]
                entry['picking_id'] = lines.picking_id.id if len(lines.picking_id) == 1 else False
                entry['move_id'] = lines.move_id.id if len(lines.move_id) == 1 else False
                entries.append(entry)
        return entries

    def _check_negative_inventory_policy(self):
        """Mandatory enforcement layer: runs for every path that completes a
        stock.move (delivery, internal transfer, scrap, unbuild, manufacturing
        consumption, inventory adjustment, ...), not just picking-driven ones.

        `stock.picking._pre_action_done_hook()`, `stock.scrap.action_validate()`
        and `stock.quant._apply_inventory()` each handle the friendlier,
        origin-specific UX (Warn confirmation, Approval request creation)
        *before* this ever runs. This method is what actually protects every
        path regardless, and is also what re-runs (and must pass) when an
        approved request resumes the original transaction - callers do that
        by passing `skip_negative_inventory_check` in the context.
        """
        if self.env.context.get('skip_negative_inventory_check'):
            return

        entries = self._get_negative_inventory_entries()
        if not entries:
            return

        allow_entries = [e for e in entries if e['policy'] == 'allow']
        warn_entries = [e for e in entries if e['policy'] == 'warn']
        blocked_entries = [e for e in entries if e['policy'] in ('approval', 'block')]

        if allow_entries:
            self._log_negative_inventory_entries(allow_entries, 'allowed')

        if warn_entries:
            header = _("Negative stock warning: the following operations will result in negative on-hand quantity.")
            _logger.warning(format_entries_message(self.env, warn_entries, header))
            for picking in self.picking_id:
                picking.message_post(body=format_entries_message_html(self.env, warn_entries, header))
            self._log_negative_inventory_entries(warn_entries, 'warned')

        if blocked_entries:
            # 'block' is a hard stop. 'approval' reaches here only when it
            # wasn't already handled upstream by an origin-specific hook
            # (stock.picking, stock.scrap, stock.quant) - e.g. a raw
            # stock.move created and completed directly with no such hook -
            # fail safe rather than let an unapproved transaction through.
            outcome = {'block': 'blocked', 'approval': 'blocked_no_approval_path'}
            log_vals = [
                self._negative_inventory_log_vals(e, outcome[e['policy']])
                for e in blocked_entries
            ]
            # This raises right after, rolling back the current transaction -
            # so the log entries are written on their own cursor, or they'd
            # vanish along with everything else.
            self.env['negative.inventory.log']._create_standalone(log_vals)
            raise UserError(format_entries_message(
                self.env, blocked_entries, _("This operation is blocked by the Negative Stock Policy configured for the following product(s)/location(s):")))

    def _negative_inventory_log_vals(self, entry, outcome, **origin_refs):
        # `entry` may already carry an authoritative (possibly False/ambiguous)
        # picking_id/move_id from _get_negative_inventory_entries - respect
        # that exactly rather than falling back to self, which could
        # misattribute an aggregate spanning several pickings to an
        # arbitrary "first" one.
        origin_refs.setdefault('picking_id', entry['picking_id'] if 'picking_id' in entry else self.picking_id[:1].id)
        origin_refs.setdefault('move_id', entry['move_id'] if 'move_id' in entry else self[:1].id)
        return build_log_vals(entry, outcome, entry['location'].company_id or self.env.company, **origin_refs)

    def _log_negative_inventory_entries(self, entries, outcome):
        vals_list = [self._negative_inventory_log_vals(e, outcome) for e in entries]
        self.env['negative.inventory.log'].sudo().create(vals_list)
