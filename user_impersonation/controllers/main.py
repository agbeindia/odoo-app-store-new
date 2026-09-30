from odoo import http
from odoo.http import request


class UserImpersonationController(http.Controller):

    @http.route('/web/impersonation/return', type='http', auth='user',
                methods=['GET'], website=False)
    def impersonation_return(self, **kwargs):
        """Put the session back in the impersonator's account and reload.

        A plain link rather than an RPC call, for two reasons. The impersonated
        account may be a portal user with no rights on ``res.users``, so a model
        method reached through ``call_kw`` could be refused by ACLs - the one
        thing that must never happen is being unable to get out. And a full page
        load is what has to happen anyway once the session changes hands.

        The route carries no CSRF token on purpose: the only thing an attacker
        could achieve by forcing this request is to end an impersonation early,
        which takes rights away rather than granting them.
        """
        request.env['res.users']._impersonation_return()
        return request.redirect('/web')
