{
    'name': 'Ariane Dialer',
    'version': '18.0.0.20.0',
    'summary': 'In-Odoo softphone with a live sales coach panel',
    'description': """
Click-to-call softphone in the systray, a customer screen pop, catalogue
search with price, stock, accessories and alternatives (with eCommerce) for the customer
on the line, add-to-quote, and a wrap-up dialog that logs the call.

The app is free. Telephony runs on your own Twilio account via the Ask
Ariane dialler service, which also serves the live coach; live
calling and the coach need an Ask Ariane account. Until the service is
configured in Settings, the panel works in preview mode: everything except
placing the call itself, and nothing is sent outside Odoo.
""",
    'author': 'Ask Ariane',
    'maintainer': 'Ask Ariane',
    'website': 'https://askariane.ai',
    'support': 'support@askariane.ai',
    'license': 'LGPL-3',
    'images': ['static/description/cover_screenshot.png'],
    'category': 'Sales/Sales',
    'depends': ['mail', 'sale_management', 'stock', 'crm'],
    'data': [
        'security/dialer_security.xml',
        'security/ir.model.access.csv',
        'data/dialer_cron.xml',
        'views/dialer_call_views.xml',
        'views/res_config_settings_views.xml',
        'views/product_template_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ariane_dialer/static/src/**/*',
        ],
    },
    'installable': True,
    'application': True,
}
