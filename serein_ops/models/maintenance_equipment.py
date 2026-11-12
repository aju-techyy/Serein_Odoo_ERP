from odoo import _, api, fields, models

# Missions whose flight hours count against the aircraft and the crew.
FLOWN_STATES = ('completed', 'billed')


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
    seeding_mission_ids = fields.One2many('seeding.mission', 'aircraft_id', string='Missions')
    seeding_mission_count = fields.Integer(compute='_compute_seeding_mission_count')
    last_service_at = fields.Datetime(
        string='Last Service', compute='_compute_last_service_at',
        help="When the most recent scheduled-service request of this aircraft was closed.",
    )
    hours_since_service = fields.Float(
        string='Hours Since Service', compute='_compute_hours_since_service',
        help="Flight hours of completed missions that landed after the last service.",
    )
    is_airworthy = fields.Boolean(
        string='Airworthy', compute='_compute_is_airworthy',
        help="No open corrective or scheduled-service request, and hours since "
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

    @api.depends('seeding_mission_ids')
    def _compute_seeding_mission_count(self):
        for equipment in self:
            equipment.seeding_mission_count = len(equipment.seeding_mission_ids)

    @api.depends(
        'maintenance_ids.is_scheduled_service', 'maintenance_ids.service_completed_at',
        'maintenance_ids.stage_id.done', 'maintenance_ids.archive',
    )
    def _compute_last_service_at(self):
        for equipment in self:
            done = equipment.maintenance_ids.filtered(
                lambda r: r.is_scheduled_service and r.stage_id.done
                and not r.archive and r.service_completed_at
            )
            equipment.last_service_at = max(done.mapped('service_completed_at'), default=False)

    @api.depends(
        'last_service_at', 'seeding_mission_ids.state',
        'seeding_mission_ids.flight_hours', 'seeding_mission_ids.landing_at',
    )
    def _compute_hours_since_service(self):
        for equipment in self:
            since = equipment.last_service_at
            flown = equipment.sudo().seeding_mission_ids.filtered(
                lambda m: m.state in FLOWN_STATES and m.landing_at
                and (not since or m.landing_at > since)
            )
            equipment.hours_since_service = round(sum(flown.mapped('flight_hours')), 2)

    @api.depends(
        'is_aircraft', 'service_interval_hours', 'hours_since_service',
        'maintenance_ids.maintenance_type', 'maintenance_ids.is_scheduled_service',
        'maintenance_ids.stage_id.done', 'maintenance_ids.archive',
    )
    def _compute_is_airworthy(self):
        for equipment in self:
            equipment.is_airworthy = (
                equipment.is_aircraft
                and not equipment._open_corrective_requests()
                and not equipment._open_service_requests()
                and equipment.hours_since_service < equipment.service_interval_hours
            )

    def _open_requests(self):
        self.ensure_one()
        return self.maintenance_ids.filtered(lambda r: not r.stage_id.done and not r.archive)

    def _open_corrective_requests(self):
        return self._open_requests().filtered(lambda r: r.maintenance_type == 'corrective')

    def _open_service_requests(self):
        return self._open_requests().filtered('is_scheduled_service')

    def _check_service_due(self):
        """Open a scheduled-service request for each aircraft that has reached
        its service interval and has none open yet. Returns the new requests."""
        Request = self.env['maintenance.request'].sudo()
        created = Request
        for aircraft in self.sudo().filtered('is_aircraft'):
            if aircraft.hours_since_service < aircraft.service_interval_hours:
                continue
            if aircraft._open_service_requests():
                continue
            hours = '%.1f' % aircraft.hours_since_service
            interval = '%.1f' % aircraft.service_interval_hours
            request = Request.create({
                'name': _("Scheduled service: %(aircraft)s at %(hours)s h", aircraft=aircraft.name, hours=hours),
                'equipment_id': aircraft.id,
                'maintenance_type': 'preventive',
                'is_scheduled_service': True,
                'maintenance_team_id': aircraft.maintenance_team_id.id or Request._get_default_team_id(),
                'user_id': aircraft.technician_user_id.id or False,
                'request_date': fields.Date.context_today(aircraft),
                'description': _(
                    "%(aircraft)s has flown %(hours)s h since its last service. The service "
                    "interval is %(interval)s h. The aircraft is grounded until this request is "
                    "closed, which resets the hours since service.",
                    aircraft=aircraft.name, hours=hours, interval=interval,
                ),
            })
            aircraft.message_post(body=_(
                "Service interval reached (%(hours)s of %(interval)s h). Request %(request)s opened.",
                hours=hours, interval=interval, request=request.name,
            ))
            created |= request
        return created

    def action_view_seeding_missions(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('serein_ops.seeding_mission_action')
        action['domain'] = [('aircraft_id', '=', self.id)]
        action['context'] = {'search_default_done': 1}
        action['view_mode'] = 'list,form'
        action['views'] = [(False, 'list'), (False, 'form')]
        return action

    @api.onchange('aircraft_type')
    def _onchange_aircraft_type(self):
        if self.aircraft_type:
            self.is_aircraft = True


class MaintenanceRequest(models.Model):
    _inherit = 'maintenance.request'

    is_scheduled_service = fields.Boolean(
        string='Scheduled Service', tracking=True,
        help="Closing this request records a completed service: the aircraft's "
             "hours since service start again from zero. While it is open, the "
             "aircraft is not airworthy.",
    )
    service_completed_at = fields.Datetime(
        string='Service Completed At', readonly=True, copy=False,
    )
    equipment_is_aircraft = fields.Boolean(related='equipment_id.is_aircraft')

    @api.model_create_multi
    def create(self, vals_list):
        requests = super().create(vals_list)
        requests._stamp_service_completion()
        return requests

    def write(self, vals):
        result = super().write(vals)
        if 'stage_id' in vals or 'is_scheduled_service' in vals:
            self._stamp_service_completion()
        return result

    def _stamp_service_completion(self):
        """A scheduled service that reaches a done stage records the time,
        which resets the aircraft's hours since service."""
        now = fields.Datetime.now()
        for request in self.filtered(lambda r: r.is_scheduled_service and r.stage_id.done):
            if request.service_completed_at:
                continue
            super(MaintenanceRequest, request).write({'service_completed_at': now})
            if request.equipment_id.is_aircraft:
                request.equipment_id.message_post(body=_(
                    "Scheduled service %s closed. Hours since service start again from zero.",
                    request.name,
                ))
        reopened = self.filtered(lambda r: r.service_completed_at and not r.stage_id.done)
        if reopened:
            super(MaintenanceRequest, reopened).write({'service_completed_at': False})
