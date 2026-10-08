from odoo import api, models


class Base(models.AbstractModel):
    """ Hook the audit trail into every model. The per-call cost for models
        without an audit rule is a single lookup in a cached dict. """
    _inherit = "base"

    def _audit_get_config(self, operation):
        if (
            self._transient
            or self._name.startswith("audit.")
            or self.env.context.get("audit_trail_disable")
            or self.env.context.get("install_mode")
            or "audit.rule" not in self.env.registry
        ):
            return None
        conf = self.env["audit.rule"]._get_audit_config().get(self._name)
        if not conf or operation not in conf["operations"] or self.env.uid in conf["excluded_user_ids"]:
            return None
        return conf

    def _audit_is_bulk(self, conf):
        return bool(conf["batch_limit"]) and len(self) > conf["batch_limit"]

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        conf = records._audit_get_config("create") if records else None
        if conf:
            Log = self.env["audit.log"]
            fnames = Log._audit_field_names(records, conf)
            if records._audit_is_bulk(conf):
                Log._audit_log_bulk(conf, "create", records)
            else:
                Log._audit_log(conf, "create", records, new=Log._audit_snapshot(records, fnames))
        return records

    def write(self, vals):
        conf = self._audit_get_config("write") if self else None
        if not conf:
            return super().write(vals)
        Log = self.env["audit.log"]
        fnames = Log._audit_field_names(self, conf, list(vals))
        if not fnames:
            return super().write(vals)
        if self._audit_is_bulk(conf):
            # mass edit: skip the per-record before/after read, log a summary
            res = super().write(vals)
            Log._audit_log_bulk(conf, "write", self, fnames, Log._audit_snapshot(self[:1], fnames).get(self[:1].id))
            return res
        old = Log._audit_snapshot(self, fnames)
        res = super().write(vals)
        Log._audit_log(conf, "write", self, old=old, new=Log._audit_snapshot(self, fnames))
        return res

    def unlink(self):
        conf = self._audit_get_config("unlink") if self else None
        if conf:
            # logged before the delete so the record name is still readable;
            # if the delete fails, the log is rolled back with it
            Log = self.env["audit.log"]
            records = self.exists()
            if records._audit_is_bulk(conf):
                Log._audit_log_bulk(conf, "unlink", records)
            else:
                fnames = Log._audit_field_names(records, conf)
                Log._audit_log(conf, "unlink", records, old=Log._audit_snapshot(records, fnames))
        return super().unlink()
