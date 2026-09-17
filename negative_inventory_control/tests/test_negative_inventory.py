from odoo import api
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestNegativeInventoryBase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.negative_stock_policy = 'allow'

        cls.category = cls.env['product.category'].create({'name': "NIC Test Category"})
        cls.product = cls.env['product.template'].create({
            'name': "NIC Test Product",
            'type': 'consu',
            'is_storable': True,
            'categ_id': cls.category.id,
        }).product_variant_id

        # Explicitly the company's own warehouse stock location - not a bare
        # search([('usage', '=', 'internal')], limit=1), which silently
        # depends on stock.location's default sort order (complete_name) to
        # land on WH/Stock. That's fragile against any other internal
        # location a real user creates that happens to sort earlier
        # alphabetically (e.g. a location literally named "Stock ...").
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', cls.company.id)], limit=1)
        cls.loc_a = cls.warehouse.lot_stock_id
        cls.loc_b = cls.env['stock.location'].create({
            'name': "NIC Test Location B",
            'usage': 'internal',
            'location_id': cls.loc_a.location_id.id,
        })
        cls.customer_loc = cls.env.ref('stock.stock_location_customers')
        cls.supplier_loc = cls.env.ref('stock.stock_location_suppliers')
        cls.out_type = cls.env['stock.picking.type'].search([
            ('code', '=', 'outgoing'), ('warehouse_id', '=', cls.warehouse.id),
        ], limit=1)

        cls.non_manager = cls.env['res.users'].create({
            'name': "NIC Stock User (non-manager)",
            'login': 'nic_test_stock_user',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('stock.group_stock_user').id,
            ])],
        })

    def tearDown(self):
        # Reset every level of the hierarchy between tests so one test's
        # configuration never leaks into the next.
        self.product.negative_stock_policy = False
        self.category.negative_stock_policy = False
        self.loc_a.negative_stock_policy = False
        self.loc_b.negative_stock_policy = False
        self.company.negative_stock_policy = 'allow'
        super().tearDown()

    def _on_hand(self, location=None):
        location = location or self.loc_a
        quants = self.env['stock.quant'].sudo()._gather(self.product, location, strict=False)
        return sum(quants.mapped('quantity'))

    def _make_picking(self, qty, location=None):
        location = location or self.loc_a
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.out_type.id,
            'location_id': location.id,
            'location_dest_id': self.customer_loc.id,
        })
        self.env['stock.move'].create({
            'picking_id': picking.id,
            'product_id': self.product.id,
            'product_uom_qty': qty,
            'product_uom': self.product.uom_id.id,
            'location_id': location.id,
            'location_dest_id': self.customer_loc.id,
        })
        return picking

    def _top_up(self, qty, location=None, product=None):
        location = location or self.loc_a
        product = product or self.product
        move = self.env['stock.move'].create({
            'product_id': product.id,
            'product_uom_qty': qty,
            'product_uom': product.uom_id.id,
            'location_id': self.supplier_loc.id,
            'location_dest_id': location.id,
        })
        move._action_confirm()
        move.picked = True
        move.quantity = qty
        move._action_done()


class TestPolicyResolution(TestNegativeInventoryBase):
    """Round 4: the configuration hierarchy and strictest-wins precedence."""

    def test_falls_back_to_company_default(self):
        self.assertEqual(self.loc_a._get_negative_inventory_policy(self.product), 'allow')

    def test_category_level_applies(self):
        self.category.negative_stock_policy = 'warn'
        self.assertEqual(self.loc_a._get_negative_inventory_policy(self.product), 'warn')

    def test_product_overrides_category_when_stricter(self):
        self.category.negative_stock_policy = 'warn'
        self.product.negative_stock_policy = 'block'
        self.assertEqual(self.loc_a._get_negative_inventory_policy(self.product), 'block')

    def test_strictest_wins_regardless_of_source(self):
        # A looser product-level setting must NOT win over a stricter location.
        self.product.negative_stock_policy = 'allow'
        self.loc_a.negative_stock_policy = 'block'
        self.assertEqual(self.loc_a._get_negative_inventory_policy(self.product), 'block')

    def test_locations_are_evaluated_independently(self):
        self.loc_a.negative_stock_policy = 'block'
        self.assertEqual(self.loc_a._get_negative_inventory_policy(self.product), 'block')
        self.assertEqual(self.loc_b._get_negative_inventory_policy(self.product), 'allow')

    def test_quant_effective_policy_field_matches_resolution(self):
        # Round E14: the only place product+location are always paired, so
        # it's the only reliable place to show what's actually in effect.
        self.loc_a.negative_stock_policy = 'warn'
        quant = self.env['stock.quant'].sudo().create({
            'product_id': self.product.id, 'location_id': self.loc_a.id,
        })
        self.assertEqual(quant.effective_negative_stock_policy, 'warn')

        self.product.negative_stock_policy = 'block'
        quant.invalidate_recordset(['effective_negative_stock_policy'])
        self.assertEqual(quant.effective_negative_stock_policy, 'block')


