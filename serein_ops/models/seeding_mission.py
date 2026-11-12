from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Fields that define the plan. They can change only while the mission is
# proposed, and changing any of them re-runs the readiness checks.
PLANNING_FIELDS = {
    'alert_id', 'area_id', 'contract_id', 'base_id', 'aircraft_id', 'pilot_id',
    'flare_product_id', 'flares_planned', 'planned_takeoff',
}


class SeedingMission(models.Model):
    _name = 'seeding.mission'
    _description = 'Seeding Mission'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'planned_takeoff desc, id desc'

    name = fields.Char(
        string='Number', required=True, copy=False, readonly=True,
        default=lambda self: _('New'),
    )
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
    )
    state = fields.Selection(
        [
            ('proposed', 'Proposed'),
            ('ready', 'Ready'),
            ('launched', 'Launched'),
            ('completed', 'Completed'),
            ('billed', 'Billed'),
            ('cancelled', 'Cancelled'),
        ],
        string='Status', default='proposed', required=True, tracking=True, copy=False,
        group_expand='_expand_states',
    )
    planned_by = fields.Selection(
        [('ai', 'AI'), ('rules', 'Rules'), ('human', 'Operator')],
        string='Planned By', default='human', required=True, readonly=True, copy=False,
        tracking=True,
    )

    # The plan -------------------------------------------------------------
    alert_id = fields.Many2one(
        'seeding.cloud.alert', string='Cloud Alert', tracking=True, index=True,
        domain="[('state', 'in', ('new', 'planned'))]",
        help="The seeding opportunity this mission answers.",
    )
    area_id = fields.Many2one(
        'seeding.target.area', string='Target Area', required=True, tracking=True, index=True,
        compute='_compute_area_id', store=True, readonly=False, precompute=True,
    )
    contract_id = fields.Many2one(
        'seeding.contract', string='Contract', tracking=True, index=True,
        compute='_compute_contract_id', store=True, readonly=False, precompute=True,
    )
    partner_id = fields.Many2one(
        related='contract_id.partner_id', string='Client', store=True,
    )
    planned_takeoff = fields.Datetime(
        string='Planned Takeoff', tracking=True,
        compute='_compute_planned_takeoff', store=True, readonly=False, precompute=True,
    )
    aircraft_id = fields.Many2one(
        'maintenance.equipment', string='Aircraft', tracking=True, index=True,
        domain="[('is_aircraft', '=', True)]",
    )
    aircraft_type = fields.Selection(related='aircraft_id.aircraft_type')
    base_id = fields.Many2one(
        'seeding.base', string='Base', tracking=True,
        compute='_compute_base_id', store=True, readonly=False, precompute=True,
        help="Where the aircraft takes off and the flares are loaded.",
    )
    pilot_id = fields.Many2one(
        'hr.employee', string='Pilot', tracking=True, index=True,
        domain="[('is_pilot', '=', True)]",
    )
    flare_product_id = fields.Many2one(
        'product.product', string='Flare', tracking=True,
        domain="[('is_seeding_flare', '=', True)]",
        default=lambda self: self._default_flare_product(),
    )
    flares_planned = fields.Integer(string='Flares Planned', tracking=True)

    # Readiness ------------------------------------------------------------
    readiness_ids = fields.One2many(
        'seeding.readiness.check', 'mission_id', string='Readiness Checks', readonly=True,
    )
    readiness_state = fields.Selection(
        [('not_run', 'Not run'), ('pass', 'All checks passed'), ('fail', 'Checks failed')],
        string='Readiness', compute='_compute_readiness_state', store=True,
    )
    failed_check_count = fields.Integer(compute='_compute_readiness_state', store=True)
    readiness_checked_at = fields.Datetime(string='Checks Run At', readonly=True, copy=False)

    # The flight -----------------------------------------------------------
    takeoff_at = fields.Datetime(string='Takeoff', tracking=True, copy=False)
    landing_at = fields.Datetime(string='Landing', tracking=True, copy=False)
    flight_hours = fields.Float(
        string='Flight Hours', compute='_compute_flight_hours', store=True,
    )
    flares_fired = fields.Integer(string='Flares Fired', tracking=True, copy=False)
    note = fields.Html()

    _flares_planned_positive = models.Constraint(
        'check(flares_planned >= 0)',
        'Flares planned cannot be negative.',
    )
    _flares_fired_positive = models.Constraint(
        'check(flares_fired >= 0)',
        'Flares fired cannot be negative.',
    )
    _landing_after_takeoff = models.Constraint(
        'check(landing_at IS NULL OR takeoff_at IS NULL OR landing_at > takeoff_at)',
        'Landing must be after takeoff.',
    )

    # Defaults and computes --------------------------------------------------

    def _expand_states(self, states, domain):
        return [key for key, _label in self._fields['state'].selection]

    @api.model
    def _default_flare_product(self):
        return self.env['product.product'].search([('is_seeding_flare', '=', True)], limit=1)

    @api.depends('alert_id')
    def _compute_area_id(self):
        for mission in self:
            mission.area_id = mission.alert_id.area_id or mission.area_id

    @api.depends('area_id')
    def _compute_contract_id(self):
        for mission in self:
            if not mission.area_id or mission.area_id in mission.contract_id.area_ids:
                mission.contract_id = mission.contract_id
                continue
            contracts = self.env['seeding.contract'].search(
                [('area_ids', 'in', mission.area_id.ids), ('state', '!=', 'cancelled')],
            )
            # Prefer an active contract, then the most recent season.
            mission.contract_id = contracts.sorted(
                lambda c: (c.state == 'active', c.season_start), reverse=True,
            )[:1]

    @api.depends('alert_id')
    def _compute_planned_takeoff(self):
        now = fields.Datetime.now().replace(second=0, microsecond=0)
        for mission in self:
            if mission.alert_id and mission.alert_id.window_start > now:
                mission.planned_takeoff = mission.alert_id.window_start
            else:
                mission.planned_takeoff = mission.planned_takeoff or now

    @api.depends('aircraft_id')
    def _compute_base_id(self):
        for mission in self:
            mission.base_id = mission.aircraft_id.home_base_id or mission.base_id

    @api.depends('readiness_ids.result')
    def _compute_readiness_state(self):
        for mission in self:
            failed = mission.readiness_ids.filtered(lambda c: c.result == 'fail')
            mission.failed_check_count = len(failed)
            if not mission.readiness_ids:
                mission.readiness_state = 'not_run'
            else:
                mission.readiness_state = 'fail' if failed else 'pass'

    @api.depends('takeoff_at', 'landing_at')
    def _compute_flight_hours(self):
        for mission in self:
            if mission.takeoff_at and mission.landing_at and mission.landing_at > mission.takeoff_at:
                seconds = (mission.landing_at - mission.takeoff_at).total_seconds()
                mission.flight_hours = round(seconds / 3600.0, 2)
            else:
                mission.flight_hours = 0.0

    # CRUD -----------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('seeding.mission') or _('New')
        missions = super().create(vals_list)
        missions.alert_id.filtered(lambda a: a.state == 'new').write({'state': 'planned'})
        missions.filtered(lambda m: m.state == 'proposed')._run_readiness()
        flying = missions.filtered(lambda m: m.state in ('ready', 'launched'))
        if flying:
            flying._run_readiness()
            flying._ensure_ready_to_fly()
        return missions

    def write(self, vals):
        planning_change = PLANNING_FIELDS & set(vals)
        if planning_change:
            locked = self.filtered(lambda m: m.state != 'proposed')
            if locked:
                raise UserError(_(
                    "Mission %s is no longer proposed. Move it back to proposed "
                    "before changing its plan.", locked[0].name,
                ))
        if vals.get('state') in ('ready', 'launched'):
            self._ensure_ready_to_fly()
        old_alerts = self.alert_id if 'alert_id' in vals else self.env['seeding.cloud.alert']
        result = super().write(vals)
        if 'alert_id' in vals:
            self.alert_id.filtered(lambda a: a.state == 'new').write({'state': 'planned'})
            (old_alerts - self.alert_id)._release_after_missions()
        if planning_change:
            self._run_readiness()
        return result

    @api.ondelete(at_uninstall=False)
    def _unlink_only_proposed_or_cancelled(self):
        if any(m.state not in ('proposed', 'cancelled') for m in self):
            raise UserError(_("Only proposed or cancelled missions can be deleted. Cancel the mission instead."))

    def unlink(self):
        alerts = self.alert_id
        result = super().unlink()
        alerts.exists()._release_after_missions()
        return result

    # Transitions ----------------------------------------------------------

    def _ensure_ready_to_fly(self):
        """Server-side guard: nothing reaches ready or launched with a failed
        or missing readiness check, whichever way the status is written."""
        for mission in self:
            if mission.readiness_state != 'pass':
                failed = mission.readiness_ids.filtered(lambda c: c.result == 'fail')
                if failed:
                    detail = '\n'.join('• %s: %s' % (c.name, c.explanation) for c in failed)
                else:
                    detail = _("The readiness checks have not been run.")
                raise UserError(_(
                    "Mission %(mission)s cannot fly yet.\n%(detail)s",
                    mission=mission.name, detail=detail,
                ))

    def _blocked_notification(self, blocked, title):
        lines = []
        for mission in blocked:
            failed = mission.readiness_ids.filtered(lambda c: c.result == 'fail')
            lines.append(_(
                "%(mission)s: %(checks)s.",
                mission=mission.name, checks=', '.join(failed.mapped('name')),
            ))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': ' '.join(lines),
                'type': 'danger',
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def action_ready(self):
        if any(m.state != 'proposed' for m in self):
            raise UserError(_("Only a proposed mission can be marked ready."))
        # Re-run first: alerts expire and aircraft get grounded after saving.
        # The fresh results are kept even when the move is refused.
        self._run_readiness()
        blocked = self.filtered(lambda m: m.readiness_state != 'pass')
        (self - blocked).write({'state': 'ready'})
        if blocked:
            return self._blocked_notification(blocked, _("Not ready: readiness checks failed"))
        return True

    def action_launch(self):
        if any(m.state != 'ready' for m in self):
            raise UserError(_("Only a ready mission can be launched."))
        self._run_readiness()
        blocked = self.filtered(lambda m: m.readiness_state != 'pass')
        if blocked:
            blocked.write({'state': 'proposed'})
            for mission in blocked:
                mission.message_post(body=_(
                    "Launch refused: readiness checks failed. The mission is back to proposed.",
                ))
        now = fields.Datetime.now().replace(second=0, microsecond=0)
        for mission in self - blocked:
            mission.write({'state': 'launched', 'takeoff_at': mission.takeoff_at or now})
        if blocked:
            return self._blocked_notification(blocked, _("Launch refused: readiness checks failed"))
        return True

    def action_complete(self):
        now = fields.Datetime.now().replace(second=0, microsecond=0)
        for mission in self:
            if mission.state != 'launched':
                raise UserError(_("Only a launched mission can be completed."))
            if mission.flares_fired > mission.flares_planned:
                raise UserError(_(
                    "%(mission)s fired %(fired)s flares but only %(planned)s were loaded.",
                    mission=mission.name, fired=mission.flares_fired, planned=mission.flares_planned,
                ))
            landing = mission.landing_at or now
            if landing <= mission.takeoff_at:
                raise UserError(_("Landing must be after takeoff on %s.", mission.name))
            mission.write({'state': 'completed', 'landing_at': landing})
        return True

    def action_cancel(self):
        if any(m.state not in ('proposed', 'ready') for m in self):
            raise UserError(_("Only a proposed or ready mission can be cancelled."))
        self.write({'state': 'cancelled'})
        self.alert_id._release_after_missions()
        return True

    def action_reset_to_proposed(self):
        if any(m.state not in ('ready', 'cancelled') for m in self):
            raise UserError(_("Only a ready or cancelled mission can go back to proposed."))
        self.write({'state': 'proposed'})
        self.alert_id.filtered(lambda a: a.state == 'new').write({'state': 'planned'})
        self._run_readiness()
        return True

    def action_rerun_checks(self):
        if any(m.state not in ('proposed', 'ready') for m in self):
            raise UserError(_("Checks are run only on proposed or ready missions."))
        self._run_readiness()
        dropped = self.filtered(lambda m: m.state == 'ready' and m.readiness_state != 'pass')
        if dropped:
            dropped.write({'state': 'proposed'})
            for mission in dropped:
                mission.message_post(body=_(
                    "Readiness checks failed on re-run. The mission is back to proposed.",
                ))
        return True
