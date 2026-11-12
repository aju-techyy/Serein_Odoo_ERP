from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .seeding_readiness_check import READINESS_CHECKS
from .seeding_weather_snapshot import WIND_CALM, WIND_LIMIT

_WIND_CALM, _WIND_LIMIT = '%g' % WIND_CALM, '%g' % WIND_LIMIT
MAX_REPLANS = 2
WINDOW_TOLERANCE = timedelta(minutes=15)
TIME_FORMAT = '%Y-%m-%d %H:%M'

PLAN_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'aircraft_id': {'type': 'INTEGER', 'description': "id of the chosen aircraft"},
        'pilot_id': {'type': 'INTEGER', 'description': "id of the chosen pilot or drone operator"},
        'base_id': {'type': 'INTEGER', 'description': "id of the aircraft's home base"},
        'flares_planned': {'type': 'INTEGER', 'description': "number of flares to load"},
        'launch_start': {'type': 'STRING', 'description': "earliest takeoff, UTC, YYYY-MM-DD HH:MM"},
        'launch_end': {'type': 'STRING', 'description': "latest takeoff, UTC, YYYY-MM-DD HH:MM"},
        'reasoning': {'type': 'STRING', 'description': "why this plan, in plain English"},
        'risks': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
    },
    'required': [
        'aircraft_id', 'pilot_id', 'base_id', 'flares_planned',
        'launch_start', 'launch_end', 'reasoning', 'risks',
    ],
    'propertyOrdering': [
        'aircraft_id', 'pilot_id', 'base_id', 'flares_planned',
        'launch_start', 'launch_end', 'reasoning', 'risks',
    ],
}

PLAN_INSTRUCTIONS = """You are the mission planner of a cloud seeding operator.
From the cloud alert, contract, fleet, crew and flare stock in the input, choose
one aircraft, one pilot, the number of flares to load, and the launch window.

Every plan is verified against these readiness checks before anyone can fly it:
%(checks)s

Rules:
- Use only aircraft, pilots and bases listed in the input, and give their ids.
- base_id must be the chosen aircraft's home base: flares are loaded there.
- A crewed aircraft needs a pilot whose role is "pilot". A drone needs a pilot
  whose role is "drone_operator".
- flares_planned must be at least 1, fit the aircraft's rack capacity, and not
  exceed the flares on hand at the base.
- launch_start and launch_end are takeoff times in UTC, formatted YYYY-MM-DD HH:MM.
  Both must lie inside the alert window, and launch_start must not be before now_utc.
- Prefer the plan that releases the most seeding material while passing every check.
- reasoning: three to five plain sentences for the operations team, referring to
  the weather readings and to why these assets were chosen over the others.
- risks: short phrases, one per real risk to the flight or the seeding. Describe each
  in proportion to the readings: wind and gusts up to """ + _WIND_CALM + """ km/h are light (the
  score gives full wind points), and wind scores nothing from """ + _WIND_LIMIT + """ km/h. A favourable
  reading, such as light wind, is not a risk. Use an empty list if there are none.
"""

REPLAN_INSTRUCTIONS = PLAN_INSTRUCTIONS + """
This is a re-plan. The input holds previous_plan and failed_checks: the previous
plan failed those readiness checks. Propose a different plan that resolves every
failed check, and say in the reasoning what you changed and why.
"""


class PlanRejected(Exception):
    """The model's answer cannot be used as a plan."""


def _fmt(moment):
    return moment.strftime(TIME_FORMAT) if moment else None


