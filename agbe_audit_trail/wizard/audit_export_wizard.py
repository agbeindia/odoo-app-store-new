import base64
import io

import xlsxwriter

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.audit_log import OPERATIONS


class AuditExportWizard(models.TransientModel):
    _name = "audit.export.wizard"
    _description = "Audit Trail: Export Wizard"

    date_from = fields.Datetime(string="From")
    date_to = fields.Datetime(string="To", default=fields.Datetime.now)
    model_ids = fields.Many2many(
        comodel_name="ir.model",
        string="Models",
        help="Leave empty to export every audited model.",
    )
    user_ids = fields.Many2many(comodel_name="res.users", string="Users", help="Leave empty to export every user.")
    operation = fields.Selection(
        selection=[("all", "All Operations")] + OPERATIONS,
        default="all",
        required=True,
    )
    log_ids = fields.Many2many(
        comodel_name="audit.log",
        string="Selected Logs",
        help="When set, exactly these logs are exported and the filters above are ignored.",
    )
    export_format = fields.Selection(
        selection=[("xlsx", "Excel (.xlsx)"), ("pdf", "PDF")],
        string="Format",
        default="xlsx",
        required=True,
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        context = self.env.context
        if context.get("active_ids"):
            if context.get("active_model") == "audit.log":
                res["log_ids"] = [fields.Command.set(context["active_ids"])]
            elif context.get("active_model") == "audit.log.line":
                lines = self.env["audit.log.line"].browse(context["active_ids"])
                res["log_ids"] = [fields.Command.set(lines.log_id.ids)]
        return res

    def _get_logs(self):
        self.ensure_one()
        if self.log_ids:
            return self.log_ids
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError(_("The start date must be before the end date."))
        domain = []
        if self.date_from:
            domain.append(("date", ">=", self.date_from))
        if self.date_to:
            domain.append(("date", "<=", self.date_to))
        if self.model_ids:
            domain.append(("model_id", "in", self.model_ids.ids))
        if self.user_ids:
            domain.append(("user_id", "in", self.user_ids.ids))
        if self.operation != "all":
            domain.append(("operation", "=", self.operation))
        return self.env["audit.log"].search(domain)

    def action_export(self):
        logs = self._get_logs()
        if not logs:
            raise UserError(_("No audit logs match these filters."))
        if self.export_format == "pdf":
            return self.env.ref("agbe_audit_trail.action_report_audit_log").report_action(logs)
        return self._export_xlsx(logs)

    def _export_xlsx(self, logs):
        operation_labels = dict(OPERATIONS)
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        title = workbook.add_format({"bold": True, "font_size": 14})
        header = workbook.add_format({"bold": True, "bg_color": "#714B67", "font_color": "#FFFFFF", "border": 1})
        cell = workbook.add_format({"border": 1, "valign": "top", "text_wrap": True})
        old_cell = workbook.add_format({"border": 1, "valign": "top", "text_wrap": True, "bg_color": "#FDECEA"})
        new_cell = workbook.add_format({"border": 1, "valign": "top", "text_wrap": True, "bg_color": "#E8F5E9"})

        def to_local(dt):
            return fields.Datetime.context_timestamp(self, dt).strftime("%Y-%m-%d %H:%M:%S")

        # Sheet 1: one row per field change
        sheet = workbook.add_worksheet(_("Field Changes"))
        sheet.write(0, 0, _("Audit Trail - Field Changes"), title)
        headers = [_("Date"), _("User"), _("Operation"), _("Document Type"), _("Record ID"),
                   _("Record"), _("Field"), _("Old Value"), _("New Value"), _("IP Address")]
        widths = [20, 22, 12, 24, 10, 32, 26, 45, 45, 16]
        for col, (label, width) in enumerate(zip(headers, widths)):
            sheet.write(2, col, label, header)
            sheet.set_column(col, col, width)
        row = 3
        for log in logs:
            base = [to_local(log.date), log.user_id.name or "", operation_labels[log.operation],
                    log.model_description or log.model_name, log.res_id, log.record_name or ""]
            for line in log.line_ids or [None]:
                for col, value in enumerate(base):
                    sheet.write(row, col, value, cell)
                sheet.write(row, 6, line.field_description if line else "", cell)
                sheet.write(row, 7, line.old_value or "" if line else "", old_cell)
                sheet.write(row, 8, line.new_value or "" if line else "", new_cell)
                sheet.write(row, 9, log.ip_address or "", cell)
                row += 1
        sheet.autofilter(2, 0, max(row - 1, 2), len(headers) - 1)
        sheet.freeze_panes(3, 0)

        # Sheet 2: summary per model / operation
        summary = workbook.add_worksheet(_("Summary"))
        summary.write(0, 0, _("Audit Trail - Summary"), title)
        for col, label in enumerate([_("Document Type"), _("Created"), _("Updated"), _("Deleted"), _("Total")]):
            summary.write(2, col, label, header)
        summary.set_column(0, 0, 32)
        summary.set_column(1, 4, 12)
        counts = {}
        for log in logs:
            by_op = counts.setdefault(log.model_description or log.model_name, {"create": 0, "write": 0, "unlink": 0})
            by_op[log.operation] += 1
        for row, (model, by_op) in enumerate(sorted(counts.items()), start=3):
            summary.write(row, 0, model, cell)
            summary.write(row, 1, by_op["create"], cell)
            summary.write(row, 2, by_op["write"], cell)
            summary.write(row, 3, by_op["unlink"], cell)
            summary.write(row, 4, sum(by_op.values()), cell)

        workbook.close()
        filename = f"audit_trail_{fields.Date.context_today(self)}.xlsx"
        attachment = self.env["ir.attachment"].create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(output.getvalue()),
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }
