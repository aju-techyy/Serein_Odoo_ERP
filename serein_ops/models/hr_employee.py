from datetime import timedelta

from odoo import api, fields, models

from .maintenance_equipment import FLOWN_STATES

DUTY_WINDOW = timedelta(days=7)


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    is_pilot = fields.Boolean(
        string='Is Pilot', tracking=True,
        help="Flies seeding missions, as a crewed-aircraft pilot or as a drone operator.",
    )
    pilot_role = fields.Selection(
        [('pilot', 'Pilot (crewed aircraft)'), ('drone_operator', 'Drone operator')],
        string='Flying Role', tracking=True,
        help="Drone operators need no aviation medical.",
    )
    pilot_licence_number = fields.Char(string='Licence No.', tracking=True)
    licence_expiry = fields.Date(string='Licence Expiry', tracking=True)
    medical_expiry = fields.Date(
        string='Medical Expiry', tracking=True,
        help="Not needed for drone operators.",
    )
    seeding_mission_ids = fields.One2many('seeding.mission', 'pilot_id', string='Missions Flown')
    duty_hours_7d = fields.Float(
        string='Duty Hours (last 7 days)', compute='_compute_duty_hours_7d',
        help="Flight hours of completed missions that landed in the last 7 days.",
    )
    weekly_duty_limit = fields.Float(
        string='Weekly Duty Limit (h)', default=30.0, tracking=True,
        help="Maximum flight duty hours in any rolling 7 days, as set by the "
             "operator's flight time limitations.",
    )
    pilot_currency_id = fields.Many2one(
        'res.currency', compute='_compute_pilot_currency_id',
    )
    pilot_hourly_cost = fields.Monetary(
        string='Flying Hourly Cost', currency_field='pilot_currency_id', tracking=True,
    )

    _weekly_duty_limit_positive = models.Constraint(
        'check(weekly_duty_limit >= 0)',
        'The weekly duty limit cannot be negative.',
    )

    @api.depends('company_id')
    def _compute_pilot_currency_id(self):
        for employee in self:
            employee.pilot_currency_id = (employee.company_id or self.env.company).currency_id

    @api.depends('seeding_mission_ids.state', 'seeding_mission_ids.flight_hours',
                 'seeding_mission_ids.landing_at')
    def _compute_duty_hours_7d(self):
        since = fields.Datetime.now() - DUTY_WINDOW
        for employee in self:
            flown = employee.sudo().seeding_mission_ids.filtered(
                lambda m: m.state in FLOWN_STATES and m.landing_at and m.landing_at > since
            )
            employee.duty_hours_7d = round(sum(flown.mapped('flight_hours')), 2)

    @api.onchange('pilot_role')
    def _onchange_pilot_role(self):
        if self.pilot_role:
            self.is_pilot = True


class HrEmployeePublic(models.Model):
    _inherit = 'hr.employee.public'

    # Operators are not HR users. They see employees through this public
    # profile, and need these two to choose a pilot for a mission.
    is_pilot = fields.Boolean(readonly=True)
    pilot_role = fields.Selection(
        [('pilot', 'Pilot (crewed aircraft)'), ('drone_operator', 'Drone operator')],
        string='Flying Role', readonly=True,
    )
