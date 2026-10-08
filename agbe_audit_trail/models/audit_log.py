import difflib
import json
import re

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from odoo.tools import html2plaintext

from .audit_rule import MAGIC_FIELDS, SENSITIVE_FIELDS, SKIPPED_TYPES

OPERATIONS = [
    ("create", "Created"),
    ("write", "Updated"),
    ("unlink", "Deleted"),
]

# Values longer than this get a word-level diff instead of plain old/new cells.
WORD_DIFF_MIN_LENGTH = 40

# Longer values are cut, so a huge HTML body or JSON blob cannot bloat the log.
MAX_VALUE_LENGTH = 10000
TRUNCATED_SUFFIX = " [truncated]"

# Field types whose old value can be written back with "Restore Old Value".
RESTORABLE_TYPES = {
    "char", "text", "html", "selection", "integer", "float", "monetary",
    "boolean", "date", "datetime", "many2one", "many2many",
}


def _truncate(text):
    if len(text) > MAX_VALUE_LENGTH:
        return text[:MAX_VALUE_LENGTH - len(TRUNCATED_SUFFIX)] + TRUNCATED_SUFFIX
    return text


class AuditLog(models.Model):
    _name = "audit.log"
    _description = "Audit Trail: Log"
    _order = "date desc, id desc"
    _rec_name = "record_name"

    date = fields.Datetime(default=fields.Datetime.now, required=True, readonly=True, index=True)
    user_id = fields.Many2one(
        comodel_name="res.users", string="User", readonly=True, index=True,
        default=lambda self: self.env.user,
    )
    company_id = fields.Many2one(
        comodel_name="res.company", string="Company", readonly=True, index=True,
        default=lambda self: self.env.company,
    )
    rule_id = fields.Many2one(comodel_name="audit.rule", string="Audit Rule", readonly=True, ondelete="set null", index=True)
    model_id = fields.Many2one(comodel_name="ir.model", string="Model", readonly=True, ondelete="cascade", index=True)
    model_name = fields.Char(string="Technical Model", readonly=True, index=True)
    model_description = fields.Char(string="Document Type", readonly=True)
    res_id = fields.Integer(string="Record ID", readonly=True, index=True)
    record_name = fields.Char(string="Record", readonly=True)
    operation = fields.Selection(selection=OPERATIONS, required=True, readonly=True, index=True)
    line_ids = fields.One2many(comodel_name="audit.log.line", inverse_name="log_id", string="Field Changes", readonly=True)
    change_count = fields.Integer(string="# Fields", readonly=True)
    changed_fields = fields.Char(string="Changed Fields", readonly=True)
    ip_address = fields.Char(string="IP Address", readonly=True, index=True)
    user_agent = fields.Char(string="Browser / Device", readonly=True)
    is_bulk = fields.Boolean(string="Bulk Operation", readonly=True, index=True)
    bulk_count = fields.Integer(string="Records Affected", readonly=True)
    bulk_res_ids = fields.Text(string="Affected Record IDs", readonly=True)
    diff_html = fields.Html(string="Changes", compute="_compute_diff_html", sanitize=False)
    record_exists = fields.Boolean(compute="_compute_record_exists")

    def _compute_display_name(self):
        for log in self:
            log.display_name = f"{log.model_description or log.model_name} / {log.record_name or log.res_id}"

    @api.depends("line_ids")
    def _compute_diff_html(self):
        for log in self:
            log.diff_html = log._render_diff_table()

    def _compute_record_exists(self):
        for log in self:
            if log.model_name in self.env and log.res_id:
                log.record_exists = bool(self.env[log.model_name].sudo().browse(log.res_id).exists())
            else:
                log.record_exists = False

    # ------------------------------------------------------------------
    # Diff viewer
    # ------------------------------------------------------------------

    @api.model
    def _word_diff(self, old, new):
        """ Return (old_html, new_html) with removed words struck in red and
            added words highlighted in green. """
        old_words = re.split(r"(\s+)", old)
        new_words = re.split(r"(\s+)", new)
        old_html, new_html = [], []
        matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            old_chunk = escape("".join(old_words[i1:i2]))
            new_chunk = escape("".join(new_words[j1:j2]))
            if tag == "equal":
                old_html.append(old_chunk)
                new_html.append(new_chunk)
                continue
            if old_chunk:
                old_html.append(Markup('<del class="o_audit_del">%s</del>') % old_chunk)
            if new_chunk:
                new_html.append(Markup('<ins class="o_audit_ins">%s</ins>') % new_chunk)
        return Markup("").join(old_html), Markup("").join(new_html)

    @api.model
    def _diff_pair(self, operation, old, new):
        if operation == "write" and max(len(old), len(new)) >= WORD_DIFF_MIN_LENGTH:
            return self._word_diff(old, new)
        return escape(old), escape(new)

    def _render_diff_table(self):
        self.ensure_one()
        intro = Markup("")
        if self.is_bulk:
            intro = Markup('<div class="alert alert-warning">%s</div>') % _(
                "Bulk operation on %s records: values below are from the first record.", self.bulk_count)
        if not self.line_ids:
            return intro + Markup('<p class="text-muted">%s</p>') % _("No field values were recorded.")
        rows = []
        for line in self.line_ids:
            old_html, new_html = self._diff_pair(self.operation, line.old_value or "", line.new_value or "")
            rows.append(Markup(
                "<tr>"
                '<td class="fw-bold">%s<div class="text-muted small">%s</div></td>'
                '<td class="o_audit_old">%s</td>'
                '<td class="o_audit_arrow text-center">&#8594;</td>'
                '<td class="o_audit_new">%s</td>'
                "</tr>"
            ) % (line.field_description, line.field_name, old_html, new_html))
        return intro + Markup(
            "<style>"
            ".o_audit_diff td{vertical-align:top;white-space:pre-wrap;word-break:break-word}"
            ".o_audit_diff .o_audit_old{background:rgba(220,53,69,.08)}"
            ".o_audit_diff .o_audit_new{background:rgba(25,135,84,.08)}"
            ".o_audit_diff del.o_audit_del{background:rgba(220,53,69,.3);text-decoration:line-through}"
            ".o_audit_diff ins.o_audit_ins{background:rgba(25,135,84,.3);text-decoration:none}"
            "</style>"
            '<table class="table table-sm table-bordered o_audit_diff">'
            "<thead><tr><th style=\"width:22%%\">%s</th><th style=\"width:37%%\">%s</th>"
            "<th style=\"width:4%%\"></th><th style=\"width:37%%\">%s</th></tr></thead>"
            "<tbody>%s</tbody></table>"
        ) % (_("Field"), _("Old Value"), _("New Value"), Markup("").join(rows))

    # ------------------------------------------------------------------
    # Value snapshots (called from the `base` overrides)
    # ------------------------------------------------------------------

    @api.model
    def _audit_field_names(self, records, conf, candidates=None):
        """ Names of the fields of `records` the rule `conf` should record,
            restricted to `candidates` when given (e.g. the keys of a write). """
        names = []
        for fname in (records._fields if candidates is None else candidates):
            field = records._fields.get(fname)
            if (
                not field
                or not field.store
                or field.type in SKIPPED_TYPES
                or fname in MAGIC_FIELDS
                or fname in SENSITIVE_FIELDS
                or fname in conf["excluded_fields"]
                or (conf["fields"] is not None and fname not in conf["fields"])
                # on create/delete, derived values (related, computed) only
                # repeat what the user-entered fields already show
                or (candidates is None and field.compute and not field.inverse)
            ):
                continue
            names.append(fname)
        return names

    @api.model
    def _audit_snapshot(self, records, fnames):
        """ Return {record_id: {field_name: (comparable, display_text, restore_json)}}. """
        snapshot = {}
        for record in records.sudo().with_context(active_test=False):
            values = {}
            for fname in fnames:
                field = record._fields[fname]
                value = record[fname]
                values[fname] = (
                    self._audit_raw(field, value),
                    _truncate(self._audit_format(record, field, value)),
                    self._audit_json(field, value),
                )
            snapshot[record.id] = values
        return snapshot

    @api.model
    def _audit_raw(self, field, value):
        if field.type == "many2one":
            return value.id
        if field.type in ("many2many", "one2many"):
            return tuple(sorted(value.ids))
        return value

    @api.model
    def _audit_json(self, field, value):
        """ JSON form of `value` that `_audit_unjson` can write back, or None
            when the field type cannot be restored. """
        if field.type not in RESTORABLE_TYPES:
            return None
        if field.type == "many2one":
            value = value.id
        elif field.type == "many2many":
            value = value.ids
        elif field.type == "date":
            value = fields.Date.to_string(value)
        elif field.type == "datetime":
            value = fields.Datetime.to_string(value)
        text = json.dumps(value, default=str)
        return text if len(text) <= MAX_VALUE_LENGTH else None

    @api.model
    def _audit_format(self, record, field, value):
        """ Human-readable text for `value`, frozen at logging time so the log
            stays meaningful after related records are renamed or deleted. """
        if field.type == "boolean":
            return _("Yes") if value else _("No")
        if field.type in ("integer", "float", "monetary"):
            return str(value)
        if not value:
            return ""
        if field.type == "many2one":
            return value.display_name or ""
        if field.type == "many2many":
            return ", ".join(name or "" for name in value.mapped("display_name"))
        if field.type == "selection":
            return str(dict(field._description_selection(self.env)).get(value, value))
        if field.type == "datetime":
            return fields.Datetime.context_timestamp(record, value).strftime("%Y-%m-%d %H:%M:%S")
        if field.type == "date":
            return fields.Date.to_string(value)
        if field.type == "html":
            return html2plaintext(value)
        if field.type in ("json", "properties"):
            return json.dumps(value, default=str, ensure_ascii=False)
        return str(value)

    # ------------------------------------------------------------------
    # Log creation
    # ------------------------------------------------------------------

    @api.model
    def _audit_request_info(self):
        """ IP address and user agent of the HTTP request behind this change,
            empty for cron jobs, shell scripts and other non-web changes. """
        try:
            httprequest = request.httprequest if request else None
        except RuntimeError:  # no request bound to this thread
            httprequest = None
        if not httprequest:
            return {}
        return {
            "ip_address": httprequest.remote_addr,
            "user_agent": (httprequest.user_agent.string or "")[:250],
        }

    @api.model
    def _audit_line_vals(self, records, fname, old_text, new_text, old_json=None):
        field = records._fields[fname]
        ir_field = self.env["ir.model.fields"].sudo()._get(records._name, fname)
        return {
            "field_id": ir_field.id,
            "field_name": fname,
            "field_description": ir_field.field_description or field.string or fname,
            "field_type": field.type,
            "old_value": old_text,
            "new_value": new_text,
            "old_value_json": old_json,
        }

    @api.model
    def _audit_base_vals(self, conf, operation, records):
        model = self.env["ir.model"]._get(records._name)
        return {
            "rule_id": conf["rule_id"],
            "model_id": model.id,
            "model_name": records._name,
            "model_description": model.name,
            "operation": operation,
            "user_id": self.env.uid,
            "company_id": self.env.company.id,
            **self._audit_request_info(),
        }

    @api.model
    def _audit_log(self, conf, operation, records, old=None, new=None):
        """ Create the logs for `records` (one per record that actually has a
            change) and send the alert digest if the rule asks for one. """
        old, new = old or {}, new or {}
        base_vals = self._audit_base_vals(conf, operation, records)
        vals_list = []
        for record in records:
            before, after = old.get(record.id, {}), new.get(record.id, {})
            lines = []
            for fname in (after or before):
                old_raw, old_text, old_json = before.get(fname, (None, "", None))
                new_raw, new_text, __ = after.get(fname, (None, "", None))
                if operation == "write" and old_raw == new_raw:
                    continue
                if operation != "write" and not (new_raw if operation == "create" else old_raw):
                    continue  # empty/False/0 fields are noise on create/delete
                lines.append(self._audit_line_vals(
                    records, fname, old_text, new_text, old_json if operation == "write" else None,
                ))
            if operation == "write" and not lines:
                continue
            vals_list.append({
                **base_vals,
                "res_id": record.id,
                "record_name": self._audit_record_name(record, before, after),
                "change_count": len(lines),
                "changed_fields": ", ".join(line["field_description"] for line in lines),
                "line_ids": [fields.Command.create(line) for line in lines],
            })
        if not vals_list:
            return self.browse()
        logs = self.sudo().with_context(audit_trail_disable=True).create(vals_list)
        logs._audit_maybe_alert(conf)
        return logs

    @api.model
    def _audit_log_bulk(self, conf, operation, records, fnames=(), sample=None):
        """ One summary log for a mass operation on more records than the
            rule's bulk limit. `sample` is a snapshot of the first record. """
        sample = sample or {}
        lines = [
            self._audit_line_vals(
                records, fname,
                _("(values of %s records)", len(records)) if operation == "write" else "",
                text,
            )
            for fname, (__, text, __) in sample.items()
            if fname in fnames
        ]
        ids_text = ", ".join(map(str, records.ids))
        log = self.sudo().with_context(audit_trail_disable=True).create({
            **self._audit_base_vals(conf, operation, records),
            "res_id": 0,
            "record_name": _("%(count)s %(model)s records", count=len(records), model=records._description),
            "is_bulk": True,
            "bulk_count": len(records),
            "bulk_res_ids": _truncate(ids_text),
            "change_count": len(lines),
            "changed_fields": ", ".join(line["field_description"] for line in lines),
            "line_ids": [fields.Command.create(line) for line in lines],
        })
        log._audit_maybe_alert(conf)
        return log

    def _audit_maybe_alert(self, conf):
        if not self or self[0].operation not in conf["alert_operations"]:
            return
        logs = self
        if self[0].operation == "write" and conf["alert_fields"]:
            logs = self.filtered(lambda log: set(log.line_ids.mapped("field_name")) & conf["alert_fields"])
        if logs:
            logs._send_alert()

    @api.model
    def _audit_record_name(self, record, before, after):
        try:
            name = record.sudo().display_name
        except Exception:  # noqa: BLE001 - the record may already be gone
            name = False
        if not name:
            for values in (after, before):
                if values.get(record._rec_name or "name"):
                    name = values[record._rec_name or "name"][1]
                    break
        return name or f"{record._name},{record.id}"

    def _send_alert(self):
        """ One email per batch of logs of the same rule, not one per record. """
        rule = self.rule_id[:1]
        partners = rule.alert_user_ids.partner_id.filtered("email")
        if not partners:
            return
        operation_label = dict(OPERATIONS)[self[0].operation]
        base_url = self.get_base_url()
        blocks = []
        for log in self[:50]:
            link = f"{base_url}/odoo/action-agbe_audit_trail.action_audit_log/{log.id}"
            blocks.append(Markup(
                '<h4 style="margin:16px 0 4px">%s <a href="%s" style="font-size:12px">(%s)</a></h4>%s'
            ) % (log.display_name, link, _("open log"), log._render_diff_table()))
        if len(self) > 50:
            blocks.append(Markup("<p>%s</p>") % _("... and %s more records.", len(self) - 50))
        origin = Markup("")
        if self[0].ip_address:
            origin = Markup('<p style="color:#6b7280">%s</p>') % _(
                "From IP %(ip)s - %(agent)s", ip=self[0].ip_address, agent=self[0].user_agent or "")
        count = self[0].bulk_count if self[0].is_bulk else len(self)
        body = Markup(
            '<div style="font-family:Arial,sans-serif;font-size:13px">'
            "<p>%s</p>%s%s</div>"
        ) % (
            _("%(user)s %(operation)s %(count)s %(model)s record(s) on %(date)s.",
              user=self.env.user.name, operation=operation_label.lower(), count=count,
              model=self[0].model_description,
              date=fields.Datetime.context_timestamp(self[0], self[0].date).strftime("%Y-%m-%d %H:%M")),
            origin,
            Markup("").join(blocks),
        )
        self.env["mail.mail"].sudo().create({
            "subject": _("[Audit] %(model)s %(operation)s by %(user)s",
                         model=self[0].model_description, operation=operation_label.lower(),
                         user=self.env.user.name),
            "body_html": body,
            "email_from": self.env.company.email_formatted or self.env.user.email_formatted,
            "recipient_ids": [fields.Command.set(partners.ids)],
            "auto_delete": True,
        })

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_open_record(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": self.model_name,
            "res_id": self.res_id,
            "view_mode": "form",
            "target": "current",
        }

    def action_record_history(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("agbe_audit_trail.action_audit_log")
        action.update({
            "name": _("History: %s", self.record_name),
            "domain": [("model_name", "=", self.model_name), ("res_id", "=", self.res_id)],
            "context": {"create": False},
        })
        return action


class AuditLogLine(models.Model):
    _name = "audit.log.line"
    _description = "Audit Trail: Field Change"
    _order = "date desc, log_id desc, id"
    _rec_name = "field_description"

    log_id = fields.Many2one(comodel_name="audit.log", string="Log", required=True, ondelete="cascade", index=True)
    field_id = fields.Many2one(comodel_name="ir.model.fields", string="Field", ondelete="set null", index=True)
    field_name = fields.Char(string="Technical Field", readonly=True)
    field_description = fields.Char(string="Field Label", readonly=True)
    field_type = fields.Char(readonly=True)
    old_value = fields.Text(readonly=True)
    new_value = fields.Text(readonly=True)
    old_value_json = fields.Text(readonly=True, help="Machine-readable old value, used by Restore Old Value.")
    restored_date = fields.Datetime(string="Restored On", readonly=True)
    restored_by_id = fields.Many2one(comodel_name="res.users", string="Restored By", readonly=True)

    date = fields.Datetime(related="log_id.date", store=True, index=True)
    user_id = fields.Many2one(related="log_id.user_id", store=True, string="Changed By")
    operation = fields.Selection(related="log_id.operation", store=True)
    model_id = fields.Many2one(related="log_id.model_id", store=True)
    model_name = fields.Char(related="log_id.model_name")
    model_description = fields.Char(related="log_id.model_description", string="Document Type")
    res_id = fields.Integer(related="log_id.res_id", store=True)
    record_name = fields.Char(related="log_id.record_name")
    ip_address = fields.Char(related="log_id.ip_address")
    is_bulk = fields.Boolean(related="log_id.is_bulk")
    company_id = fields.Many2one(related="log_id.company_id", store=True)

    diff_old_html = fields.Html(compute="_compute_diff_html", sanitize=False)
    diff_new_html = fields.Html(compute="_compute_diff_html", sanitize=False)
    can_restore = fields.Boolean(compute="_compute_can_restore")

    @api.depends("old_value", "new_value", "operation")
    def _compute_diff_html(self):
        Log = self.env["audit.log"]
        empty = Markup('<span class="text-muted fst-italic">%s</span>') % _("(empty)")
        for line in self:
            old_html, new_html = Log._diff_pair(line.operation, line.old_value or "", line.new_value or "")
            line.diff_old_html = old_html or empty
            line.diff_new_html = new_html or empty

    @api.depends("operation", "old_value_json", "is_bulk")
    def _compute_can_restore(self):
        for line in self:
            line.can_restore = (
                line.operation == "write"
                and not line.is_bulk
                and line.old_value_json is not False
                and line.field_type in RESTORABLE_TYPES
            )

    def action_view_diff(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Change: %(field)s on %(record)s", field=self.field_description, record=self.record_name),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "views": [(self.env.ref("agbe_audit_trail.audit_log_line_view_form_diff").id, "form")],
            "target": "new",
        }

    def action_open_log(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "audit.log",
            "res_id": self.log_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_open_record(self):
        return self.log_id.action_open_record()

    def _audit_unjson(self, field):
        """ Turn the stored JSON back into a value `write()` accepts. """
        self.ensure_one()
        value = json.loads(self.old_value_json)
        if field.type == "many2one" and value:
            if not self.env[field.comodel_name].browse(value).exists():
                raise UserError(_("The old value of '%s' was deleted since, so it cannot be restored.",
                                  self.field_description))
        if field.type == "many2many":
            existing = self.env[field.comodel_name].browse(value or []).exists()
            value = [fields.Command.set(existing.ids)]
        return value

    def action_restore(self):
        """ Write the old value back onto the record. Runs with the current
            user's rights, and is itself logged as a regular update. """
        if not self.env.user.has_group("agbe_audit_trail.group_audit_manager"):
            raise AccessError(_("Only Audit Managers can restore old values."))
        # one write per record, so restoring several fields is one audited change
        vals_by_record = {}
        for line in self:
            if not line.can_restore:
                raise UserError(_("The change to '%s' cannot be restored.", line.field_description))
            if line.model_name not in self.env:
                raise UserError(_("The model %s is not installed anymore.", line.model_name))
            record = self.env[line.model_name].browse(line.res_id).exists()
            if not record:
                raise UserError(_("%s was deleted, so its old values cannot be restored.", line.record_name))
            field = record._fields.get(line.field_name)
            if not field:
                raise UserError(_("The field %s does not exist anymore.", line.field_name))
            vals_by_record.setdefault(record, {})[line.field_name] = line._audit_unjson(field)
        for record, vals in vals_by_record.items():
            record.write(vals)
        self.sudo().write({"restored_date": fields.Datetime.now(), "restored_by_id": self.env.uid})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": _("Old value restored."),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