class SeedingMissionPlanner(models.AbstractModel):
    """Plans missions for cloud alerts. The language model proposes a plan,
    the answer is validated against the records, and the readiness engine
    verifies it. When the model fails or its answer is rejected, the
    rule-based planner takes over."""
    _name = 'seeding.mission.planner'
    _description = 'Serein Mission Planner'

    @api.model
    def max_replans(self):
        return MAX_REPLANS

    # Inputs ---------------------------------------------------------------

    @api.model
    def _candidates(self):
        aircraft = self.env['maintenance.equipment'].sudo().search(
            [('is_aircraft', '=', True), ('home_base_id', '!=', False)],
        )
        pilots = self.env['hr.employee'].sudo().search([('is_pilot', '=', True)])
        if not aircraft:
            raise UserError(_("There is no aircraft with a home base to plan with."))
        if not pilots:
            raise UserError(_("There is no pilot or drone operator to plan with."))
        aircraft = aircraft.sorted(lambda a: (-a.rack_capacity, a.name or '', a.id))
        pilots = pilots.sorted(lambda p: (p.duty_hours_7d, p.name or '', p.id))
        return aircraft, pilots

    @api.model
    def _flare_product(self):
        return self.env['seeding.mission'].sudo()._default_flare_product()

    @api.model
    def _on_hand(self, flare, base):
        if not flare or not base.location_id:
            return 0.0
        return flare.with_context(location=base.location_id.id).qty_available

    @api.model
    def _planning_input(self, alert, aircraft, pilots):
        """Operational facts only: the alert, the contract, the fleet, the
        crew's qualifications and duty, and flare stock."""
        now = fields.Datetime.now().replace(second=0, microsecond=0)
        Mission = self.env['seeding.mission'].sudo()
        contract = Mission.new({'alert_id': alert.id}).contract_id
        flare = self._flare_product()
        snap = alert.snapshot_id
        launched = Mission.search([('state', '=', 'launched'), ('aircraft_id', 'in', aircraft.ids)])

        data = {
            'now_utc': _fmt(now),
            'alert': {
                'reference': alert.name,
                'target_area': alert.area_id.name,
                'latitude': alert.area_id.latitude,
                'longitude': alert.area_id.longitude,
                'radius_km': alert.area_id.radius_km,
                'window_start_utc': _fmt(alert.window_start),
                'window_end_utc': _fmt(alert.window_end),
                'seedability_score': alert.score,
                'score_breakdown': alert.score_breakdown,
                'readings_for_utc': _fmt(snap.valid_for),
                'readings': {
                    'cloud_cover_low_pct': snap.cloud_cover_low,
                    'cloud_cover_mid_pct': snap.cloud_cover_mid,
                    'cloud_cover_high_pct': snap.cloud_cover_high,
                    'cape_j_per_kg': snap.cape,
                    'lifted_index': snap.lifted_index,
                    'convective_inhibition_j_per_kg': snap.convective_inhibition,
                    'relative_humidity_pct': snap.relative_humidity,
                    'precipitation_probability_pct': snap.precipitation_probability,
                    'freezing_level_m': snap.freezing_level,
                    'wind_speed_kmh': snap.wind_speed,
                    'wind_gusts_kmh': snap.wind_gusts,
                    'wind_direction_deg': snap.wind_direction,
                },
            },
            'contract': {
                'reference': contract.name,
                'status': contract.state,
                'season_start': str(contract.season_start),
                'season_end': str(contract.season_end),
                'flight_hour_budget': contract.flight_hour_budget,
                'flight_hours_used': contract.hours_used,
                'flight_hours_remaining': contract.hours_remaining,
                'standby_fee_per_day': contract.standby_fee,
                'flight_hour_rate': contract.flight_hour_rate,
                'currency': contract.currency_id.name,
            } if contract else None,
            'aircraft': [{
                'id': plane.id,
                'name': plane.name,
                'type': plane.aircraft_type or 'crewed',
                'tail_number': plane.tail_number or None,
                'home_base_id': plane.home_base_id.id,
                'home_base': plane.home_base_id.name,
                'rack_capacity': plane.rack_capacity,
                'service_interval_h': plane.service_interval_hours,
                'hours_since_service': plane.hours_since_service,
                'open_corrective_requests': plane._open_corrective_requests().mapped('name'),
                'open_service_requests': plane._open_service_requests().mapped('name'),
                'airworthy': plane.is_airworthy,
                'flying_mission': launched.filtered(lambda m: m.aircraft_id == plane)[:1].name or None,
                'hourly_cost': plane.aircraft_hourly_cost,
            } for plane in aircraft],
            'pilots': [{
                'id': pilot.id,
                'name': pilot.name,
                'role': pilot.pilot_role or 'pilot',
                'licence_expiry': str(pilot.licence_expiry) if pilot.licence_expiry else None,
                'medical_expiry': (
                    str(pilot.medical_expiry) if pilot.medical_expiry else None
                ) if pilot.pilot_role != 'drone_operator' else 'not required',
                'duty_hours_last_7_days': pilot.duty_hours_7d,
                'weekly_duty_limit_h': pilot.weekly_duty_limit,
            } for pilot in pilots],
            'bases': [{
                'id': base.id,
                'name': base.name,
                'flare_product': flare.display_name if flare else None,
                'flares_on_hand': self._on_hand(flare, base),
            } for base in aircraft.home_base_id],
        }
        return data

    @api.model
    def _parse_time(self, value):
        """Parse a UTC time from the model into a naive UTC datetime."""
        if not isinstance(value, str) or not value.strip():
            raise PlanRejected(_("A launch time is missing."))
        text = value.strip().replace('Z', '+00:00')
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as error:
            raise PlanRejected(_("Launch time %r is not a date and time.", value)) from error
        if moment.tzinfo:
            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
        return moment.replace(second=0, microsecond=0)

    @api.model
    def _as_int(self, value, label):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise PlanRejected(_("%(label)s %(value)r is not a whole number.", label=label, value=value))
        return value

    # Validation -----------------------------------------------------------

    @api.model
    def _validate(self, data, alert, aircraft, pilots, previous=None):
        """Turn the model's answer into mission values, or raise PlanRejected.
        Every aircraft, pilot, and base must be one that was sent, and every
        quantity and time must make sense. The readiness engine then checks
        the plan against the rules."""
        missing = [key for key in PLAN_SCHEMA['required'] if key not in data]
        if missing:
            raise PlanRejected(_("The answer is missing: %s.", ', '.join(missing)))

        aircraft_id = self._as_int(data['aircraft_id'], _("Aircraft id"))
        plane = aircraft.filtered(lambda a: a.id == aircraft_id)
        if not plane:
            raise PlanRejected(_("Aircraft id %s is not in the fleet that was sent.", aircraft_id))
        pilot_id = self._as_int(data['pilot_id'], _("Pilot id"))
        pilot = pilots.filtered(lambda p: p.id == pilot_id)
        if not pilot:
            raise PlanRejected(_("Pilot id %s is not in the crew that was sent.", pilot_id))
        base_id = self._as_int(data['base_id'], _("Base id"))
        if base_id != plane.home_base_id.id:
            raise PlanRejected(_(
                "Base id %(base)s is not the home base of %(aircraft)s (%(home)s, id %(home_id)s).",
                base=base_id, aircraft=plane.name, home=plane.home_base_id.name,
                home_id=plane.home_base_id.id,
            ))
        flares = self._as_int(data['flares_planned'], _("Flares planned"))
        largest_rack = max(aircraft.mapped('rack_capacity') or [0])
        if flares < 1 or flares > largest_rack:
            raise PlanRejected(_(
                "%(flares)s flares is not a possible load (1 to %(rack)s).",
                flares=flares, rack=largest_rack,
            ))

        start = self._parse_time(data['launch_start'])
        end = self._parse_time(data['launch_end'])
        now = fields.Datetime.now().replace(second=0, microsecond=0)
        if end <= start:
            raise PlanRejected(_("The launch window ends before it starts."))
        if start < alert.window_start - WINDOW_TOLERANCE or start >= alert.window_end:
            raise PlanRejected(_(
                "Launch start %(start)s UTC is outside the alert window (%(ws)s to %(we)s UTC).",
                start=_fmt(start), ws=_fmt(alert.window_start), we=_fmt(alert.window_end),
            ))
        if end > alert.window_end + WINDOW_TOLERANCE:
            raise PlanRejected(_(
                "Launch end %(end)s UTC is after the alert window closes (%(we)s UTC).",
                end=_fmt(end), we=_fmt(alert.window_end),
            ))
        if end <= now:
            raise PlanRejected(_("The launch window is already over."))

        reasoning = data['reasoning']
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise PlanRejected(_("The answer gives no reasoning."))
        risks = data['risks']
        if not isinstance(risks, list) or not all(isinstance(r, str) for r in risks):
            raise PlanRejected(_("Risks must be a list of text."))

        plan = (plane.id, pilot.id, plane.home_base_id.id, flares)
        if previous and plan == previous:
            raise PlanRejected(_("The new plan is the same as the plan that failed its checks."))

        return {
            'aircraft_id': plane.id,
            'pilot_id': pilot.id,
            'base_id': plane.home_base_id.id,
            'flare_product_id': self._flare_product().id,
            'flares_planned': flares,
            'planned_takeoff': max(start, now),
            'launch_by': min(end, alert.window_end),
            'planned_by': 'ai',
            'plan_reasoning': reasoning.strip(),
            'plan_risks': '\n'.join('• %s' % r.strip() for r in risks if r.strip()) or False,
            'fallback_reason': False,
        }

    # Rule-based planner ---------------------------------------------------

    @api.model
    def _rule_plan(self, alert, aircraft, pilots, origin=None):
        """The first aircraft and pilot that pass every readiness check, with
        the fullest flare load the rack and base stock allow. If no pair
        passes, the pair with the fewest failed checks."""
        Mission = self.env['seeding.mission'].sudo()
        flare = self._flare_product()
        now = fields.Datetime.now().replace(second=0, microsecond=0)
        best = None
        for plane in aircraft:
            on_hand = int(self._on_hand(flare, plane.home_base_id))
            flares = min(plane.rack_capacity, on_hand) if on_hand >= 1 else plane.rack_capacity
            for pilot in pilots:
                vals = {
                    'alert_id': alert.id,
                    'aircraft_id': plane.id,
                    'pilot_id': pilot.id,
                    'base_id': plane.home_base_id.id,
                    'flare_product_id': flare.id,
                    'flares_planned': flares,
                    'planned_takeoff': max(alert.window_start, now),
                }
                failed = Mission._evaluate_plan(vals, origin=origin)
                if not failed:
                    return vals, plane, pilot, []
                if best is None or len(failed) < len(best[3]):
                    best = (vals, plane, pilot, failed)
        return best

    @api.model
    def _rule_values(self, alert, aircraft, pilots, reason, origin=None):
        vals, plane, pilot, failed = self._rule_plan(alert, aircraft, pilots, origin=origin)
        summary = _(
            "%(aircraft)s from %(base)s, flown by %(pilot)s, loading %(flares)s flares "
            "(rack %(rack)s).",
            aircraft=plane.name, base=plane.home_base_id.name, pilot=pilot.name,
            flares=vals['flares_planned'], rack=plane.rack_capacity,
        )
        if failed:
            reasoning = _(
                "No aircraft and pilot pass every readiness check. This plan has the fewest "
                "failed checks (%(count)s: %(names)s): %(summary)s",
                count=len(failed), names=', '.join(name for name, _expl in failed), summary=summary,
            )
        else:
            reasoning = _(
                "Rule-based plan: the first aircraft and pilot that pass all eleven readiness "
                "checks, with the fullest flare load the rack and base stock allow: %s",
                summary,
            )
        vals.pop('alert_id', None)
        vals.update({
            'launch_by': alert.window_end,
            'planned_by': 'rules',
            'plan_reasoning': reasoning,
            'plan_risks': False,
            'fallback_reason': reason,
        })
        return vals

    # Entry points ---------------------------------------------------------

    @api.model
    def plan_alert(self, alert):
        """Create a proposed mission for a new cloud alert."""
        alert.ensure_one()
        now = fields.Datetime.now()
        if alert.state != 'new':
            raise UserError(_("Only a new alert can be planned. %s is not new.", alert.name))
        if alert.expires_at and alert.expires_at <= now:
            raise UserError(_("Alert %s has expired.", alert.name))
        aircraft, pilots = self._candidates()

        payload = self._planning_input(alert, aircraft, pilots)
        data, log = self.env['seeding.ai.service'].generate(
            'plan', self._instructions(replan=False), payload, PLAN_SCHEMA, alert=alert,
        )
        vals = self._ai_values(data, log, alert, aircraft, pilots)
        if vals is None:
            vals = self._rule_values(alert, aircraft, pilots, self._fallback_reason(log))

        mission = self.env['seeding.mission'].create({'alert_id': alert.id, **vals})
        log.sudo().mission_id = mission
        mission.message_post(body=self._plan_message(mission, log))
        return mission

    @api.model
    def replan(self, mission):
        """Ask for a different plan for a proposed mission whose checks
        failed. At most MAX_REPLANS rounds per mission.

        Returns True when a new plan was written, 'pass' when every check
        passes, and 'not_fixable' when only contract or alert checks fail
        (no aircraft, pilot, or flare load can fix those). The last two
        call nothing and use no round."""
        mission.ensure_one()
        if mission.state != 'proposed':
            raise UserError(_("Only a proposed mission can be re-planned."))
        if not mission.alert_id:
            raise UserError(_("Mission %s has no cloud alert to plan from.", mission.name))
        if mission.replan_count >= MAX_REPLANS:
            raise UserError(_(
                "Mission %(mission)s has been re-planned %(count)s times, the maximum. "
                "Change the assets by hand.", mission=mission.name, count=MAX_REPLANS,
            ))
        alert = mission.alert_id
        if alert.window_end <= fields.Datetime.now():
            raise UserError(_("The window of alert %s has closed.", alert.name))

        mission._run_readiness()
        failed = mission.readiness_ids.filtered(lambda c: c.result == 'fail')
        if not failed:
            return 'pass'
        if not mission._replannable_failures():
            return 'not_fixable'
        aircraft, pilots = self._candidates()

        payload = self._planning_input(alert, aircraft, pilots)
        payload['previous_plan'] = {
            'aircraft_id': mission.aircraft_id.id or None,
            'aircraft': mission.aircraft_id.name or None,
            'pilot_id': mission.pilot_id.id or None,
            'pilot': mission.pilot_id.name or None,
            'base_id': mission.base_id.id or None,
            'flares_planned': mission.flares_planned,
        }
        payload['failed_checks'] = [
            {'check': check.name, 'explanation': check.explanation} for check in failed
        ]
        previous = (
            mission.aircraft_id.id, mission.pilot_id.id, mission.base_id.id, mission.flares_planned,
        )
        data, log = self.env['seeding.ai.service'].generate(
            'replan', self._instructions(replan=True), payload, PLAN_SCHEMA,
            mission=mission, alert=alert,
        )
        vals = self._ai_values(data, log, alert, aircraft, pilots, previous=previous)
        if vals is None:
            vals = self._rule_values(alert, aircraft, pilots, self._fallback_reason(log), origin=mission)
        vals['replan_count'] = mission.replan_count + 1
        failed_names = failed.mapped('name')  # the lines are replaced by the write
        mission.write(vals)
        mission.message_post(body=self._plan_message(mission, log, failed_names=failed_names))
        return True

    # Helpers --------------------------------------------------------------

    @api.model
    def _instructions(self, replan):
        checks = '\n'.join('%s. %s' % (number, label) for number, _c, label, _m in READINESS_CHECKS)
        return (REPLAN_INSTRUCTIONS if replan else PLAN_INSTRUCTIONS) % {'checks': checks}

    @api.model
    def _ai_values(self, data, log, alert, aircraft, pilots, previous=None):
        if data is None:
            return None
        try:
            return self._validate(data, alert, aircraft, pilots, previous=previous)
        except PlanRejected as error:
            log._reject(str(error))
            return None

    @api.model
    def _fallback_reason(self, log):
        if log.outcome == 'failed':
            return _("The AI planner was unavailable: %s", log.outcome_reason)
        return _("The AI plan was rejected: %s", log.outcome_reason)

    @api.model
    def _plan_message(self, mission, log, failed_names=None):
        if failed_names:
            intro = _(
                "Re-plan round %(round)s of %(max)s, after failed checks: %(checks)s.",
                round=mission.replan_count, max=MAX_REPLANS,
                checks=', '.join(failed_names),
            )
        else:
            intro = _("Planned from alert %s.", mission.alert_id.name)
        if mission.planned_by == 'ai':
            source = _("Plan proposed by AI (%s).", log.model_name)
        else:
            source = _("Plan made by the rule-based planner. %s", mission.fallback_reason)
        result = _("Readiness: all checks passed.") if mission.readiness_state == 'pass' else _(
            "Readiness: %s check(s) failed.", mission.failed_check_count,
        )
        return ' '.join([intro, source, result])
