{
    'name': 'Serein',
    'version': '19.0.1.0.0',
    'summary': 'AI mission planning for cloud seeding operators',
    'description': """
Serein plans and tracks cloud seeding missions for paying clients.

AI proposes, rules verify, a person decides.
    """,
    'category': 'Services/Serein',
    'author': 'Serein team',
    'license': 'LGPL-3',
    'depends': [
        'contacts',
        'mail',
        'account',
        'stock',
        'purchase',
        'maintenance',
        'hr',
    ],
    'data': [
        'security/serein_security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'views/seeding_base_views.xml',
        'views/seeding_target_area_views.xml',
        'views/seeding_contract_views.xml',
        'views/maintenance_equipment_views.xml',
        'views/hr_employee_views.xml',
        'views/product_template_views.xml',
        'views/serein_menus.xml',
    ],
    'application': True,
    'installable': True,
}
