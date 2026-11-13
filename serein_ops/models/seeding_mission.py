import json
from datetime import timedelta

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare

from .weather_service import RAINFALL_SOURCE

# Observed rainfall covers the takeoff hour to this many hours after the landing hour.
# Missions that have flown: their hours count against the contract, the
# aircraft's service interval, and the crew's duty.
FLOWN_STATES = ('completed', 'billed')
RAINFALL_HOURS_AFTER_LANDING = 24

# Checks a different aircraft, pilot, or flare load can fix (5 to 11).
# Checks 1 to 4 depend on the contract and the alert.
ASSET_CHECKS = {
    'aircraft_airworthy', 'aircraft_free', 'rack_capacity',
    'pilot_licence', 'pilot_medical', 'duty_hours', 'flares_in_stock',
}

TEXT_SOURCES = [('ai', 'AI'), ('template', 'Mission record template')]

# Fields that define the plan. They can change only while the mission is
# proposed, and changing any of them re-runs the readiness checks.
PLANNING_FIELDS = {
    'alert_id', 'area_id', 'contract_id', 'base_id', 'aircraft_id', 'pilot_id',
    'flare_product_id', 'flares_planned', 'planned_takeoff', 'launch_by',
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
        help="Who proposed the aircraft, crew, and load: AI planner, rule-based planner, or operator.",
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
        help="Planned takeoff time. Filled from the alert window when planned from an alert.",
    )
    aircraft_id = fields.Many2one(
        'maintenance.equipment', string='Aircraft', tracking=True, index=True,
        domain="[('is_aircraft', '=', True)] + (['|', ('home_base_id.area_ids', '=', False), "
               "('home_base_id.area_ids', 'in', [area_id])] if area_id else [])",
    )
    aircraft_type = fields.Selection(related='aircraft_id.aircraft_type')
    base_id = fields.Many2one(
        'seeding.base', string='Base', tracking=True,
        compute='_compute_base_id', store=True, readonly=False, precompute=True,
        help="Where the aircraft takes off and the flares are loaded.",
    )
    pilot_id = fields.Many2one(
        'hr.employee', string='Pilot', tracking=True, index=True,
        domain="[('is_pilot', '=', True)] + ([('home_base_id', 'in', [base_id, False])] if base_id else [])",
    )
    flare_product_id = fields.Many2one(
        'product.product', string='Flare', tracking=True,
        domain="[('is_seeding_flare', '=', True)]",
        default=lambda self: self._default_flare_product(),
    )
    flares_planned = fields.Integer(
        string='Flares Planned', tracking=True,
        help="Flares to load. They must fit the aircraft's rack and be in stock at the base.",
    )
    launch_by = fields.Datetime(
        string='Launch By', tracking=True,
        help="Latest takeoff that still reaches the seedable window.",
    )

    # Planner --------------------------------------------------------------
    plan_reasoning = fields.Text(string='Reasoning', readonly=True, copy=False)
    plan_risks = fields.Text(string='Risks', readonly=True, copy=False)
    fallback_reason = fields.Char(
        string='Fallback Reason', readonly=True, copy=False,
        help="Why the rule-based planner made this plan instead of the AI planner.",
    )
    replan_count = fields.Integer(string='Re-plan Rounds', readonly=True, copy=False)
    can_replan = fields.Boolean(compute='_compute_can_replan')
    ai_log_ids = fields.One2many('seeding.ai.log', 'mission_id', string='AI Calls')
    ai_log_count = fields.Integer(compute='_compute_ai_log_count')

    # Readiness ------------------------------------------------------------
    readiness_ids = fields.One2many(
        'seeding.readiness.check', 'mission_id', string='Readiness Checks', readonly=True,
    )
    readiness_state = fields.Selection(
        [('not_run', 'Not run'), ('pass', 'All checks passed'), ('fail', 'Checks failed')],
        string='Readiness', compute='_compute_readiness_state', store=True,
        help="Result of the latest run of the 11 readiness checks.",
    )
    failed_check_count = fields.Integer(compute='_compute_readiness_state', store=True)
    readiness_checked_at = fields.Datetime(string='Checks Run At', readonly=True, copy=False)

    # The flight -----------------------------------------------------------
    takeoff_at = fields.Datetime(
        string='Takeoff', tracking=True, copy=False,
        help="Actual takeoff time, recorded on Launch.",
    )
    landing_at = fields.Datetime(
        string='Landing', tracking=True, copy=False,
        help="Actual landing time, recorded on Complete.",
    )
    flight_hours = fields.Float(
        string='Flight Hours', compute='_compute_flight_hours', store=True,
        help="Landing time minus takeoff time.",
    )
    flares_fired = fields.Integer(
        string='Flares Fired', tracking=True, copy=False,
        help="Flares actually fired. On Complete they leave the base stock by batch.",
    )
    material_released_g = fields.Float(
        string='Material Released (g)', compute='_compute_material_released', store=True,
        digits=(12, 1), help="Flares fired x grams of seeding material per flare.",
    )
    picking_id = fields.Many2one(
        'stock.picking', string='Flare Transfer', readonly=True, copy=False,
        help="The validated transfer that moved the flares fired out of the base, by batch.",
    )
    flare_batches = fields.Char(
        string='Flare Batches', compute='_compute_flare_batches',
        help="Batches of the flares fired, with the number taken from each.",
    )
    observed_rainfall_mm = fields.Float(
        string='Observed Rainfall (mm)', readonly=True, copy=False, digits=(8, 1),
        help="Rainfall observed over the target area after the flight.",
    )
    rainfall_recorded_at = fields.Datetime(string='Rainfall Recorded At', readonly=True, copy=False)
    rainfall_period_start = fields.Datetime(
        string='Rainfall From', readonly=True, copy=False,
        help="Start of the hours the observed rainfall covers: the takeoff hour.",
    )
    rainfall_period_end = fields.Datetime(
        string='Rainfall To', readonly=True, copy=False,
        help="End of the hours the observed rainfall covers: %s hours after the landing hour."
             % RAINFALL_HOURS_AFTER_LANDING,
    )
    rainfall_source = fields.Char(string='Rainfall Source', readonly=True, copy=False)
    rainfall_raw_response = fields.Text(
        string='Rainfall Raw Response', readonly=True, copy=False,
        help="The archive's full answer the rainfall was summed from.",
    )
    note = fields.Html()

    # Cost and billing -------------------------------------------------------
    currency_id = fields.Many2one(related='company_id.currency_id')
    flare_cost = fields.Monetary(
        string='Flare Cost', compute='_compute_costs', store=True,
        help="Flares fired x the flare's cost.",
    )
    aircraft_cost = fields.Monetary(
        string='Aircraft Cost', compute='_compute_costs', store=True,
        help="Flight hours x the aircraft's hourly cost.",
    )
    crew_cost = fields.Monetary(
        string='Crew Cost', compute='_compute_costs', store=True,
        help="Flight hours x the pilot's flying hourly cost.",
    )
    mission_cost = fields.Monetary(
        string='Mission Cost', compute='_compute_costs', store=True,
        help="Flare, aircraft, and crew cost of the flight, at the costs in force when the "
             "flight was recorded.",
    )
    invoice_id = fields.Many2one(
        'account.move', string='Invoice', readonly=True, copy=False, index='btree_not_null',
        help="The customer invoice that billed this mission's flight hours.",
    )

    # Crew briefing (drafted when the mission becomes ready) ----------------
    briefing = fields.Text(string='Crew Briefing', copy=False)
    briefing_source = fields.Selection(
        TEXT_SOURCES, string='Briefing Drafted By', readonly=True, copy=False,
    )
    briefing_model = fields.Char(string='Briefing Model', readonly=True, copy=False)
    briefing_fallback_reason = fields.Char(readonly=True, copy=False)
    briefing_drafted_at = fields.Datetime(string='Briefing Drafted At', readonly=True, copy=False)
    briefing_reviewed_by = fields.Many2one(
        'res.users', string='Briefing Reviewed By', readonly=True, copy=False,
    )
    briefing_reviewed_at = fields.Datetime(string='Briefing Reviewed At', readonly=True, copy=False)
    briefing_note = fields.Char(compute='_compute_text_notes')

    # Client summary (drafted on completion, approved before the report) ---
    client_summary = fields.Text(string='Client Summary', copy=False)
    summary_source = fields.Selection(
        TEXT_SOURCES, string='Summary Drafted By', readonly=True, copy=False,
    )
    summary_model = fields.Char(string='Summary Model', readonly=True, copy=False)
    summary_fallback_reason = fields.Char(readonly=True, copy=False)
    summary_drafted_at = fields.Datetime(string='Summary Drafted At', readonly=True, copy=False)
    summary_reviewed_by = fields.Many2one(
        'res.users', string='Summary Approved By', readonly=True, copy=False, tracking=True,
    )
    summary_reviewed_at = fields.Datetime(string='Summary Approved At', readonly=True, copy=False)
    summary_state = fields.Selection(
        [('none', 'Not drafted'), ('to_review', 'To review'), ('approved', 'Approved')],
        string='Summary', compute='_compute_summary_state', store=True,
    )
    summary_note = fields.Char(compute='_compute_text_notes')

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

    @api.depends('state', 'alert_id', 'replan_count', 'readiness_ids.result', 'readiness_ids.code')
    def _compute_can_replan(self):
        max_rounds = self.env['seeding.mission.planner'].max_replans()
        for mission in self:
            mission.can_replan = (
                mission.state == 'proposed' and bool(mission.alert_id)
                and mission.replan_count < max_rounds
                and bool(mission._replannable_failures())
            )

    def _replannable_failures(self):
        """Failed checks that a different aircraft, pilot, or flare load can fix."""
        self.ensure_one()
        return self.readiness_ids.filtered(lambda c: c.result == 'fail' and c.code in ASSET_CHECKS)

    @api.depends('ai_log_ids')
    def _compute_ai_log_count(self):
        counts = dict(self.env['seeding.ai.log'].sudo()._read_group(
            [('mission_id', 'in', self.ids)], ['mission_id'], ['__count'],
        )) if self.ids else {}
        for mission in self:
            mission.ai_log_count = counts.get(mission._origin, 0)

    @api.depends('takeoff_at', 'landing_at')
    def _compute_flight_hours(self):
        for mission in self:
            if mission.takeoff_at and mission.landing_at and mission.landing_at > mission.takeoff_at:
                seconds = (mission.landing_at - mission.takeoff_at).total_seconds()
                mission.flight_hours = round(seconds / 3600.0, 2)
            else:
                mission.flight_hours = 0.0

    @api.depends('flares_fired', 'flare_product_id.grams_per_flare')
    def _compute_material_released(self):
        for mission in self:
            mission.material_released_g = mission.flares_fired * mission.flare_product_id.grams_per_flare

    # Rates are read when the flight figures change, and are deliberately not
    # dependencies: a later change of a rate does not rewrite past missions.
    @api.depends('flight_hours', 'flares_fired', 'aircraft_id', 'pilot_id', 'flare_product_id')
    def _compute_costs(self):
        for mission in self:
            sudo = mission.sudo()
            flare = sudo.flare_product_id.with_company(mission.company_id)
            currency = mission.company_id.currency_id
            mission.flare_cost = currency.round(mission.flares_fired * flare.standard_price)
            mission.aircraft_cost = currency.round(mission.flight_hours * sudo.aircraft_id.aircraft_hourly_cost)
            mission.crew_cost = currency.round(mission.flight_hours * sudo.pilot_id.pilot_hourly_cost)
            mission.mission_cost = mission.flare_cost + mission.aircraft_cost + mission.crew_cost

    @api.depends('picking_id.move_line_ids.lot_id', 'picking_id.move_line_ids.quantity')
    def _compute_flare_batches(self):
        for mission in self:
            lines = mission._flare_batch_lines()
            mission.flare_batches = ', '.join(
                '%s x %s' % (line['batch'], line['flares']) for line in lines
            ) or False

    def _flare_batch_lines(self):
        """Batches of the flares fired, as [{'batch', 'flares'}], from the
        validated release transfer. Read as superuser: operators and the
        report need them without stock rights."""
        self.ensure_one()
        picking = self.sudo().picking_id
        if not picking or picking.state != 'done':
            return []
        flares = {}
        for line in picking.move_line_ids.filtered(lambda l: l.lot_id and l.product_id == self.sudo().flare_product_id):
            flares[line.lot_id.name] = flares.get(line.lot_id.name, 0) + line.quantity
        return [
            {'batch': name, 'flares': int(qty) if qty == int(qty) else qty}
            for name, qty in sorted(flares.items())
        ]

    @api.depends('client_summary', 'summary_reviewed_by')
    def _compute_summary_state(self):
        for mission in self:
            if mission.summary_reviewed_by:
                mission.summary_state = 'approved'
            elif mission.client_summary:
                mission.summary_state = 'to_review'
            else:
                mission.summary_state = 'none'

    @api.depends('briefing_source', 'briefing_model', 'briefing_drafted_at', 'briefing_reviewed_by',
                 'briefing_reviewed_at', 'summary_source', 'summary_model', 'summary_drafted_at',
                 'summary_reviewed_by', 'summary_reviewed_at')
    def _compute_text_notes(self):
        for mission in self:
            mission.briefing_note = mission._text_note(
                mission.briefing_source, mission.briefing_model, mission.briefing_drafted_at,
                mission.briefing_reviewed_by, mission.briefing_reviewed_at, _("Reviewed"),
            )
            mission.summary_note = mission._text_note(
                mission.summary_source, mission.summary_model, mission.summary_drafted_at,
                mission.summary_reviewed_by, mission.summary_reviewed_at, _("Reviewed and approved"),
            )

    def _text_note(self, source, model, drafted_at, reviewer, reviewed_at, reviewed_label):
        """Who wrote a text and who reviewed it, in one sentence."""
        if not source:
            return False
        if source == 'ai':
            drafted = _("AI-drafted (%(model)s) on %(when)s.", model=model or _("language model"),
                        when=self._utc_text(drafted_at))
        else:
            drafted = _("Written from the mission record on %s.", self._utc_text(drafted_at))
        if reviewer:
            review = _("%(label)s by %(name)s on %(when)s.", label=reviewed_label,
                       name=reviewer.name, when=self._utc_text(reviewed_at))
        else:
            review = _("Not yet reviewed.")
        return '%s %s' % (drafted, review)

    @api.model
    def _utc_text(self, moment):
        """A stored (UTC) datetime as shown on the report and in the texts."""
        return '%s UTC' % moment.strftime('%d %b %Y %H:%M') if moment else ''

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
        self._check_text_locks(vals)
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

    def _check_text_locks(self, vals):
        """A reviewed briefing or an approved summary cannot be edited until
        its review is reopened (the writer resets the reviewer itself)."""
        if 'briefing' in vals and vals.get('briefing_reviewed_by', True):
            if self.filtered('briefing_reviewed_by'):
                raise UserError(_("The crew briefing has been reviewed and can no longer be edited."))
        if 'client_summary' in vals and vals.get('summary_reviewed_by', True):
            if self.filtered('summary_reviewed_by'):
                raise UserError(_(
                    "The client summary has been approved. Reopen it before editing.",
                ))

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
        ready = self - blocked
        ready.write({'state': 'ready'})
        # The briefing never blocks the move: on any AI problem the writer
        # falls back to the template.
        writer = self.env['seeding.mission.writer']
        for mission in ready:
            writer.write_briefing(mission)
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
            mission._release_fired_flares()
            mission.aircraft_id._check_service_due()
            self.env['seeding.mission.writer'].write_summary(mission)
        return True

    def _release_fired_flares(self):
        """Create and validate the transfer of the flares fired, out of the
        base's stock location into "Released to Atmosphere". Batches are
        taken first in, first out. Refuses (and so blocks completion) if the
        base cannot supply them. Done as superuser: operators complete
        missions without inventory rights."""
        self.ensure_one()
        if self.flares_fired <= 0 or self.picking_id:
            return self.picking_id
        mission = self.sudo()
        product = mission.flare_product_id
        location = mission.base_id.location_id
        if not product:
            raise UserError(_("%s has no flare product, so the flares fired cannot leave stock.", self.name))
        if not location:
            raise UserError(_("%s has no base stock location, so the flares fired cannot leave stock.", self.name))
        warehouse = location.warehouse_id
        if not warehouse:
            raise UserError(_(
                "The stock location of base %s is not inside a warehouse, so the flares fired "
                "cannot leave stock.", mission.base_id.name,
            ))
        company = mission.company_id
        destination = company._seeding_release_location()
        picking = self.env['stock.picking'].sudo().create({
            'picking_type_id': company._seeding_release_picking_type(warehouse).id,
            'location_id': location.id,
            'location_dest_id': destination.id,
            'origin': self.name,
            'seeding_mission_id': self.id,
            'company_id': company.id,
            'scheduled_date': mission.landing_at or fields.Datetime.now(),
            'move_ids': [Command.create({
                'product_id': product.id,
                'product_uom_qty': self.flares_fired,
                'product_uom': product.uom_id.id,
                'location_id': location.id,
                'location_dest_id': destination.id,
                'company_id': company.id,
            })],
        })
        picking.action_confirm()
        picking.action_assign()
        move = picking.move_ids
        usable = move.move_line_ids
        if product.tracking != 'none':
            usable = usable.filtered('lot_id')
        available = sum(usable.mapped('quantity'))
        short = float_compare(available, self.flares_fired, precision_rounding=move.product_uom.rounding) < 0
        if short or usable != move.move_line_ids:
            on_hand = product.with_context(location=location.id).qty_available
            raise UserError(_(
                "%(mission)s fired %(fired)s %(flare)s, but %(base)s can supply %(available)s from "
                "batches on hand (%(on_hand)s on hand). Correct the base stock, then complete the "
                "mission again.",
                mission=self.name, fired=self.flares_fired, flare=product.display_name,
                base=mission.base_id.name, available=int(available),
                on_hand=int(on_hand) if on_hand == int(on_hand) else on_hand,
            ))
        move.picked = True
        picking.with_context(skip_backorder=True, cancel_backorder=True).button_validate()
        if picking.state != 'done':
            raise UserError(_("The flare transfer %s could not be validated.", picking.name))
        self.picking_id = picking
        message = _(
            "%(fired)s flares left %(base)s by transfer %(picking)s.",
            fired=self.flares_fired, base=mission.base_id.name, picking=picking.name,
        )
        if self.flare_batches:
            message = '%s %s' % (message, _("Batches: %s.", self.flare_batches))
        self.message_post(body=message)
        return picking

    # Observed rainfall ------------------------------------------------------

    def _rainfall_period(self):
        """(start, end) in naive UTC: the takeoff hour to
        RAINFALL_HOURS_AFTER_LANDING hours after the end of the landing hour."""
        self.ensure_one()
        start = self.takeoff_at.replace(minute=0, second=0, microsecond=0)
        landing_hour = self.landing_at.replace(minute=0, second=0, microsecond=0)
        if landing_hour < self.landing_at:
            landing_hour += timedelta(hours=1)
        return start, landing_hour + timedelta(hours=RAINFALL_HOURS_AFTER_LANDING)

    def _check_rainfall_fetchable(self):
        self.ensure_one()
        if self.state not in FLOWN_STATES:
            raise UserError(_("Observed rainfall is fetched for completed missions (%s).", self.name))
        if not self.takeoff_at or not self.landing_at:
            raise UserError(_("%s has no takeoff or landing time.", self.name))
        start, end = self._rainfall_period()
        if end > fields.Datetime.now():
            raise UserError(_(
                "The rainfall period of %(mission)s runs until %(end)s. Fetch the rainfall after "
                "that: the reanalysis archive is usually about 6 days behind.",
                mission=self.name, end=self._utc_text(end),
            ))
        return start, end

    def _record_rainfall(self, data=None):
        """Sum the observed rainfall over the area for the rainfall period and
        store it. `data` is an archive response already covering the period
        (the demo world loader fetches one per area); without it the archive
        is called. Raises UserError if the archive does not cover it yet."""
        self.ensure_one()
        start, end = self._check_rainfall_fetchable()
        service = self.env['seeding.weather.service']
        if data is None:
            data = service.fetch_archive(self.area_id, start, end)
        total = service.rainfall_total(data, start, end)
        self.write({
            'observed_rainfall_mm': total,
            'rainfall_recorded_at': fields.Datetime.now(),
            'rainfall_period_start': start,
            'rainfall_period_end': end,
            'rainfall_source': RAINFALL_SOURCE,
            'rainfall_raw_response': json.dumps(data),
        })
        message = _(
            "Observed rainfall recorded: %(mm)s mm over %(area)s from %(start)s to %(end)s "
            "(%(source)s). Rainfall data by Open-Meteo.com (CC BY 4.0).",
            mm='%g' % total, area=self.area_id.name, start=self._utc_text(start),
            end=self._utc_text(end), source=RAINFALL_SOURCE,
        )
        if self.summary_state == 'approved':
            message += ' ' + _(
                "The approved client summary does not include it. To add it: Reopen Summary, "
                "Redraft Summary, then Approve Summary.",
            )
        elif self.summary_state == 'to_review':
            message += ' ' + _("Redraft Summary to include it in the client summary.")
        self.message_post(body=message)
        return total

    def action_fetch_rainfall(self):
        """'Fetch Observed Rainfall' button."""
        lines = []
        for mission in self:
            total = mission._record_rainfall()
            lines.append(_("%(mission)s: %(mm)s mm.", mission=mission.name, mm='%g' % total))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Observed rainfall recorded"),
                'message': ' '.join(lines),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

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

    def action_replan(self):
        self.ensure_one()
        result = self.env['seeding.mission.planner'].replan(self)
        if result is True:
            return True
        if result == 'pass':
            title, message, kind = _("Nothing to re-plan"), _("Every readiness check passes now."), 'info'
        else:
            failed = self.readiness_ids.filtered(lambda c: c.result == 'fail')
            title, kind = _("Re-plan cannot fix these checks"), 'warning'
            message = _(
                "Re-plan changes the aircraft, pilot, or flare load. These checks depend on the "
                "contract or the alert: %s.", ', '.join(failed.mapped('name')),
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': kind,
                'sticky': kind == 'warning',
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    # Briefing and client summary -------------------------------------------

    def action_redraft_briefing(self):
        for mission in self:
            if mission.state != 'ready' or mission.briefing_reviewed_by:
                raise UserError(_("Only the unreviewed briefing of a ready mission can be redrafted."))
            self.env['seeding.mission.writer'].write_briefing(mission)
        return True

    def action_review_briefing(self):
        for mission in self:
            if mission.state not in ('ready', 'launched') or not mission.briefing:
                raise UserError(_("There is no briefing to review on %s.", mission.name))
            if mission.briefing_reviewed_by:
                continue
            mission.write({
                'briefing_reviewed_by': self.env.user.id,
                'briefing_reviewed_at': fields.Datetime.now(),
            })
            mission.message_post(body=_("Crew briefing reviewed by %s.", self.env.user.name))
        return True

    def action_redraft_summary(self):
        for mission in self:
            if mission.state not in FLOWN_STATES or mission.summary_reviewed_by:
                raise UserError(_("Only the unapproved summary of a completed mission can be redrafted."))
            self.env['seeding.mission.writer'].write_summary(mission)
        return True

    def action_approve_summary(self):
        for mission in self:
            if mission.state not in FLOWN_STATES:
                raise UserError(_("Only the summary of a completed mission can be approved."))
            if not (mission.client_summary or '').strip():
                raise UserError(_("The client summary of %s is empty.", mission.name))
            if mission.summary_reviewed_by:
                continue
            mission.write({
                'summary_reviewed_by': self.env.user.id,
                'summary_reviewed_at': fields.Datetime.now(),
            })
            mission.message_post(body=_(
                "Client summary approved by %s. It now appears on the mission report.", self.env.user.name,
            ))
        return True

    def action_reopen_summary(self):
        for mission in self:
            if not mission.summary_reviewed_by:
                continue
            mission.write({'summary_reviewed_by': False, 'summary_reviewed_at': False})
            mission.message_post(body=_(
                "Client summary reopened for editing by %s. It is off the mission report until "
                "approved again.", self.env.user.name,
            ))
        return True

    def action_print_report(self):
        for mission in self:
            if mission.state not in FLOWN_STATES:
                raise UserError(_("The mission report is printed for completed missions."))
            if not mission.summary_reviewed_by:
                raise UserError(_("Approve the client summary of %s before printing the report.", mission.name))
        return self.env.ref('serein_ops.action_report_seeding_mission').report_action(self)

    def _release_from_invoice(self):
        """Billed missions whose invoice line was deleted or whose invoice was
        cancelled go back to completed, so a new invoice can bill them."""
        lines = self.env['account.move.line'].sudo().search([
            ('seeding_mission_id', 'in', self.ids), ('parent_state', '!=', 'cancel'),
        ])
        for mission in self.filtered(lambda m: m.state == 'billed'):
            if mission in lines.seeding_mission_id:
                continue
            invoice = mission.invoice_id
            mission.write({'state': 'completed', 'invoice_id': False})
            mission.message_post(body=_(
                "No longer invoiced (%s was cancelled or the line removed). The mission can be "
                "invoiced again.", invoice.name or _("the invoice"),
            ))

    def action_view_invoice(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.invoice_id.id,
            'view_mode': 'form',
            'views': [(self.env.ref('account.view_move_form').id, 'form')],
            'name': _("Invoice"),
        }

    def action_view_flare_transfer(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': self.picking_id.id,
            'view_mode': 'form',
            'name': _("Flare Transfer"),
        }

    @api.model
    def _open_missions(self, domain, context=None, history=False):
        """The Missions action filtered on `domain`. `history` opens the list
        first, for an aircraft's or a pilot's past missions."""
        action = self.env['ir.actions.act_window']._for_xml_id('serein_ops.seeding_mission_action')
        action['domain'] = domain
        action['context'] = context or {}
        if history:
            action['view_mode'] = 'list,form'
            action['views'] = [(False, 'list'), (False, 'form')]
        return action

    def action_view_ai_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('serein_ops.seeding_ai_log_action')
        action['domain'] = [('mission_id', '=', self.id)]
        action['context'] = {}
        return action

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