class TestDetectionAndEnforcement(TestNegativeInventoryBase):
    """Round 5: the mandatory stock.move-level backstop."""

    def test_allow_is_a_pure_regression_no_behaviour_change(self):
        before = self._on_hand()
        self._make_picking(5).button_validate()
        self.assertEqual(self._on_hand(), before - 5)

    def test_warn_proceeds(self):
        self.product.negative_stock_policy = 'warn'
        before = self._on_hand()
        self._make_picking(5).button_validate()
        self.assertEqual(self._on_hand(), before - 5)

    def test_warn_shows_an_onscreen_notification(self):
        self.product.negative_stock_policy = 'warn'
        picking = self._make_picking(5)
        res = picking.button_validate()

        self.assertIsInstance(res, dict)
        self.assertEqual(res.get('tag'), 'display_notification')
        self.assertIn(self.product.display_name, res['params']['message'])
        self.assertEqual(picking.state, 'done', "the notification must not have blocked the transfer")

    def test_warn_chatter_message_is_real_html_not_escaped_tags(self):
        self.product.negative_stock_policy = 'warn'
        picking = self._make_picking(5)
        picking.button_validate()

        warning_message = picking.message_ids.filtered(lambda m: 'on hand' in (m.body or ''))
        self.assertTrue(warning_message, "the negative-stock warning note must be on the chatter")
        body = warning_message[0].body
        self.assertIn('<p>', body, "the chatter note must be real HTML, not a plain string with literal <br/> text in it")
        self.assertNotIn('&lt;', body, "the markup must not have been escaped into visible tag text")
        self.assertIn(self.product.display_name, body)

    def test_block_raises_and_leaves_quantity_untouched(self):
        self.product.negative_stock_policy = 'block'
        before = self._on_hand()
        picking = self._make_picking(5)
        with self.assertRaises(UserError):
            picking.button_validate()
        self.assertEqual(self._on_hand(), before)
        self.assertNotEqual(picking.state, 'done')

    def test_sufficient_stock_is_never_blocked(self):
        self._top_up(100)
        self.product.negative_stock_policy = 'block'
        before = self._on_hand()
        self._make_picking(10).button_validate()
        self.assertEqual(self._on_hand(), before - 10)

    def test_locations_are_blocked_independently(self):
        self.loc_b.negative_stock_policy = 'block'
        before_a = self._on_hand(self.loc_a)
        # loc_a has no policy of its own and no stock either, but 'allow' lets it through
        self._make_picking(3, self.loc_a).button_validate()
        self.assertEqual(self._on_hand(self.loc_a), before_a - 3)

        before_b = self._on_hand(self.loc_b)
        with self.assertRaises(UserError):
            self._make_picking(3, self.loc_b).button_validate()
        self.assertEqual(self._on_hand(self.loc_b), before_b)

    def test_scrap_is_covered_by_the_same_backstop(self):
        # Scrap has no picking-level pre-hook, so this only works if the
        # stock.move-level check (not the picking hook) is the one enforcing it.
        self.product.negative_stock_policy = 'block'
        scrap = self.env['stock.scrap'].create({
            'product_id': self.product.id,
            'product_uom_id': self.product.uom_id.id,
            'scrap_qty': 5,
            'location_id': self.loc_a.id,
        })
        with self.assertRaises(UserError):
            scrap.do_scrap()


