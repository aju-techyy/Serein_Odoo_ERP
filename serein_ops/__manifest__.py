{
    'name': 'Serein',
    'version': '19.0.1.4.0',
    'summary': 'Cloud seeding operations: weather monitoring, mission planning, fleet, and billing',
    'description': """
Plans and tracks cloud seeding missions for client contracts: live weather
monitoring and cloud alerts per target area, mission planning and readiness
checks, aircraft and crew, flare stock, maintenance, and invoicing.
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
        'data/serein_weather_data.xml',
        'data/serein_ai_data.xml',
        'views/seeding_base_views.xml',
        'views/seeding_target_area_views.xml',
        'views/seeding_contract_views.xml',
        'views/seeding_weather_snapshot_views.xml',
        'views/seeding_ai_log_views.xml',
        'views/seeding_mission_views.xml',
        'views/seeding_cloud_alert_views.xml',
        'views/maintenance_equipment_views.xml',
        'views/hr_employee_views.xml',
        'views/product_template_views.xml',
        'views/serein_menus.xml',
        'report/seeding_mission_report.xml',
    ],
    'application': True,
    'installable': True,
}
