from odoo import api, fields, models

# Key under which the impersonating user's id is kept in the HTTP session.
# Its presence is what makes a session an impersonation - not the current
# user's groups - so the way back out survives even when the account being
# impersonated has almost no rights.
IMPERSONATOR_SESSION_KEY = 'impersonator_uid'

# Id of the open log row, so returning can close the same row it opened.
IMPERSONATION_LOG_SESSION_KEY = 'impersonation_log_id'


class UserImpersonationLog(models.Model):
    _name = 'user.impersonation.log'
    _description = 'User Impersonation Log'
    _order = 'start_datetime desc, id desc'

    impersonator_id = fields.Many2one(
        'res.users', string='Logged In As Themselves',
        required=True, readonly=True, ondelete='restrict', index=True,
        help='The administrator who started this session.')
    target_user_id = fields.Many2one(
        'res.users', string='Worked As',
        required=True, readonly=True, ondelete='restrict', index=True,
        help='The user whose account was entered.')
    start_datetime = fields.Datetime(
        string='Started', required=True, readonly=True,
        default=fields.Datetime.now)
    end_datetime = fields.Datetime(
        string='Ended', readonly=True,
        help='Set when the administrator returns to their own account. '
             'Empty means the session was never returned from - it expired, '
             'the browser was closed, or it is still open.')
    duration_minutes = fields.Float(
        string='Duration (min)', compute='_compute_duration_minutes',
        store=True, readonly=True)
    ip_address = fields.Char(string='IP Address', readonly=True)
    # A hash, never the session id itself. The raw value is a credential:
    # anyone holding it holds the session.
    session_hash = fields.Char(
        string='Session Fingerprint', readonly=True, index=True,
        help='Fingerprint of the browser session, used to match the return '
             'to the right log entry. The session itself is never stored.')

    @api.depends('start_datetime', 'end_datetime')
    def _compute_duration_minutes(self):
        for log in self:
            if log.start_datetime and log.end_datetime:
                delta = log.end_datetime - log.start_datetime
                log.duration_minutes = delta.total_seconds() / 60.0
            else:
                log.duration_minutes = 0.0

    @api.depends('impersonator_id', 'target_user_id')
    def _compute_display_name(self):
        for log in self:
            log.display_name = '%s → %s' % (
                log.impersonator_id.name or '',
                log.target_user_id.name or '',
            )