class TestExpandedMatrix(TestNegativeInventoryBase):
    """Round B: test-matrix items called out as gaps in the Round 8 review -
    lot-tracked products, MRP consumption, multi-product transfers, returns,
    and multi-warehouse - each proving the mechanism on a scenario the core
    suite above didn't specifically exercise.
    """

    def test_lot_tracked_product_going_negative(self):
        lot_product = self.env['product.template'].create({
            'name': "NIC Lot Product", 'type': 'consu', 'is_storable': True,
            'categ_id': self.category.id, 'tracking': 'lot',
        }).product_variant_id
        lot = self.env['stock.lot'].create({
            'product_id': lot_product.id, 'name': 'NIC-LOT-1', 'company_id': self.company.id,
        })
        lot_product.negative_stock_policy = 'block'

        move = self.env['stock.move'].create({
            'product_id': lot_product.id, 'product_uom_qty': 5, 'product_uom': lot_product.uom_id.id,
            'location_id': self.loc_a.id, 'location_dest_id': self.customer_loc.id,
        })
        move._action_confirm()
        self.env['stock.move.line'].create({
            'move_id': move.id, 'product_id': lot_product.id, 'product_uom_id': lot_product.uom_id.id,
            'location_id': self.loc_a.id, 'location_dest_id': self.customer_loc.id,
            'lot_id': lot.id, 'quantity': 5, 'picked': True,
        })
        with self.assertRaises(UserError):
            move._action_done()

    def test_mrp_consumption_going_negative(self):
        if 'mrp.production' not in self.env:
            self.skipTest("mrp is not installed")

        finished = self.env['product.template'].create({
            'name': "NIC Finished Product", 'type': 'consu', 'is_storable': True,
            'categ_id': self.category.id,
        }).product_variant_id
        bom = self.env['mrp.bom'].create({
            'product_tmpl_id': finished.product_tmpl_id.id,
            'product_qty': 1,
            'type': 'normal',
            'bom_line_ids': [(0, 0, {'product_id': self.product.id, 'product_qty': 5})],
        })
        self.product.negative_stock_policy = 'block'

        mo = self.env['mrp.production'].create({
            'product_id': finished.id,
            'product_qty': 1,
            'product_uom_id': finished.uom_id.id,
            'bom_id': bom.id,
        })
        mo.action_confirm()
        with self.assertRaises(UserError):
            mo.button_mark_done()

    def test_multi_product_transfer_only_blocks_the_offending_line(self):
        other_product = self.env['product.template'].create({
            'name': "NIC Other Product", 'type': 'consu', 'is_storable': True,
            'categ_id': self.category.id,
        }).product_variant_id
        self._top_up(100, self.loc_a, product=other_product)
        other_before = sum(self.env['stock.quant'].sudo()._gather(other_product, self.loc_a, strict=False).mapped('quantity'))

        self.product.negative_stock_policy = 'block'
        picking = self._make_picking(5)  # self.product, no stock -> would go negative
        self.env['stock.move'].create({
            'picking_id': picking.id, 'product_id': other_product.id, 'product_uom_qty': 3,
            'product_uom': other_product.uom_id.id, 'location_id': self.loc_a.id,
            'location_dest_id': self.customer_loc.id,
        })

        with self.assertRaises(UserError):
            picking.button_validate()
        # atomic batch: the whole picking is blocked, including the line that
        # would have been fine on its own - nothing partially applies
        other_after = sum(self.env['stock.quant'].sudo()._gather(other_product, self.loc_a, strict=False).mapped('quantity'))
        self.assertEqual(other_after, other_before)

    def test_return_is_never_falsely_blocked(self):
        self._top_up(20)
        self.product.negative_stock_policy = 'block'
        delivery = self._make_picking(5)
        delivery.button_validate()

        return_picking = self.env['stock.picking'].create({
            'picking_type_id': self.out_type.return_picking_type_id.id or self.out_type.id,
            'location_id': self.customer_loc.id,
            'location_dest_id': self.loc_a.id,
        })
        self.env['stock.move'].create({
            'picking_id': return_picking.id, 'product_id': self.product.id, 'product_uom_qty': 5,
            'product_uom': self.product.uom_id.id, 'location_id': self.customer_loc.id,
            'location_dest_id': self.loc_a.id,
        })
        before = self._on_hand()
        return_picking.button_validate()  # must not raise - increasing stock is never a risk
        self.assertEqual(self._on_hand(), before + 5)

    def test_multi_warehouse_policy_via_location_hierarchy(self):
        # Proves the Round 3 design decision that "warehouse-level" policy is
        # just policy set on that warehouse's own location subtree, with no
        # separate warehouse field needed.
        wh2 = self.env['stock.warehouse'].create({'name': "NIC WH2", 'code': "NIC2"})
        wh2.view_location_id.negative_stock_policy = 'block'

        before_wh1 = self._on_hand(self.loc_a)
        self._make_picking(5, self.loc_a).button_validate()
        self.assertEqual(self._on_hand(self.loc_a), before_wh1 - 5)

        with self.assertRaises(UserError):
            self._make_picking(5, wh2.lot_stock_id).button_validate()


