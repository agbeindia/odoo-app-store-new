from odoo import SUPERUSER_ID
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.agbe_user_impersonation.models.impersonation_log import (
    IMPERSONATION_LOG_SESSION_KEY,
    IMPERSONATOR_SESSION_KEY,
)

from .common import mock_request


@tagged('post_install', '-at_install')
class TestImpersonation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.group = cls.env.ref('agbe_user_impersonation.group_user_impersonation')
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        cls.impersonator = Users.create({
            'name': 'Support Agent',
            'login': 'impersonation_support',
            'group_ids': [
                (4, cls.env.ref('base.group_user').id),
                (4, cls.group.id),
            ],
        })
        cls.target = Users.create({
            'name': 'Ravi',
            'login': 'impersonation_ravi',
            'group_ids': [(4, cls.env.ref('base.group_user').id)],
        })
        cls.outsider = Users.create({
            'name': 'No Rights Here',
            'login': 'impersonation_outsider',
            'group_ids': [(4, cls.env.ref('base.group_user').id)],
        })
        cls.settings_user = Users.create({
            'name': 'Settings Person',
            'login': 'impersonation_settings',
            'group_ids': [
                (4, cls.env.ref('base.group_user').id),
                (4, cls.env.ref('base.group_system').id),
            ],
        })

    def _as(self, user, record):
        return record.with_user(user)

    # ------------------------------------------------------------------
    # The happy path
    # ------------------------------------------------------------------
    def test_impersonate_swaps_the_session(self):
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id) as request:
            before = request.session.session_token
            result = self._as(self.impersonator, self.target).action_impersonate()

            self.assertEqual(request.session.uid, self.target.id)
            self.assertEqual(request.session.login, self.target.login)
            self.assertEqual(
                request.session[IMPERSONATOR_SESSION_KEY], self.impersonator.id,
                'The marker is what makes the way back out possible.')
            self.assertNotEqual(
                request.session.session_token, before,
                'A session token belonging to the previous user would be '
                'rejected on the next request.')
            self.assertTrue(
                request.session.should_rotate,
                'The session id must be re-issued so the token is recomputed '
                'against it before the response leaves.')
            self.assertEqual(
                result,
                {'type': 'ir.actions.act_url', 'url': '/web', 'target': 'self'},
                'Reloading the current page would reopen the Settings user '
                'form as the impersonated user.')

    def test_token_is_the_targets_own(self):
        """The stored token must hash the target's data, not the admin's.

        ``_compute_session_token`` is cached on the session id alone, so the
        naive call would hand back the impersonator's token and the session
        would be dropped the moment that cache was cleared.
        """
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id) as request:
            sid = request.session.sid
            # Warm the cache with the impersonator's token for this sid, the
            # way a normal incoming request does.
            self.impersonator._compute_session_token(sid)

            self._as(self.impersonator, self.target).action_impersonate()

            self.env.registry.clear_cache()
            expected = self.target._compute_session_token(sid)
            self.assertEqual(request.session.session_token, expected)

    def test_log_is_written(self):
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id, remote_addr='10.1.2.3') as request:
            self._as(self.impersonator, self.target).action_impersonate()
            log = self.env['user.impersonation.log'].browse(
                request.session[IMPERSONATION_LOG_SESSION_KEY])

            self.assertEqual(log.impersonator_id, self.impersonator)
            self.assertEqual(log.target_user_id, self.target)
            self.assertEqual(log.ip_address, '10.1.2.3')
            self.assertTrue(log.start_datetime)
            self.assertFalse(log.end_datetime)
            self.assertNotIn(
                request.session.sid, log.session_hash or '',
                'The raw session id is a credential and must never be stored.')

    def test_rights_are_the_targets_only(self):
        """Entering an account grants that account's rights, not a union."""
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id) as request:
            self._as(self.impersonator, self.target).action_impersonate()
            after = self.env['res.users'].browse(request.session.uid)

            self.assertTrue(self.impersonator.has_group(
                'agbe_user_impersonation.group_user_impersonation'))
            self.assertFalse(
                after.has_group('agbe_user_impersonation.group_user_impersonation'),
                'Ravi cannot impersonate, so the session must not either.')

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------
    def test_requires_the_group(self):
        env = self.env(user=self.outsider)
        with mock_request(env, uid=self.outsider.id):
            with self.assertRaises(AccessError):
                self._as(self.outsider, self.target).action_impersonate()

    def test_cannot_impersonate_self(self):
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id):
            with self.assertRaises(UserError):
                self._as(self.impersonator, self.impersonator).action_impersonate()

    def test_cannot_impersonate_archived(self):
        self.target.sudo().active = False
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id):
            with self.assertRaises(UserError):
                self._as(self.impersonator, self.target).action_impersonate()

    def test_cannot_impersonate_superuser(self):
        root = self.env['res.users'].browse(SUPERUSER_ID)
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id):
            with self.assertRaises(AccessError):
                self._as(self.impersonator, root).action_impersonate()

    def test_cannot_impersonate_public_user(self):
        public = self.env.ref('base.public_user', raise_if_not_found=False)
        if not public:
            self.skipTest('No public user in this database.')
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id):
            with self.assertRaises(UserError):
                self._as(self.impersonator, public).action_impersonate()

    def test_cannot_climb_to_more_rights(self):
        """A support agent must not reach Settings through somebody else."""
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id):
            with self.assertRaises(AccessError):
                self._as(self.impersonator, self.settings_user).action_impersonate()

    def test_cannot_nest_impersonations(self):
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id):
            self._as(self.impersonator, self.target).action_impersonate()
            with self.assertRaises(AccessError):
                self._as(self.impersonator, self.outsider).action_impersonate()

    def test_stateless_session_refused(self):
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id) as request:
            request.session.can_save = False
            with self.assertRaises(UserError):
                self._as(self.impersonator, self.target).action_impersonate()

    # ------------------------------------------------------------------
    # Button visibility follows the same rules
    # ------------------------------------------------------------------
    def test_available_flag_matches_the_guards(self):
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id):
            self.assertTrue(
                self._as(self.impersonator, self.target).impersonation_available)
            self.assertFalse(
                self._as(self.impersonator, self.impersonator).impersonation_available)
            self.assertFalse(
                self._as(self.impersonator, self.settings_user).impersonation_available)
            self.assertFalse(
                self._as(self.outsider, self.target).impersonation_available)

    # ------------------------------------------------------------------
    # Returning
    # ------------------------------------------------------------------
    def test_return_restores_the_session(self):
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id) as request:
            self._as(self.impersonator, self.target).action_impersonate()
            log_id = request.session[IMPERSONATION_LOG_SESSION_KEY]

            returned = self.env['res.users']._impersonation_return()

            self.assertTrue(returned)
            self.assertEqual(request.session.uid, self.impersonator.id)
            self.assertEqual(request.session.login, self.impersonator.login)
            self.assertNotIn(IMPERSONATOR_SESSION_KEY, request.session)
            self.assertNotIn(IMPERSONATION_LOG_SESSION_KEY, request.session)

            log = self.env['user.impersonation.log'].browse(log_id)
            self.assertTrue(log.end_datetime)
            self.assertGreaterEqual(log.duration_minutes, 0.0)

    def test_return_works_without_any_rights(self):
        """The impersonated account may be unable to read res.users at all."""
        portal = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Portal Person',
            'login': 'impersonation_portal',
            'group_ids': [(6, 0, [self.env.ref('base.group_portal').id])],
        })
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id) as request:
            self._as(self.impersonator, portal).action_impersonate()

            # From here on the request runs as the portal user.
            portal_env = self.env(user=portal)
            self.assertTrue(portal_env['res.users']._impersonation_return())
            self.assertEqual(request.session.uid, self.impersonator.id)

    def test_return_without_marker_is_refused(self):
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id):
            with self.assertRaises(UserError):
                self.env['res.users']._impersonation_return()

    def test_return_logs_out_if_impersonator_is_gone(self):
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id) as request:
            self._as(self.impersonator, self.target).action_impersonate()
            log_id = request.session[IMPERSONATION_LOG_SESSION_KEY]
            self.impersonator.sudo().active = False

            self.assertFalse(self.env['res.users']._impersonation_return())
            self.assertFalse(request.session.uid)

            log = self.env['user.impersonation.log'].browse(log_id)
            self.assertTrue(
                log.end_datetime,
                'The entry must still be closed, or the log would show a '
                'session that never ended.')

    # ------------------------------------------------------------------
    # The target is never disturbed
    # ------------------------------------------------------------------
    def test_audit_note_reaches_nobody(self):
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id):
            before = self.env['mail.message'].search([], order='id desc', limit=1)
            self._as(self.impersonator, self.target).action_impersonate()

            note = self.env['mail.message'].search([
                ('id', '>', before.id or 0),
                ('model', '=', 'res.partner'),
                ('res_id', '=', self.target.partner_id.id),
            ], limit=1)
            self.assertTrue(note, 'The impersonation should leave a trace.')
            self.assertEqual(note.subtype_id, self.env.ref('mail.mt_note'))
            self.assertFalse(
                note.notified_partner_ids,
                'The user must not be told that their account was entered.')
            self.assertIn(self.impersonator.name, note.body)

    # ------------------------------------------------------------------
    # The log is evidence, so it cannot be edited from the interface
    # ------------------------------------------------------------------
    def test_log_is_read_only_for_everyone(self):
        env = self.env(user=self.impersonator)
        with mock_request(env, uid=self.impersonator.id) as request:
            self._as(self.impersonator, self.target).action_impersonate()
            log_id = request.session[IMPERSONATION_LOG_SESSION_KEY]

        log = self.env['user.impersonation.log'].with_user(self.impersonator).browse(log_id)
        self.assertTrue(log.impersonator_id)  # readable
        with self.assertRaises(AccessError):
            log.write({'ip_address': '0.0.0.0'})
        with self.assertRaises(AccessError):
            log.unlink()
