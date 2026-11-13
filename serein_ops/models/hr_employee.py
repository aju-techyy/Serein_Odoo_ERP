from datetime import timedelta

from odoo import api, fields, models

from .seeding_mission import FLOWN_STATES

DUTY_WINDOW = timedelta(days=7)
PILOT_ROLES = [('pilot', 'Pilot (crewed aircraft)'), ('drone_operator', 'Drone operator')]
# A cleared pilot is flagged "expiring soon" this many days before a licence
# or medical runs out, or once this share of the weekly duty limit is flown.
EXPIRY_WARNING_DAYS = 30
DUTY_WARNING_SHARE = 0.8


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    is_pilot = fields.Boolean(
        string='Is Pilot', tracking=True,
        help="Flies seeding missions, as a crewed-aircraft pilot or as a drone operator.",
    )
    pilot_role = fields.Selection(
        PILOT_ROLES,
        string='Flying Role', tracking=True,
        help="Drone operators need no aviation medical.",
    )
    pilot_licence_number = fields.Char(
        string='Licence No.', tracking=True,
        help="Licence held by the pilot or drone operator.",
    )
    licence_expiry = fields.Date(
        string='Licence Expiry', tracking=True,
        help="Last valid day. Missions after it fail the pilot licence check.",
    )
    home_base_id = fields.Many2one(
        'seeding.base', string='Home Base', tracking=True, index=True,
        help="Base the pilot or drone operator flies from. They fly only that base's aircraft. "
             "Without a home base, they may fly from any base.",
    )
    medical_expiry = fields.Date(
        string='Medical Expiry', tracking=True,
        help="Not needed for drone operators.",
    )
    seeding_mission_ids = fields.One2many('seeding.mission', 'pilot_id', string='Missions Flown')
    seeding_mission_count = fields.Integer(compute='_compute_seeding_mission_count')
    duty_hours_7d = fields.Float(
        string='Duty Hours (last 7 days)', compute='_compute_duty_hours_7d',
        help="Flight hours of completed missions that landed in the last 7 days.",
    )
    weekly_duty_limit = fields.Float(
        string='Weekly Duty Limit (h)', default=30.0, tracking=True,
        help="Maximum flight duty hours in any rolling 7 days, as set by the "
             "operator's flight time limitations.",
    )
    flying_status = fields.Selection(
        [('ok', 'Cleared'), ('expiring', 'Expiring Soon'), ('grounded', 'Not Cleared')],
        string='Status', compute='_compute_flying_status',
        help="Not Cleared: licence or medical expired, or the weekly duty limit reached. "
             "Expiring Soon: one of them runs out within 30 days, or 80 % of the duty limit is flown.",
    )
    pilot_currency_id = fields.Many2one(
        'res.currency', compute='_compute_pilot_currency_id',
    )
    pilot_hourly_cost = fields.Monetary(
        string='Flying Hourly Cost', currency_field='pilot_currency_id', tracking=True,
        help="Cost of one flight hour of this crew member. Used for mission cost.",
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
        now = fields.Datetime.now()
        for employee in self:
            employee.duty_hours_7d = employee._duty_hours_until(now)

    def _duty_hours_until(self, moment):
        """Flight hours of flown missions that landed in the 7 days up to `moment`."""
        self.ensure_one()
        since = moment - DUTY_WINDOW
        flown = self.sudo().seeding_mission_ids.filtered(
            lambda m: m.state in FLOWN_STATES and m.landing_at and since < m.landing_at <= moment
        )
        return round(sum(flown.mapped('flight_hours')), 2)

    @api.depends('pilot_role', 'licence_expiry', 'medical_expiry', 'duty_hours_7d', 'weekly_duty_limit')
    def _compute_flying_status(self):
        today = fields.Date.context_today(self)
        warn_until = today + timedelta(days=EXPIRY_WARNING_DAYS)
        for employee in self:
            expiries = [employee.licence_expiry]
            if employee.pilot_role != 'drone_operator':
                expiries.append(employee.medical_expiry)
            duty, limit = employee.duty_hours_7d, employee.weekly_duty_limit
            if any(not d or d < today for d in expiries) or duty >= limit:
                employee.flying_status = 'grounded'
            elif any(d <= warn_until for d in expiries) or duty >= DUTY_WARNING_SHARE * limit:
                employee.flying_status = 'expiring'
            else:
                employee.flying_status = 'ok'

    @api.depends('seeding_mission_ids')
    def _compute_seeding_mission_count(self):
        for employee in self:
            employee.seeding_mission_count = len(employee.sudo().seeding_mission_ids)

    def action_view_seeding_missions(self):
        self.ensure_one()
        return self.env['seeding.mission']._open_missions([('pilot_id', '=', self.id)], history=True)

    @api.onchange('pilot_role')
    def _onchange_pilot_role(self):
        if self.pilot_role:
            self.is_pilot = True


class HrEmployeePublic(models.Model):
    _inherit = 'hr.employee.public'

    # Operators are not HR users. They see employees through this public
    # profile, and need these to choose a pilot for a mission.
    is_pilot = fields.Boolean(readonly=True)
    home_base_id = fields.Many2one('seeding.base', string='Home Base', readonly=True)
    pilot_role = fields.Selection(
        PILOT_ROLES,
        string='Flying Role', readonly=True,
    )