class TestApprovalWorkflow(TestNegativeInventoryBase):
    """Round 6: the picking-level Approval Required workflow."""

    def setUp(self):
        super().setUp()
        self.product.negative_stock_policy = 'approval'

    def test_button_validate_pauses_and_creates_a_draft_request(self):
        picking = self._make_picking(5)
        res = picking.button_validate()
        self.assertIsInstance(res, dict)
        self.assertEqual(res.get('res_model'), 'negative.inventory.request')
        self.assertNotEqual(picking.state, 'done')

        request = self.env['negative.inventory.request'].search([('picking_id', '=', picking.id)])
        self.assertEqual(len(request), 1)
        self.assertEqual(request.state, 'draft')
        self.assertEqual(len(request.line_ids), 1)

    def test_submit_without_reason_is_rejected(self):
        picking = self._make_picking(5)
        picking.button_validate()
        request = self.env['negative.inventory.request'].search([('picking_id', '=', picking.id)])
        with self.assertRaises(UserError):
            request.action_submit()

    def test_full_approval_cycle_resumes_and_completes(self):
        before = self._on_hand()
        picking = self._make_picking(5)
        picking.button_validate()
        request = self.env['negative.inventory.request'].search([('picking_id', '=', picking.id)])

        request.reason = "Testing the approval cycle."
        request.action_submit()
        self.assertEqual(request.state, 'to_approve')

        request.action_approve()
        self.assertEqual(request.state, 'done')
        self.assertEqual(picking.state, 'done')
        self.assertEqual(self._on_hand(), before - 5)

    def test_non_manager_cannot_approve_or_reject(self):
        picking = self._make_picking(5)
        picking.button_validate()
        request = self.env['negative.inventory.request'].search([('picking_id', '=', picking.id)])
        request.reason = "test"
        request.action_submit()

        with self.assertRaises(UserError):
            request.with_user(self.non_manager).action_approve()
        with self.assertRaises(UserError):
            request.with_user(self.non_manager).action_reject()
        self.assertEqual(request.state, 'to_approve')

    def test_reject_leaves_picking_unvalidated(self):
        picking = self._make_picking(5)
        picking.button_validate()
        request = self.env['negative.inventory.request'].search([('picking_id', '=', picking.id)])
        request.reason = "test"
        request.action_submit()
        request.action_reject()

        self.assertEqual(request.state, 'rejected')
        self.assertNotEqual(picking.state, 'done')

    def test_block_takes_priority_and_never_creates_a_request(self):
        self.product.negative_stock_policy = 'block'
        picking = self._make_picking(5)
        with self.assertRaises(UserError):
            picking.button_validate()
        self.assertFalse(self.env['negative.inventory.request'].search([('picking_id', '=', picking.id)]))

    def test_reattempting_an_already_pending_request_does_not_duplicate(self):
        picking = self._make_picking(5)
        picking.button_validate()
        first = self.env['negative.inventory.request'].search([('picking_id', '=', picking.id)])
        first.reason = "test"
        first.action_submit()

        with self.assertRaises(UserError):
            picking.button_validate()
        self.assertEqual(
            self.env['negative.inventory.request'].search_count([('picking_id', '=', picking.id)]), 1)


