import hashlib

from odoo import SUPERUSER_ID, _, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from .impersonation_log import (
    IMPERSONATION_LOG_SESSION_KEY,
    IMPERSONATOR_SESSION_KEY,
)

IMPERSONATION_GROUP = 'agbe_user_impersonation.group_user_impersonation'


class ResUsers(models.Model):
    _inherit = 'res.users'

    impersonation_available = fields.Boolean(
        string='Can Be Logged In As',
        compute='_compute_impersonation_available',
        help='Technical field: whether the current user is allowed to '
             'continue their session in this account.')

    # ------------------------------------------------------------------
    # Button visibility
    # ------------------------------------------------------------------
    def _compute_impersonation_available(self):
        """Drive the form button from the same rules the server enforces.

        Computed rather than expressed as an ``invisible`` domain in the view
        so that there is exactly one definition of "who may be impersonated":
        :meth:`_check_impersonation_allowed`. A rule added there shows up in
        the interface automatically.
        """
        for user in self:
            try:
                user._check_impersonation_allowed()
            except (AccessError, UserError):
                user.impersonation_available = False
            else:
                user.impersonation_available = True

    # ------------------------------------------------------------------
    # Starting an impersonation
    # ------------------------------------------------------------------
    def action_impersonate(self):
        """Continue this browser session in ``self``'s account, no password.

        A normal login writes the user, the context and a session token into
        the session (``Session.finalize`` in ``odoo/http.py``). This does the
        same thing minus the credential check, which is the whole point of the
        feature: support staff have to reproduce a user's problem without ever
        holding that user's password.
        """
        self.ensure_one()
        self._check_impersonation_allowed()

        impersonator = self.env.user
        session = request.session

        # Both writes happen before the swap. Afterwards this request runs as
        # the target user, who normally has no rights on the audit log.
        log = self.env['user.impersonation.log'].sudo().create({
            'impersonator_id': impersonator.id,
            'target_user_id': self.id,
            'ip_address': request.httprequest.remote_addr,
            'session_hash': self._impersonation_session_hash(session.sid),
        })
        self._impersonation_post_audit_note(impersonator)

        self._impersonation_apply_session(self)
        session[IMPERSONATOR_SESSION_KEY] = impersonator.id
        session[IMPERSONATION_LOG_SESSION_KEY] = log.id

        # Land on the home page, not on whatever was open. The button is
        # pressed from a user form in Settings, and reloading that URL would
        # drop the impersonated user straight onto their own record in a
        # screen most of them are not allowed to be in. '/web' sends internal
        # users to the web client and portal users to their portal, so the
        # same line is right whoever was entered.
        return {'type': 'ir.actions.act_url', 'url': '/web', 'target': 'self'}

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------
    def _check_impersonation_allowed(self):
        """Refuse anything that is not a plain, downward impersonation.

        Raises rather than returning a boolean so the caller gets a message
        explaining which rule stopped them.
        """
        self.ensure_one()
        if not request:
            raise UserError(_(
                'Logging in as another user is only possible from the web '
                'interface.'))

        if not getattr(request.session, 'can_save', True):
            # Stateless API requests (X-Odoo-Database header) never write the
            # session back, so the swap would be silently lost.
            raise UserError(_(
                'This session cannot be changed. Open Odoo in a browser and '
                'try again.'))

        current = self.env.user

        if not current.has_group(IMPERSONATION_GROUP):
            # Hiding the button is presentation, not protection: /web/dataset
            # /call_kw reaches this method whatever the form shows.
            raise AccessError(_('You are not allowed to log in as another user.'))

        if request.session.get(IMPERSONATOR_SESSION_KEY):
            # Without this, the record of who is actually at the keyboard is
            # lost and one click of "Return" would only unwind a single step.
            raise AccessError(_(
                "You are already working in another user's account. Return to "
                "your own account before switching again."))

        if self.id == current.id:
            raise UserError(_('You are already logged in as yourself.'))

        if not self.active:
            raise UserError(_(
                '%s is archived. Restore the user before logging in as them.',
                self.name))

        if self.id == SUPERUSER_ID:
            # OdooBot is exempt from every access rule, so nothing done in such
            # a session could be meaningfully attributed or audited.
            raise AccessError(_('The system superuser cannot be logged in as.'))

        if self._is_public():
            raise UserError(_('The public user cannot be logged in as.'))

        # No climbing: entering an account must never hand out rights the
        # impersonator does not already hold.
        if self._is_system() and not current._is_system():
            raise AccessError(_(
                'You cannot log in as a user who has more rights than you do.'))
        if self._is_admin() and not current._is_admin():
            raise AccessError(_(
                'You cannot log in as a user who has more rights than you do.'))

    # ------------------------------------------------------------------
    # Returning
    # ------------------------------------------------------------------
    def _impersonation_return(self):
        """Give the session back to whoever started the impersonation.

        Deliberately *not* protected by a group. The account being impersonated
        may have almost no rights - a portal user, say - and it still has to be
        able to get out. What authorises the return is that this session was
        opened as an impersonation, which only an authorised user could have
        done; the impersonated account cannot set that marker itself.
        """
        if not request:
            raise UserError(_('No active web session.'))

        session = request.session
        impersonator_uid = session.get(IMPERSONATOR_SESSION_KEY)
        if not impersonator_uid:
            raise UserError(_('This session is not an impersonation.'))

        impersonator = self.env['res.users'].sudo().browse(impersonator_uid).exists()
        if not impersonator or not impersonator.active:
            # Archived or deleted while the session was open. Sending them to
            # the login screen is better than leaving them stranded inside
            # somebody else's account.
            self._impersonation_close_log()
            session.logout(keep_db=True)
            return False

        self._impersonation_close_log()
        self._impersonation_apply_session(impersonator)
        session.pop(IMPERSONATOR_SESSION_KEY, None)
        session.pop(IMPERSONATION_LOG_SESSION_KEY, None)
        return True

    def _impersonation_close_log(self):
        """Stamp the end time on the row this session opened."""
        log_id = request.session.get(IMPERSONATION_LOG_SESSION_KEY)
        if not log_id:
            return
        log = self.env['user.impersonation.log'].sudo().browse(log_id).exists()
        if log and not log.end_datetime:
            log.write({'end_datetime': fields.Datetime.now()})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _impersonation_apply_session(self, user):
        """Point the session at ``user``, exactly as a finished login would."""
        session = request.session
        user_sudo = user.sudo()
        target_env = self.env(user=user_sudo.id, su=False)
        session.update({
            'login': user_sudo.login,
            'uid': user_sudo.id,
            'context': dict(target_env['res.users'].context_get()),
            'session_token': self._impersonation_session_token(user_sudo, session.sid),
        })
        # Odoo re-issues the session id before the response leaves and
        # recomputes the token against the new one, so this survives rotation.
        session.should_rotate = True

    def _impersonation_session_token(self, user_sudo, sid):
        """Session token for ``user_sudo``, bypassing the shared token cache.

        ``_compute_session_token`` is an ``ormcache`` keyed on the session id
        alone - not on the user - because in normal operation one session id
        only ever belongs to one user. Impersonation is the exception: the id
        is still the impersonator's, so the cached entry would hand back the
        impersonator's token and the session would be dropped the moment the
        cache was cleared. Recomputing from the uncached helpers avoids it.
        """
        get_values = getattr(user_sudo, '_session_token_get_values', None)
        hash_compute = getattr(user_sudo, '_session_token_hash_compute', None)
        if get_values and hash_compute:
            return hash_compute(sid, get_values())
        return user_sudo._compute_session_token(sid)

    @staticmethod
    def _impersonation_session_hash(sid):
        """Fingerprint of a session id.

        The id itself is a credential - anyone holding it holds the session -
        so only its hash is ever written to the database.
        """
        return hashlib.sha256((sid or '').encode('utf-8')).hexdigest()

    def _impersonation_post_audit_note(self, impersonator):
        """Leave a trace on the target's contact record, quietly.

        An internal note with no recipients: no e-mail, no inbox item and no
        unread counter reaches the person whose account was entered.
        """
        self.ensure_one()
        partner = self.sudo().partner_id
        if not partner or not hasattr(partner, 'message_post'):
            return
        partner.message_post(
            body=_('%(admin)s logged in as this user.', admin=impersonator.name),
            subtype_xmlid='mail.mt_note',
            partner_ids=[],
        )
