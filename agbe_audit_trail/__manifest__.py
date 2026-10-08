{
    'name': 'Audit Trail & Field Level Change Tracking',
    'version': '19.0.1.1.0',
    'category': 'Extra Tools',
    'summary': 'Rule-based audit trail with a field-level diff viewer, record history, '
               'Excel and PDF export, and email alerts - see who changed what and when.',
    'description': """
Audit Trail & Field Level Change Tracking
==========================================
Track every create, update and delete on any Odoo model, down to the
individual field, without writing a line of code:

- Audit Rules: pick a model and choose which operations to log
  (Create / Update / Delete)
- Field-level control: log all fields, only selected fields, or all
  fields except an exclusion list
- Exclude technical/integration users from being audited
- Audit Logs list with one row per changed field and a "View Diff"
  popup: old value (red) vs. new value (green), with word-level
  highlighting for long text and HTML fields
- One-click "Restore Old Value": undo any field change straight from
  the log (Audit Managers only; the restore is itself audited)
- IP address and browser/device captured with every change
- Record history: one click from any audited record (Action menu) or
  from a log entry shows the full timeline of that record
- Human-readable values: relations, selections, dates in the user's
  timezone and booleans are stored as the user saw them, so history
  survives even after the related record is deleted
- Wizard-driven export to Excel (.xlsx) or PDF, filtered by period,
  model, user and operation - or export just the selected log lines
- Email alerts to chosen users when audited records are created,
  changed or deleted (one digest per transaction, not one per record),
  optionally only when specific fields change (price, credit limit...)
- Bulk-operation guard: mass edits, imports and bulk deletes above a
  configurable size write one summary log, keeping big jobs fast
- Per-rule retention period with an automatic daily purge
- Summary dashboard: graph and pivot of activity by day, model,
  user and operation
- Tamper-resistant: logs are read-only for everyone, only Audit
  Managers can purge them
- Two security levels: Auditor (read and export) and Audit Manager
  (configure rules)
""",
    'author': 'AGBE Technologies',
    'website': 'https://www.agbeindia.com',
    'license': 'LGPL-3',
    'images': ['static/description/banner.png'],
    'depends': ['base', 'mail'],
    'external_dependencies': {'python': ['xlsxwriter']},
    'data': [
        'security/audit_security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'report/audit_log_report.xml',
        'views/audit_rule_views.xml',
        'views/audit_log_views.xml',
        'wizard/audit_export_wizard_views.xml',
        'views/menus.xml',
    ],
    'demo': [],
    'assets': {},
    'installable': True,
    'application': True,
    'auto_install': False,
}