class TestScrapApproval(TestNegativeInventoryBase):
    """Round A1: Approval Required extended to scrap, which has no picking
    and therefore no _pre_action_done_hook - stock.scrap.action_validate()
    is its own equivalent gate.
    """

    def _make_scrap(self, qty, location=None):
        return self.env['stock.scrap'].create({
            'product_id': self.product.id,
            'product_uom_id': self.product.uom_id.id,
            'scrap_qty': qty,
            'location_id': (location or self.loc_a).id,
        })

    def test_approval_pauses_and_creates_a_draft_request(self):
        self.product.negative_stock_policy = 'approval'
        scrap = self._make_scrap(5)
        res = scrap.action_validate()
        self.assertIsInstance(res, dict)
        self.assertEqual(res.get('res_model'), 'negative.inventory.request')
        self.assertNotEqual(scrap.state, 'done')

        request = self.env['negative.inventory.request'].search([('scrap_id', '=', scrap.id)])
        self.assertEqual(len(request), 1)
        self.assertEqual(request.state, 'draft')

    def test_full_approval_cycle_resumes_and_completes(self):
        self.product.negative_stock_policy = 'approval'
        before = self._on_hand()
        scrap = self._make_scrap(5)
        scrap.action_validate()
        request = self.env['negative.inventory.request'].search([('scrap_id', '=', scrap.id)])

        request.reason = "Testing scrap approval."
        request.action_submit()
        request.action_approve()

        self.assertEqual(request.state, 'done')
        self.assertEqual(scrap.state, 'done')
        self.assertEqual(self._on_hand(), before - 5)

    def test_block_still_hard_stops_before_any_request(self):
        self.product.negative_stock_policy = 'block'
        scrap = self._make_scrap(5)
        with self.assertRaises(UserError):
            scrap.action_validate()
        self.assertFalse(self.env['negative.inventory.request'].search([('scrap_id', '=', scrap.id)]))


class TestQuantApproval(TestNegativeInventoryBase):
    """Round A1: Approval Required extended to inventory adjustments, the
    other path that calls _action_done() directly (stock.quant._apply_inventory),
    bypassing every picking-level hook.
    """

    def _set_count(self, quantity, location=None):
        location = location or self.loc_a
        quant = self.env['stock.quant']._gather(self.product, location, strict=False)
        if not quant:
            quant = self.env['stock.quant'].create({
                'product_id': self.product.id,
                'location_id': location.id,
            })
        quant = quant[:1].with_context(inventory_mode=True)
        quant.inventory_quantity = quantity
        return quant

    def test_approval_pauses_and_creates_a_draft_request(self):
        self.product.negative_stock_policy = 'approval'
        quant = self._set_count(-5)
        res = quant.action_apply_inventory()
        self.assertIsInstance(res, dict)
        self.assertEqual(res.get('res_model'), 'negative.inventory.request')

        request = self.env['negative.inventory.request'].search([('quant_id', '=', quant.id)])
        self.assertEqual(len(request), 1)
        self.assertEqual(request.state, 'draft')
        # the count was never applied - it's still sitting there for resume
        self.assertEqual(quant.inventory_quantity, -5)

    def test_full_approval_cycle_resumes_and_applies_the_count(self):
        self.product.negative_stock_policy = 'approval'
        quant = self._set_count(-5)
        quant.action_apply_inventory()
        request = self.env['negative.inventory.request'].search([('quant_id', '=', quant.id)])

        request.reason = "Testing inventory adjustment approval."
        request.action_submit()
        request.action_approve()

        self.assertEqual(request.state, 'done')
        self.assertEqual(self._on_hand(), -5)

    def test_block_still_hard_stops_before_any_request(self):
        self.product.negative_stock_policy = 'block'
        quant = self._set_count(-5)
        with self.assertRaises(UserError):
            quant.action_apply_inventory()
        self.assertFalse(self.env['negative.inventory.request'].search([('quant_id', '=', quant.id)]))
        # nothing was applied - the count is still pending, untouched
        self.assertEqual(quant.inventory_quantity, -5)

    def test_positive_count_is_never_a_negative_stock_risk(self):
        self.product.negative_stock_policy = 'block'
        quant = self._set_count(50)
        # a count that only ever increases stock must never be blocked
        quant.action_apply_inventory()
        self.assertEqual(self._on_hand(), 50)


