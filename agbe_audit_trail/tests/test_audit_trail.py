from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestAuditTrail(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tz="UTC"))
        cls.manager = new_test_user(
            cls.env, login="audit_manager", email="manager@example.com",
            groups="base.group_user,base.group_partner_manager,agbe_audit_trail.group_audit_manager",
        )
        cls.plain_user = new_test_user(
            cls.env, login="audit_plain", groups="base.group_user,base.group_partner_manager",
        )
        cls.rule = cls.env["audit.rule"].create({
            "name": "Contacts",
            "model_id": cls.env["ir.model"]._get("res.partner").id,
            "alert_user_ids": [fields.Command.set(cls.manager.ids)],
        })
        cls.tag = cls.env["res.partner.category"].create({"name": "VIP"})

    def _logs(self, partner, operation=None):
        domain = [("model_name", "=", "res.partner"), ("res_id", "=", partner.id)]
        if operation:
            domain.append(("operation", "=", operation))
        return self.env["audit.log"].search(domain)

    def _alerts(self):
        return self.env["mail.mail"].search([("subject", "like", "[Audit]")])

    # ------------------------------------------------------------------

    def test_create_logs_user_entered_fields(self):
        partner = self.env["res.partner"].create({"name": "Acme", "email": "a@acme.com"})
        log = self._logs(partner, "create")
        self.assertEqual(len(log), 1)
        names = log.line_ids.mapped("field_name")
        self.assertIn("name", names)
        self.assertIn("email", names)
        self.assertNotIn("email_normalized", names, "computed fields are noise on create")
        self.assertNotIn("phone", names, "empty fields are not logged on create")

    def test_write_logs_field_diff(self):
        partner = self.env["res.partner"].create({"name": "Acme"})
        partner.write({"name": "Acme Corp", "category_id": [fields.Command.link(self.tag.id)]})
        log = self._logs(partner, "write")
        self.assertEqual(len(log), 1)
        by_field = {line.field_name: line for line in log.line_ids}
        self.assertEqual(by_field["name"].old_value, "Acme")
        self.assertEqual(by_field["name"].new_value, "Acme Corp")
        self.assertEqual(by_field["category_id"].new_value, "VIP")
        self.assertIn("o_audit_new", log.diff_html)

    def test_write_without_change_not_logged(self):
        partner = self.env["res.partner"].create({"name": "Acme"})
        partner.write({"name": "Acme"})
        self.assertFalse(self._logs(partner, "write"))

    def test_word_diff_for_long_text(self):
        partner = self.env["res.partner"].create({"name": "Acme", "comment": "<p>The quick brown fox jumps over the lazy dog</p>"})
        partner.comment = "<p>The quick red fox jumps over the lazy dog</p>"
        line = self._logs(partner, "write").line_ids
        self.assertIn('<del class="o_audit_del">brown</del>', line.diff_old_html)
        self.assertIn('<ins class="o_audit_ins">red</ins>', line.diff_new_html)

    def test_unlink_logged_with_record_name(self):
        partner = self.env["res.partner"].create({"name": "Doomed"})
        partner_id = partner.id
        partner.unlink()
        log = self.env["audit.log"].search([("res_id", "=", partner_id), ("operation", "=", "unlink")])
        self.assertEqual(log.record_name, "Doomed")
        self.assertIn("Doomed", log.line_ids.mapped("old_value"))

    def test_selected_fields_only(self):
        self.rule.write({
            "field_mode": "selected",
            "field_ids": [fields.Command.set(self.env["ir.model.fields"]._get("res.partner", "phone").ids)],
        })
        partner = self.env["res.partner"].create({"name": "Beta"})
        partner.name = "Beta 2"
        self.assertFalse(self._logs(partner, "write"))
        partner.phone = "123"
        self.assertEqual(self._logs(partner, "write").line_ids.field_name, "phone")

    def test_excluded_fields(self):
        self.rule.write({
            "field_mode": "exclude",
            "field_ids": [fields.Command.set(self.env["ir.model.fields"]._get("res.partner", "phone").ids)],
        })
        partner = self.env["res.partner"].create({"name": "Beta"})
        partner.phone = "123"
        self.assertFalse(self._logs(partner, "write"))

    def test_ignored_user(self):
        self.rule.excluded_user_ids = self.plain_user
        partner = self.env["res.partner"].with_user(self.plain_user).create({"name": "Ignored"})
        self.assertFalse(self._logs(partner))

    def test_plain_user_is_logged_but_cannot_read_logs(self):
        partner = self.env["res.partner"].with_user(self.plain_user).create({"name": "Plain"})
        partner.write({"phone": "1"})
        self.assertEqual(self._logs(partner).user_id, self.plain_user)
        with self.assertRaises(AccessError):
            self.env["audit.log"].with_user(self.plain_user).search([])

    def test_inactive_rule_stops_logging(self):
        self.rule.active = False
        partner = self.env["res.partner"].create({"name": "Off"})
        self.assertFalse(self._logs(partner))

    def test_alert_on_operation(self):
        self.rule.alert_unlink = True
        partner = self.env["res.partner"].create({"name": "Alerted"})
        partner.unlink()
        self.assertEqual(len(self._alerts()), 1)
        self.assertEqual(self._alerts().recipient_ids, self.manager.partner_id)

    def test_alert_only_on_selected_fields(self):
        self.rule.write({
            "alert_write": True,
            "alert_field_ids": [fields.Command.set(self.env["ir.model.fields"]._get("res.partner", "email").ids)],
        })
        partner = self.env["res.partner"].create({"name": "Watched"})
        partner.phone = "1"
        self.assertFalse(self._alerts(), "phone is not an alert field")
        partner.email = "x@example.com"
        self.assertEqual(len(self._alerts()), 1)

    def test_bulk_operation_summary(self):
        self.rule.batch_limit = 3
        partners = self.env["res.partner"].create([{"name": f"P{i}"} for i in range(5)])
        create_log = self.env["audit.log"].search([("is_bulk", "=", True), ("operation", "=", "create")])
        self.assertEqual(create_log.bulk_count, 5)
        partners.write({"phone": "555"})
        write_log = self.env["audit.log"].search([("is_bulk", "=", True), ("operation", "=", "write")])
        self.assertEqual(write_log.bulk_count, 5)
        self.assertEqual(write_log.line_ids.new_value, "555")
        self.assertFalse(write_log.line_ids.can_restore, "bulk lines cannot be restored")
        # small writes are still logged per record
        partners[:2].write({"phone": "556"})
        self.assertEqual(self.env["audit.log"].search_count([
            ("res_id", "in", partners[:2].ids), ("operation", "=", "write"),
        ]), 2)

    def test_restore_old_value(self):
        partner = self.env["res.partner"].create({"name": "Original", "category_id": [fields.Command.link(self.tag.id)]})
        partner.write({"name": "Changed", "category_id": [fields.Command.clear()]})
        lines = self._logs(partner, "write").line_ids
        self.assertTrue(all(lines.mapped("can_restore")))
        lines.with_user(self.manager).action_restore()
        self.assertEqual(partner.name, "Original")
        self.assertEqual(partner.category_id, self.tag)
        self.assertTrue(lines[0].restored_date)
        # the restore itself is audited
        self.assertEqual(len(self._logs(partner, "write")), 2)

    def test_restore_requires_manager(self):
        partner = self.env["res.partner"].create({"name": "Original"})
        partner.name = "Changed"
        line = self._logs(partner, "write").line_ids
        auditor = new_test_user(self.env, login="auditor", groups="base.group_user,agbe_audit_trail.group_audit_user")
        with self.assertRaises(AccessError):
            line.with_user(auditor).action_restore()

    def test_retention_purge(self):
        partner = self.env["res.partner"].create({"name": "Old"})
        log = self._logs(partner)
        log.date = fields.Datetime.now() - timedelta(days=self.rule.retention_days + 1)
        self.env["audit.rule"]._cron_purge_logs()
        self.assertFalse(log.exists())

    def test_history_action_menu_entry(self):
        self.rule.action_add_history_action()
        action = self.rule.history_action_id
        self.assertEqual(action.binding_model_id.model, "res.partner")
        self.rule.action_remove_history_action()
        self.assertFalse(action.exists())

    def test_blocked_model(self):
        with self.assertRaises(ValidationError):
            self.env["audit.rule"].create({
                "name": "Recursive",
                "model_id": self.env["ir.model"]._get("audit.log").id,
            })

    def test_export_excel(self):
        partner = self.env["res.partner"].create({"name": "Exported"})
        partner.phone = "1"
        wizard = self.env["audit.export.wizard"].create({"export_format": "xlsx"})
        action = wizard.action_export()
        self.assertEqual(action["type"], "ir.actions.act_url")
        attachment = self.env["ir.attachment"].search([("res_model", "=", wizard._name), ("res_id", "=", wizard.id)])
        self.assertTrue(attachment.datas)

    def test_export_selected_lines(self):
        partner = self.env["res.partner"].create({"name": "Selected"})
        line = self._logs(partner).line_ids[:1]
        wizard = self.env["audit.export.wizard"].with_context(
            active_model="audit.log.line", active_ids=line.ids,
        ).create({})
        self.assertEqual(wizard.log_ids, line.log_id)
