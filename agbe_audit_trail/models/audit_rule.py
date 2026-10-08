from datetime import timedelta

from odoo import _, api, fields, models, tools
from odoo.exceptions import ValidationError

# High-volume or purely technical models: auditing them would flood the log
# (or recurse into the log itself) without telling an admin anything useful.
BLOCKED_MODELS = {
    "audit.rule", "audit.log", "audit.log.line",
    "bus.bus", "bus.presence", "ir.logging", "ir.cron.trigger", "ir.cron.progress",
    "mail.mail", "mail.message", "mail.notification", "mail.tracking.value",
    "mail.followers", "res.users.log", "res.device.log",
}

# Fields that change on every write or can never carry business meaning.
MAGIC_FIELDS = {"id", "create_uid", "create_date", "write_uid", "write_date", "display_name"}

# Field types the audit never records: binaries are too big to diff, and
# one2many changes are recorded on the child model (add a rule for it).
SKIPPED_TYPES = {"binary", "one2many", "properties_definition"}

# Never store secrets in clear text in the log.
SENSITIVE_FIELDS = {"password", "new_password", "totp_secret", "api_key", "oauth_access_token"}


class AuditRule(models.Model):
    _name = "audit.rule"
    _description = "Audit Trail: Rule"
    _inherit = ["mail.thread"]
    _order = "sequence, id"

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, tracking=True)
    model_id = fields.Many2one(
        comodel_name="ir.model",
        string="Model",
        required=True,
        ondelete="cascade",
        tracking=True,
        domain=[("transient", "=", False), ("abstract", "=", False)],
    )
    model_name = fields.Char(related="model_id.model", string="Technical Name", store=True, index=True)

    track_create = fields.Boolean(string="Log Creation", default=True, tracking=True)
    track_write = fields.Boolean(string="Log Updates", default=True, tracking=True)
    track_unlink = fields.Boolean(string="Log Deletion", default=True, tracking=True)

    field_mode = fields.Selection(
        selection=[
            ("all", "All Fields"),
            ("selected", "Only Selected Fields"),
            ("exclude", "All Fields Except Selected"),
        ],
        string="Fields to Track",
        required=True,
        default="all",
        tracking=True,
    )
    field_ids = fields.Many2many(
        comodel_name="ir.model.fields",
        relation="audit_rule_field_rel",
        column1="rule_id",
        column2="field_id",
        string="Fields",
        domain="[('model_id', '=', model_id), ('store', '=', True), "
               "('ttype', 'not in', ('binary', 'one2many', 'properties_definition'))]",
    )
    excluded_user_ids = fields.Many2many(
        comodel_name="res.users",
        relation="audit_rule_excluded_user_rel",
        column1="rule_id",
        column2="user_id",
        string="Ignored Users",
        help="Changes made by these users (e.g. integration or import users) are not logged.",
    )

    alert_create = fields.Boolean(string="Alert on Creation")
    alert_write = fields.Boolean(string="Alert on Update")
    alert_unlink = fields.Boolean(string="Alert on Deletion", default=True)
    alert_user_ids = fields.Many2many(
        comodel_name="res.users",
        relation="audit_rule_alert_user_rel",
        column1="rule_id",
        column2="user_id",
        string="Alert Recipients",
        help="These users receive an email digest when a matching operation is logged.",
    )
    alert_field_ids = fields.Many2many(
        comodel_name="ir.model.fields",
        relation="audit_rule_alert_field_rel",
        column1="rule_id",
        column2="field_id",
        string="Only Alert When These Fields Change",
        domain="[('model_id', '=', model_id), ('store', '=', True), "
               "('ttype', 'not in', ('binary', 'one2many', 'properties_definition'))]",
        help="Leave empty to alert on any update. When set, an update only triggers an alert "
             "if at least one of these fields changed (e.g. price, credit limit, bank account).",
    )

    batch_limit = fields.Integer(
        string="Bulk Operation Limit",
        default=500,
        help="When a single operation touches more records than this (mass edit, import, "
             "bulk delete), one summary log is written instead of one log per record, "
             "so large jobs stay fast. 0 always logs every record.",
    )

    retention_days = fields.Integer(
        string="Keep Logs (Days)",
        default=365,
        tracking=True,
        help="Logs older than this are purged automatically every day. 0 keeps them forever.",
    )
    history_action_id = fields.Many2one(
        comodel_name="ir.actions.act_window",
        string="History Action",
        readonly=True,
        copy=False,
        help="'Audit History' entry added to the Action menu of the audited model.",
    )
    log_count = fields.Integer(compute="_compute_log_count", string="Logs")

    _model_uniq = models.Constraint(
        "UNIQUE(model_id)",
        "There is already an audit rule for this model. Edit that rule instead.",
    )

    def _compute_log_count(self):
        counts = dict(self.env["audit.log"]._read_group(
            [("rule_id", "in", self.ids)], ["rule_id"], ["__count"],
        ))
        for rule in self:
            rule.log_count = counts.get(rule, 0)

    @api.constrains("model_id")
    def _check_model_id(self):
        for rule in self:
            if rule.model_id.model in BLOCKED_MODELS:
                raise ValidationError(_(
                    "The model '%s' cannot be audited: it is technical or part of the audit trail itself.",
                    rule.model_id.name,
                ))

    @api.constrains("track_create", "track_write", "track_unlink")
    def _check_operations(self):
        for rule in self:
            if not (rule.track_create or rule.track_write or rule.track_unlink):
                raise ValidationError(_("Rule '%s' must log at least one operation.", rule.name))

    @api.constrains("retention_days", "batch_limit")
    def _check_positive_numbers(self):
        if any(rule.retention_days < 0 or rule.batch_limit < 0 for rule in self):
            raise ValidationError(_("The retention period and the bulk operation limit cannot be negative."))

    @api.onchange("model_id")
    def _onchange_model_id(self):
        self.field_ids = False
        self.alert_field_ids = False
        if self.model_id and not self.name:
            self.name = self.model_id.name

    # ------------------------------------------------------------------
    # CRUD: the audit config is cached per registry, so every change to a
    # rule must invalidate it (this also signals the other workers).
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        rules = super().create(vals_list)
        self.env.registry.clear_cache()
        return rules

    def write(self, vals):
        res = super().write(vals)
        if "model_id" in vals:
            # the Action-menu entry points at the old model: rebuild it
            for rule in self.filtered("history_action_id"):
                rule.history_action_id.unlink()
                rule._create_history_action()
        if "active" in vals and not vals["active"]:
            self.history_action_id.unlink()
        self.env.registry.clear_cache()
        return res

    def unlink(self):
        self.history_action_id.unlink()
        res = super().unlink()
        self.env.registry.clear_cache()
        return res

    # ------------------------------------------------------------------
    # Runtime config, read by the create/write/unlink overrides on `base`
    # ------------------------------------------------------------------

    @api.model
    @tools.ormcache()
    def _get_audit_config(self):
        """ Return {model_name: config} for every active rule. Cached, since it
            is consulted on every create/write/unlink of every model. """
        config = {}
        for rule in self.sudo().with_context(active_test=True).search([]):
            if rule.model_name not in self.env:
                continue  # the model's module was uninstalled
            operations = set()
            alert_operations = set()
            for operation in ("create", "write", "unlink"):
                if rule[f"track_{operation}"]:
                    operations.add(operation)
                    if rule[f"alert_{operation}"] and rule.alert_user_ids:
                        alert_operations.add(operation)
            field_names = frozenset(rule.field_ids.mapped("name"))
            config[rule.model_name] = {
                "rule_id": rule.id,
                "operations": frozenset(operations),
                "alert_operations": frozenset(alert_operations),
                "fields": field_names if rule.field_mode == "selected" else None,
                "excluded_fields": field_names if rule.field_mode == "exclude" else frozenset(),
                "excluded_user_ids": frozenset(rule.excluded_user_ids.ids),
                "alert_fields": frozenset(rule.alert_field_ids.mapped("name")),
                "batch_limit": rule.batch_limit,
            }
        return config

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _create_history_action(self):
        self.ensure_one()
        self.history_action_id = self.env["ir.actions.act_window"].sudo().create({
            "name": _("Audit History"),
            "res_model": "audit.log",
            "view_mode": "list,form",
            "domain": f"[('model_name', '=', {self.model_name!r}), ('res_id', 'in', active_ids)]",
            "context": "{'create': False}",
            "binding_model_id": self.model_id.id,
            "binding_view_types": "list,form",
            "group_ids": [fields.Command.link(self.env.ref("agbe_audit_trail.group_audit_user").id)],
        })

    def action_add_history_action(self):
        for rule in self.filtered(lambda r: not r.history_action_id):
            rule._create_history_action()

    def action_remove_history_action(self):
        self.history_action_id.unlink()

    def action_view_logs(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("agbe_audit_trail.action_audit_log")
        action["domain"] = [("rule_id", "=", self.id)]
        action["context"] = {"create": False}
        return action

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------

    @api.model
    def _cron_purge_logs(self):
        now = fields.Datetime.now()
        Log = self.env["audit.log"].sudo()
        for rule in self.with_context(active_test=False).search([("retention_days", ">", 0)]):
            Log.search([
                ("rule_id", "=", rule.id),
                ("date", "<", now - timedelta(days=rule.retention_days)),
            ]).unlink()