class TestMultiCompanySecurity(TestNegativeInventoryBase):
    """Round A2: requests and audit-log entries must be scoped per company -
    a user restricted to another company should never see them.
    """

    def test_request_and_log_are_company_scoped(self):
        company_b = self.env['res.company'].create({'name': "NIC Company B"})
        user_b = self.env['res.users'].create({
            'name': "NIC Company B User",
            'login': 'nic_test_company_b_user',
            'company_id': company_b.id,
            'company_ids': [(6, 0, [company_b.id])],
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('stock.group_stock_user').id,
            ])],
        })

        self.product.negative_stock_policy = 'approval'
        picking = self._make_picking(5)
        picking.button_validate()
        request = self.env['negative.inventory.request'].search([('picking_id', '=', picking.id)])
        self.assertTrue(request, "sanity check: the request must exist for company A")
        log = self.env['negative.inventory.log'].search([('picking_id', '=', picking.id)], limit=1)
        self.assertTrue(log, "sanity check: a pending_approval log entry must exist for company A")

        self.assertFalse(
            self.env['negative.inventory.request'].with_user(user_b).search([('id', '=', request.id)]),
            "a user restricted to Company B must not see Company A's request")
        self.assertFalse(
            self.env['negative.inventory.log'].with_user(user_b).search([('id', '=', log.id)]),
            "a user restricted to Company B must not see Company A's audit log entry")


class TestAuditLog(TestNegativeInventoryBase):
    """Round 7: every outcome leaves a durable trace."""

    def test_allow_and_warn_are_logged(self):
        self._make_picking(5).button_validate()
        self.assertTrue(self.env['negative.inventory.log'].search([
            ('product_id', '=', self.product.id), ('outcome', '=', 'allowed')]))

        self.product.negative_stock_policy = 'warn'
        self._make_picking(3).button_validate()
        self.assertTrue(self.env['negative.inventory.log'].search([
            ('product_id', '=', self.product.id), ('outcome', '=', 'warned')]))

    def test_approval_lifecycle_is_logged(self):
        self.product.negative_stock_policy = 'approval'
        picking = self._make_picking(5)
        picking.button_validate()
        request = self.env['negative.inventory.request'].search([('picking_id', '=', picking.id)])

        self.assertTrue(self.env['negative.inventory.log'].search([
            ('request_id', '=', request.id), ('outcome', '=', 'pending_approval')]))

        request.reason = "test"
        request.action_submit()
        request.action_approve()

        self.assertTrue(self.env['negative.inventory.log'].search([
            ('request_id', '=', request.id), ('outcome', '=', 'approved_proceeded')]))
        self.assertEqual(request.log_count, 2)


