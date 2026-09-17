from odoo import fields, models
from odoo.tools.float_utils import float_compare

from .negative_inventory_policy import POLICY_SELECTION, strictest_policy

# Only these location types represent real, countable stock. Customer, vendor,
# inventory-loss, and production locations are intentionally infinite
# counterparts by Odoo's own design (see stock.location.should_bypass_reservation)
# and are never subject to negative-stock control.
CONTROLLED_LOCATION_USAGES = ('internal', 'transit')


class StockLocation(models.Model):
    _inherit = 'stock.location'

    negative_stock_policy = fields.Selection(
        POLICY_SELECTION,
        string="Negative Stock Policy",
        help="Policy applied when stock at this location would go negative. "
             "Leave empty to inherit from a parent location, the product, its "
             "category, or the company default. Only meaningful for Internal "
             "and Transit locations - other location types are never subject "
             "to negative-stock control.",
    )

    def _get_negative_inventory_policy(self, product):
        """Resolve the effective negative-stock policy for `product` at this
        location.

        Collects every explicitly-set policy across the product, its category
        chain, this location's parent chain, and the company default, then
        returns the strictest one (Block > Approval Required > Warn > Allow).
        """
        self.ensure_one()
        policies = [product.negative_stock_policy]

        category = product.categ_id
        while category:
            policies.append(category.negative_stock_policy)
            category = category.parent_id

        location = self
        while location:
            policies.append(location.negative_stock_policy)
            location = location.location_id

        company = self.company_id or self.env.company
        policies.append(company.negative_stock_policy)

        return strictest_policy(policies)

    def _project_negative_inventory_entry(self, product, decrement):
        """Detect whether removing `decrement` units of `product` from this
        location would go negative, and under what policy.

        Pure detection - never raises, never writes anything. Shared by every
        origin that can drive stock negative (stock.move, stock.scrap,
        stock.quant inventory adjustments), so they all agree on what "would
        go negative" means and how on-hand quantity is computed.

        :return: an entry dict (product, location, on_hand, decrement,
                 projected, policy), or None if this location isn't
                 controlled, no policy applies, or it wouldn't go negative.
        """
        self.ensure_one()
        if self.usage not in CONTROLLED_LOCATION_USAGES:
            return None

        policy = self._get_negative_inventory_policy(product)
        if not policy:
            return None

        on_hand = sum(self.env['stock.quant'].sudo()._gather(product, self, strict=False).mapped('quantity'))
        projected = on_hand - decrement
        if float_compare(projected, 0.0, precision_rounding=product.uom_id.rounding) >= 0:
            return None

        return {
            'product': product,
            'location': self,
            'on_hand': on_hand,
            'decrement': decrement,
            'projected': projected,
            'policy': policy,
        }
