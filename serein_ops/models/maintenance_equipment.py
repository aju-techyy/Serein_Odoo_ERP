from odoo import api, fields, models


class MaintenanceEquipment(models.Model):
    _inherit = 'maintenance.equipment'

    is_aircraft = fields.Boolean(string='Is Aircraft', tracking=True)
    aircraft_type = fields.Selection(
        [('crewed', 'Crewed aircraft'), ('drone', 'Drone')],
        string='Aircraft Type', tracking=True,
    )
    tail_number = fields.Char(tracking=True, copy=False)
    home_base_id = fields.Many2one('seeding.base', string='Home Base', tracking=True)
    rack_capacity = fields.Integer(
        string='Flare Rack Capacity', tracking=True,
        help="Most flares the aircraft can carry on one mission.",
    )
    service_interval_hours = fields.Float(
        string='Service Interval (h)', default=100.0, tracking=True,
        help="Flight hours allowed between scheduled services, as set by the "
             "maintenance programme for this aircraft.",
    )
    hours_since_service = fields.Float(
        string='Hours Since Service', compute='_compute_hours_since_service',
    )
    is_airworthy = fields.Boolean(
        string='Airworthy', compute='_compute_is_airworthy',
        help="No open corrective maintenance request, and hours since "
             "service below the service interval.",
    )
    aircraft_currency_id = fields.Many2one(
        'res.currency', compute='_compute_aircraft_currency_id',
    )
    aircraft_hourly_cost = fields.Monetary(
        string='Hourly Cost', currency_field='aircraft_currency_id', tracking=True,
    )

    _tail_number_uniq = models.Constraint(
        'unique (tail_number)',
        'Another aircraft already has this tail number.',
    )
    _rack_capacity_positive = models.Constraint(
        'check(rack_capacity >= 0)',
        'The flare rack capacity cannot be negative.',
    )
    _service_interval_positive = models.Constraint(
        'check(service_interval_hours >= 0)',
        'The service interval cannot be negative.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        # Without a team, maintenance requests opened from the aircraft start
        # with an empty (required) Team. Give aircraft the default team.
        default_aircraft = self.env.context.get('default_is_aircraft')
        for vals in vals_list:
            if vals.get('is_aircraft', default_aircraft) and not vals.get('maintenance_team_id'):
                vals['maintenance_team_id'] = self.env['maintenance.request']._get_default_team_id()
        return super().create(vals_list)

    @api.depends('company_id')
    def _compute_aircraft_currency_id(self):
        for equipment in self:
            equipment.aircraft_currency_id = (equipment.company_id or self.env.company).currency_id

    def _compute_hours_since_service(self):
        # Mission flight hours arrive in Phase 6. Until then no hours are flown.
        for equipment in self:
            equipment.hours_since_service = 0.0

    @api.depends(
        'is_aircraft', 'service_interval_hours',
        'maintenance_ids.maintenance_type', 'maintenance_ids.stage_id.done',
        'maintenance_ids.archive',
    )
    def _compute_is_airworthy(self):
        for equipment in self:
            equipment.is_airworthy = (
                equipment.is_aircraft
                and not equipment._open_corrective_requests()
                and equipment.hours_since_service < equipment.service_interval_hours
            )

    def _open_corrective_requests(self):
        self.ensure_one()
        return self.maintenance_ids.filtered(
            lambda r: r.maintenance_type == 'corrective'
            and not r.stage_id.done and not r.archive
        )

    @api.onchange('aircraft_type')
    def _onchange_aircraft_type(self):
        if self.aircraft_type:
            self.is_aircraft = True
