{
    'name': 'Ask Ariane OS',
    'version': '18.0.0.1.0',
    'summary': 'Connect this Odoo to Ask Ariane for a free business diagnostic',
    'description': """
Connect your Odoo to Ask Ariane in a few clicks. An administrator ticks the
companies Ask Ariane may read, signs in on askariane.ai, and confirms inside
Odoo. Your Odoo then sends a small daily snapshot of counts (customers,
products, users, open quotations, open invoices, monthly sales totals) for
the approved companies only. No names, no free text, no customer records.

The key is made inside your Odoo; Ask Ariane holds only the public half.
Disconnect at any time, from Odoo or from askariane.ai.
""",
    'author': 'Ask Ariane',
    'maintainer': 'Ask Ariane',
    'website': 'https://askariane.ai',
    'support': 'support@askariane.ai',
    'license': 'LGPL-3',
    'category': 'Productivity',
    'depends': ['base', 'web'],
    'external_dependencies': {'python': ['cryptography', 'requests']},
    'data': [
        'security/ir.model.access.csv',
        'data/connect_cron.xml',
        'views/connect_views.xml',
        'views/confirm_templates.xml',
    ],
    'installable': True,
    'application': True,
}
