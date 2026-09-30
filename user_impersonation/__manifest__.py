{
    'name': 'Login as User',
    'version': '19.0.1.0.2',
    'category': 'Extra Tools',
    'summary': 'Work as any user without their password, with a full audit trail.',
    'description': """
Login as User
=============

Lets an administrator continue in another user's account without logging out
and without ever knowing that user's password - the way support teams need to
reproduce "it doesn't work for me" reports.

While logged in as someone else you have **their** rights, not your own: their
menus, their record rules, their companies. That is the point - you see exactly
what they see.

Built for people who have to answer for it afterwards
-----------------------------------------------------
* Every impersonation is recorded: who, whom, from which address, start and end.
* A banner is pinned to the screen for the whole session, so nobody forgets
  whose account they are working in.
* One click returns you to your own account.
* Nobody can impersonate a user with more rights than their own.
* The target user is never notified and never disturbed.
    """,
    'author': 'AGBE Technologies',
    'website': 'https://www.agbeindia.com',
    'license': 'LGPL-3',
    'support': 'support@agbeindia.com',
    'depends': ['base', 'web', 'mail'],
    'images': [
        'images/main_screenshot.png',
    ],
    'data': [
        'security/impersonation_groups.xml',
        'security/ir.model.access.csv',
        'views/impersonation_log_views.xml',
        'views/res_users_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'user_impersonation/static/src/impersonation_banner.scss',
            'user_impersonation/static/src/impersonation_banner.js',
            'user_impersonation/static/src/impersonation_banner.xml',
        ],
    },
    'installable': True,
    'application': False,
}