@tagged('post_install', '-at_install')
class TestBlockAuditDurability(TransactionCase):
    """Proves a Blocked outcome's audit-log entry is written on a standalone
    cursor and survives even though the transaction it describes never does.

    TransactionCase's own cursor (self.cr / self.env.cr) has commit/rollback
    monkey-patched to raise - Odoo's test framework deliberately forbids
    committing it, since that would break the savepoint-based rollback every
    other test here relies on. That patch only touches that one cursor
    instance, though (see odoo/tests/common.py), so this test opens genuinely
    separate cursors of its own - the same "open another cursor if really
    necessary" escape hatch the framework's own error message points to -
    and cleans up everything it commits itself.

    Two separate cursors are needed, not one: Odoo runs every connection at
    REPEATABLE READ isolation (see odoo/sql_db.py), so a cursor's snapshot is
    fixed as of its first query in a transaction. The cursor used to attempt
    the blocked transaction takes its snapshot *before*
    _create_standalone's write commits, so it could never see that write no
    matter how long it waited - proving nothing either way. Reading the
    result back needs a third, later cursor, exactly like a real, separate
    request checking the audit log afterwards would.
    """

    def test_blocked_event_is_logged_and_survives_the_rollback(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, self.env.uid, {})
            company = env.company
            category = env['product.category'].create({'name': "NIC Durability Category"})
            product = env['product.template'].create({
                'name': "NIC Durability Product", 'type': 'consu', 'is_storable': True,
                'categ_id': category.id,
            }).product_variant_id
            warehouse = env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
            loc = warehouse.lot_stock_id
            customer_loc = env.ref('stock.stock_location_customers')
            out_type = env['stock.picking.type'].search([
                ('code', '=', 'outgoing'), ('warehouse_id', '=', warehouse.id),
            ], limit=1)
            product.negative_stock_policy = 'block'

            picking = env['stock.picking'].create({
                'picking_type_id': out_type.id,
                'location_id': loc.id,
                'location_dest_id': customer_loc.id,
            })
            env['stock.move'].create({
                'picking_id': picking.id, 'product_id': product.id, 'product_uom_qty': 5,
                'product_uom': product.uom_id.id, 'location_id': loc.id,
                'location_dest_id': customer_loc.id,
            })
            product_id, category_id, picking_id = product.id, category.id, picking.id

            # Committing here is what makes this representative of real
            # usage, where the product and picking already exist as
            # committed data long before anyone tries to validate it -
            # without it, _create_standalone's own separate cursor couldn't
            # see this data either (same MVCC visibility rule being tested
            # here), and its FK constraints would fail before we ever got to
            # prove the interesting part.
            cr.commit()

            with self.assertRaises(UserError):
                picking.button_validate()
            # This cursor's own snapshot predates _create_standalone's write
            # (REPEATABLE READ) - it will never see it, so don't bother
            # checking here; verify from a fresh cursor below instead.

        with self.registry.cursor() as cr2:
            env2 = api.Environment(cr2, self.env.uid, {})
            log = env2['negative.inventory.log'].search([
                ('product_id', '=', product_id), ('outcome', '=', 'blocked')])
            self.assertTrue(log, "the blocked attempt must still be on record even though the attempt itself was prevented")
            log.unlink()

            # Cleanup order matters: the earlier confirm/reserve steps (before
            # the block raised) left the move 'assigned', with a reservation
            # on a quant. cancel() must release that reservation before
            # anything can be unlinked, and a zero-value quant is not
            # auto-removed by Odoo (see stock.quant._unlink_zero_quants's own
            # docstring) - it has to be swept explicitly.
            picking = env2['stock.picking'].browse(picking_id)
            picking.action_cancel()
            env2['stock.quant']._unlink_zero_quants()
            picking.move_ids.unlink()
            picking.unlink()
            env2['product.product'].browse(product_id).unlink()
            env2['product.category'].browse(category_id).unlink()

    def test_log_attribution_is_blank_not_wrong_across_multiple_pickings(self):
        """Round C8: when a single _action_done() call aggregates move lines
        from more than one picking sharing the same product+location, the
        log entry must not misattribute the aggregate to an arbitrary "first"
        picking - it should leave picking_id blank instead.
        """
        with self.registry.cursor() as cr:
            env = api.Environment(cr, self.env.uid, {})
            company = env.company
            category = env['product.category'].create({'name': "NIC Attribution Category"})
            product = env['product.template'].create({
                'name': "NIC Attribution Product", 'type': 'consu', 'is_storable': True,
                'categ_id': category.id,
            }).product_variant_id
            warehouse = env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
            loc = warehouse.lot_stock_id
            customer_loc = env.ref('stock.stock_location_customers')
            out_type = env['stock.picking.type'].search([
                ('code', '=', 'outgoing'), ('warehouse_id', '=', warehouse.id),
            ], limit=1)
            product.negative_stock_policy = 'block'

            pickings = env['stock.picking']
            for _i in range(2):
                picking = env['stock.picking'].create({
                    'picking_type_id': out_type.id, 'location_id': loc.id, 'location_dest_id': customer_loc.id,
                })
                env['stock.move'].create({
                    'picking_id': picking.id, 'product_id': product.id, 'product_uom_qty': 3,
                    'product_uom': product.uom_id.id, 'location_id': loc.id, 'location_dest_id': customer_loc.id,
                })
                pickings |= picking
            product_id, category_id, picking_ids = product.id, category.id, pickings.ids

            cr.commit()

            moves = pickings.move_ids
            moves._action_confirm()
            moves.picked = True
            for move in moves:
                move.quantity = 3
            with self.assertRaises(UserError):
                # Deliberately bypassing button_validate()/the picking hook -
                # this goes straight through the move-level backstop, which
                # is the layer that actually aggregates across pickings.
                moves._action_done()

        with self.registry.cursor() as cr2:
            env2 = api.Environment(cr2, self.env.uid, {})
            log = env2['negative.inventory.log'].search([
                ('product_id', '=', product_id), ('outcome', '=', 'blocked')])
            self.assertEqual(len(log), 1, "both moves share the same product+location, so they aggregate into one entry")
            self.assertFalse(log.picking_id, "must be blank, not an arbitrary guess, when two different pickings contributed")
            log.unlink()

            pickings = env2['stock.picking'].browse(picking_ids)
            pickings.action_cancel()
            env2['stock.quant']._unlink_zero_quants()
            pickings.move_ids.unlink()
            pickings.unlink()
            env2['product.product'].browse(product_id).unlink()
            env2['product.category'].browse(category_id).unlink()


