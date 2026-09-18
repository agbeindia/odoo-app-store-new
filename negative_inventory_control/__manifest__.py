{
    'name': "Negative Inventory Control",
    'summary': "Configurable Allow / Warn / Approval / Block policies for negative stock, with a full audit trail.",
    'description': """
Negative Inventory Control
===========================
Gives Inventory managers explicit, hierarchical control over what happens when
a stock operation (delivery, internal transfer, scrap, manufacturing
consumption, inventory adjustment, ...) would push on-hand quantity below
zero.

Configure a policy - Allow, Warn, Approval Required, or Block - at Company,
Location, Product Category, and Product level. The strictest applicable
setting always wins.

Every negative-stock-capable path is covered: picking-driven transfers
(delivery, internal transfer, manufacturing component pickings), scrap, and
inventory adjustments all detect, enforce, and (where configured) route
through an approval workflow with automatic resume. Every outcome - including
ones that were simply allowed - is written to a permanent, read-only audit
log. Requests and audit-log entries are scoped per company.

No core files are modified, and every policy defaults to Allow/inherit, so
installing this module changes nothing on an existing database until it is
configured.

See README.md for the full configuration hierarchy and design notes.
    """,
    'version': '19.0.2.1.0',
    'category': 'Inventory/Inventory',
    'author': 'AGBE Technologies',
    'website': 'https://www.agbeindia.com',
    'license': 'LGPL-3',
    'depends': ['stock', 'mail'],
    'images': [
        'images/main_screenshot.png',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/security.xml',
        'data/negative_inventory_data.xml',
        'views/res_config_settings_views.xml',
        'views/product_template_views.xml',
        'views/product_category_views.xml',
        'views/stock_location_views.xml',
        'views/negative_inventory_request_views.xml',
        'views/negative_inventory_log_views.xml',
        'views/stock_quant_views.xml',
    ],
    'installable': True,
    'application': False,
}
