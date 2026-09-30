from odoo import models
from odoo.http import request

from .impersonation_log import IMPERSONATOR_SESSION_KEY


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    def session_info(self):
        """Tell the web client whether this session is an impersonation.

        Keyed off the session, not off the current user's groups. Ravi signing
        in normally and an administrator working inside Ravi's account are the
        same user as far as the ORM is concerned; only the session carries the
        marker, so only the administrator sees the banner.
        """
        info = super().session_info()
        impersonator_uid = request.session.get(IMPERSONATOR_SESSION_KEY)
        if not impersonator_uid:
            return info

        impersonator = self.env['res.users'].sudo().browse(impersonator_uid).exists()
        info['impersonation'] = {
            'active': True,
            'impersonator_id': impersonator_uid,
            'impersonator_name': impersonator.name or '',
            'target_name': self.env.user.name,
            'return_url': '/web/impersonation/return',
        }
        return info