class TestApprovalConcurrency(TransactionCase):
    """Round C9: two managers approving the same request at the same instant
    must not both resume the transaction - action_approve/action_reject lock
    the row first (try_lock_for_update, SKIP LOCKED) so the loser sees it as
    unavailable and moves on instead of double-processing it.
    """

    def test_locked_request_cannot_be_locked_again(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, self.env.uid, {})
            company = env.company
            category = env['product.category'].create({'name': "NIC Concurrency Category"})
            product = env['product.template'].create({
                'name': "NIC Concurrency Product", 'type': 'consu', 'is_storable': True,
                'categ_id': category.id,
            }).product_variant_id
            warehouse = env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
            loc = warehouse.lot_stock_id
            customer_loc = env.ref('stock.stock_location_customers')
            out_type = env['stock.picking.type'].search([
                ('code', '=', 'outgoing'), ('warehouse_id', '=', warehouse.id),
            ], limit=1)
            product.negative_stock_policy = 'approval'

            picking = env['stock.picking'].create({
                'picking_type_id': out_type.id, 'location_id': loc.id, 'location_dest_id': customer_loc.id,
            })
            env['stock.move'].create({
                'picking_id': picking.id, 'product_id': product.id, 'product_uom_qty': 5,
                'product_uom': product.uom_id.id, 'location_id': loc.id, 'location_dest_id': customer_loc.id,
            })
            picking.button_validate()
            request = env['negative.inventory.request'].search([('picking_id', '=', picking.id)])
            request.reason = "concurrency test"
            request.action_submit()
            product_id, category_id, picking_id, request_id = product.id, category.id, picking.id, request.id
            cr.commit()

            # Hold an exclusive lock on the request, simulating manager A's
            # in-flight action_approve() that hasn't committed yet.
            self.assertTrue(request.try_lock_for_update())

            with self.registry.cursor() as cr_b:
                env_b = api.Environment(cr_b, self.env.uid, {})
                request_b = env_b['negative.inventory.request'].browse(request_id)
                # Manager B's click: the row is locked by A, so SKIP LOCKED
                # means this returns nothing rather than blocking or racing.
                self.assertFalse(
                    request_b.try_lock_for_update(),
                    "a request already locked by another transaction must not be lockable again")

        # cr's block exit releases the lock (nothing was written on it, so
        # there's nothing to roll back); clean up on a fresh cursor.
        with self.registry.cursor() as cr2:
            env2 = api.Environment(cr2, self.env.uid, {})
            env2['negative.inventory.log'].search([('product_id', '=', product_id)]).unlink()
            env2['negative.inventory.request.line'].search([('request_id', '=', request_id)]).unlink()
            env2['negative.inventory.request'].browse(request_id).unlink()
            picking = env2['stock.picking'].browse(picking_id)
            picking.action_cancel()
            env2['stock.quant']._unlink_zero_quants()
            picking.move_ids.unlink()
            picking.unlink()
            env2['product.product'].browse(product_id).unlink()
            env2['product.category'].browse(category_id).unlink()
