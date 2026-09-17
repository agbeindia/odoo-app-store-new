import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class NegativeInventoryRequest(models.Model):
    _name = 'negative.inventory.request'
    _description = "Negative Inventory Approval Request"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default="New", copy=False, readonly=True)

    # Exactly one of these three is set, depending on what kind of
    # transaction hit an Approval Required policy - see the _check_one_origin
    # constraint. Picking-driven transfers are by far the most common case;
    # scrap and inventory adjustments are the two other paths that can
    # trigger negative stock without going through a picking (see Round 1's
    # source investigation).
    picking_id = fields.Many2one(
        'stock.picking', string="Transfer", ondelete='cascade',
        readonly=True, index=True, check_company=True)
    scrap_id = fields.Many2one(
        'stock.scrap', string="Scrap", ondelete='cascade',
        readonly=True, index=True, check_company=True)
    quant_id = fields.Many2one(
        'stock.quant', string="Inventory Adjustment", ondelete='cascade',
        readonly=True, index=True, check_company=True)

    company_id = fields.Many2one('res.company', compute='_compute_company_id', store=True, readonly=True)
    origin_display_name = fields.Char(compute='_compute_origin_display_name')

    state = fields.Selection([
        ('draft', "Draft"),
        ('to_approve', "To Approve"),
        ('approved', "Approved"),
        ('rejected', "Rejected"),
        ('done', "Done"),
    ], default='draft', required=True, tracking=True, index=True)

    line_ids = fields.One2many('negative.inventory.request.line', 'request_id', string="Lines")

    reason = fields.Text(string="Reason for requesting", tracking=True)
    decision_note = fields.Text(string="Decision Note", tracking=True)

    requested_by = fields.Many2one('res.users', default=lambda self: self.env.user, readonly=True)
    request_date = fields.Datetime(readonly=True)
    approved_by = fields.Many2one('res.users', readonly=True, tracking=True)
    decision_date = fields.Datetime(readonly=True)
    log_count = fields.Integer(compute='_compute_log_count')

    _check_one_origin = models.Constraint(
        'CHECK(num_nonnulls(picking_id, scrap_id, quant_id) = 1)',
        "A negative-stock request must be linked to exactly one transfer, scrap, or inventory adjustment.",
    )

    @api.depends('picking_id.company_id', 'scrap_id.company_id', 'quant_id.company_id')
    def _compute_company_id(self):
        for request in self:
            origin = request.picking_id or request.scrap_id or request.quant_id
            request.company_id = origin.company_id if origin else self.env.company

    @api.depends('picking_id.display_name', 'scrap_id.display_name', 'quant_id.display_name')
    def _compute_origin_display_name(self):
        for request in self:
            origin = request.picking_id or request.scrap_id or request.quant_id
            request.origin_display_name = origin.display_name if origin else False

    def _compute_log_count(self):
        counts = dict(self.env['negative.inventory.log']._read_group(
            [('request_id', 'in', self.ids)], ['request_id'], ['__count']))
        for request in self:
            request.log_count = counts.get(request, 0)

    def action_view_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('negative_inventory_control.negative_inventory_log_action')
        action['domain'] = [('request_id', '=', self.id)]
        return action

    def action_view_origin(self):
        self.ensure_one()
        origin = self.picking_id or self.scrap_id or self.quant_id
        return {
            'type': 'ir.actions.act_window',
            'res_model': origin._name,
            'view_mode': 'form',
            'res_id': origin.id,
        }

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', "New") == "New":
                vals['name'] = self.env['ir.sequence'].next_by_code('negative.inventory.request') or "New"
        return super().create(vals_list)

    def action_submit(self):
        for request in self:
            if request.state != 'draft':
                continue
            if not request.reason or not request.reason.strip():
                raise UserError(_("Please explain why this negative-stock transaction should be allowed before submitting it for approval."))
            request.write({'state': 'to_approve', 'request_date': fields.Datetime.now()})
            request.message_post(body=_(
                "Approval requested by %(user)s for %(origin)s.",
                user=request.requested_by.display_name, origin=request.origin_display_name,
            ))

    def _check_can_decide(self):
        if not self.env.user.has_group('stock.group_stock_manager'):
            raise UserError(_("Only an Inventory Administrator can approve or reject a negative-stock request."))

    def action_approve(self):
        self._check_can_decide()
        # Two managers clicking Approve on the same request at the same
        # instant must not both resume the transaction. try_lock_for_update
        # (SKIP LOCKED) lets the loser see the row as unavailable and move on
        # immediately rather than block-and-then-double-process it.
        for request in self.try_lock_for_update():
            if request.state != 'to_approve':
                continue
            request.write({
                'state': 'approved',
                'approved_by': self.env.user.id,
                'decision_date': fields.Datetime.now(),
            })
            request._resume_transaction()

    def action_reject(self):
        self._check_can_decide()
        for request in self.try_lock_for_update():
            if request.state != 'to_approve':
                continue
            request.write({
                'state': 'rejected',
                'approved_by': self.env.user.id,
                'decision_date': fields.Datetime.now(),
            })
            request.message_post(body=_("Request rejected by %s.", self.env.user.display_name))
            request._log_lines('rejected')

    def _resume_transaction(self):
        """Re-run the original transaction now that this request is
        approved, bypassing only the negative-stock check (everything else -
        lots, backorders, other sanity checks - still applies normally).
        """
        self.ensure_one()
        origin = self.picking_id or self.scrap_id or self.quant_id
        try:
            resumed = origin.with_context(skip_negative_inventory_check=True)
            if self.picking_id:
                resumed.button_validate()
            elif self.scrap_id:
                resumed.do_scrap()
            else:
                resumed._apply_inventory()
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure must be recorded, not crash the Approve button
            _logger.exception("Failed to resume %s after approval of request %s", origin.display_name, self.name)
            self.message_post(body=_("Approved, but the transaction could not be completed automatically: %s\nPlease complete it manually.", exc))
            return
        self.state = 'done'
        self.message_post(body=_("Transaction completed automatically after approval."))
        self._log_lines('approved_proceeded')

    def _log_lines(self, outcome):
        self.ensure_one()
        self.env['negative.inventory.log'].sudo().create([{
            'product_id': line.product_id.id,
            'location_id': line.location_id.id,
            'company_id': self.company_id.id,
            'quantity_before': line.quantity_before,
            'transaction_quantity': line.quantity_requested,
            'quantity_after': line.quantity_expected_after,
            'policy_applied': 'approval',
            'outcome': outcome,
            'picking_id': self.picking_id.id,
            'scrap_id': self.scrap_id.id,
            'quant_id': self.quant_id.id,
            'request_id': self.id,
            'reason': self.reason,
        } for line in self.line_ids])


class NegativeInventoryRequestLine(models.Model):
    _name = 'negative.inventory.request.line'
    _description = "Negative Inventory Approval Request Line"

    request_id = fields.Many2one('negative.inventory.request', required=True, ondelete='cascade')
    product_id = fields.Many2one('product.product', required=True, readonly=True)
    location_id = fields.Many2one('stock.location', required=True, readonly=True)
    quantity_before = fields.Float(string="On Hand", readonly=True)
    quantity_requested = fields.Float(string="Requested", readonly=True)
    quantity_expected_after = fields.Float(string="Expected After", readonly=True)
