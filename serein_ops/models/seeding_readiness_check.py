from odoo import _, api, fields, models

# (number, code, label, method). The order is the order shown on the mission.
READINESS_CHECKS = [
    (1, 'contract_active', 'Contract active', '_readiness_contract_active'),
    (2, 'area_covered', 'Area covered', '_readiness_area_covered'),
    (3, 'hour_budget', 'Flight-hour budget', '_readiness_hour_budget'),
    (4, 'alert_valid', 'Alert still valid', '_readiness_alert_valid'),
    (5, 'aircraft_airworthy', 'Aircraft airworthy', '_readiness_aircraft_airworthy'),
    (6, 'aircraft_free', 'Aircraft free', '_readiness_aircraft_free'),
    (7, 'rack_capacity', 'Rack capacity', '_readiness_rack_capacity'),
    (8, 'pilot_licence', 'Pilot licence', '_readiness_pilot_licence'),
    (9, 'pilot_medical', 'Pilot medical', '_readiness_pilot_medical'),
    (10, 'duty_hours', 'Pilot duty hours', '_readiness_duty_hours'),
    (11, 'flares_in_stock', 'Flares in stock', '_readiness_flares_in_stock'),
]

PASS, FAIL, NA = 'pass', 'fail', 'na'


def _hours(value):
    return '%.1f' % value


class SeedingReadinessCheck(models.Model):
    _name = 'seeding.readiness.check'
    _description = 'Mission Readiness Check'
    _order = 'mission_id, sequence'

    mission_id = fields.Many2one(
        'seeding.mission', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(string='#', required=True)
    code = fields.Selection(
        [(code, label) for _num, code, label, _method in READINESS_CHECKS],
        required=True,
    )
    name = fields.Char(string='Check', required=True)
    result = fields.Selection(
        [(PASS, 'Pass'), (FAIL, 'Fail'), (NA, 'Not applicable')],
        required=True,
    )
    explanation = fields.Text(required=True)


class SeedingMission(models.Model):
    """The readiness engine: eleven go/no-go rules, one method each.
    Every method returns (result, explanation) in plain language."""
    _inherit = 'seeding.mission'

    def _run_readiness(self):
        """Evaluate every check and replace the mission's readiness lines.
        The lines are written as superuser: users can read them but never
        write a result by hand."""
        now = fields.Datetime.now()
        Check = self.env['seeding.readiness.check'].sudo()
        for mission in self:
            evaluated = mission.sudo()
            vals_list = []
            for number, code, label, method in READINESS_CHECKS:
                result, explanation = getattr(evaluated, method)()
                vals_list.append({
                    'mission_id': mission.id,
                    'sequence': number,
                    'code': code,
                    'name': label,
                    'result': result,
                    'explanation': explanation,
                })
            Check.search([('mission_id', '=', mission.id)]).unlink()
            Check.create(vals_list)
            mission.sudo().readiness_checked_at = now
        self.invalidate_recordset(['readiness_ids', 'readiness_state', 'failed_check_count'])
        return True

    @api.model
    def _evaluate_plan(self, vals, origin=None):
        """Run every check on an unsaved plan. Returns the failed checks as
        (label, explanation) pairs. Nothing is written."""
        record = self.sudo().new(vals, origin=origin)
        failed = []
        for _number, _code, label, method in READINESS_CHECKS:
            result, explanation = getattr(record, method)()
            if result == FAIL:
                failed.append((label, explanation))
        return failed

    def _flight_date(self):
        """The day the checks are judged for: the planned takeoff day, or
        today if that is already past."""
        today = fields.Date.context_today(self)
        if self.planned_takeoff:
            return max(today, fields.Date.context_today(self, self.planned_takeoff))
        return today

    # 1 ----------------------------------------------------------------------
    def _readiness_contract_active(self):
        contract = self.contract_id
        if not contract:
            return FAIL, _("No contract is linked to this mission.")
        day = self._flight_date()
        if contract.state != 'active':
            return FAIL, _(
                "Contract %(contract)s is %(state)s, not active.",
                contract=contract.name,
                state=dict(contract._fields['state']._description_selection(self.env))[contract.state].lower(),
            )
        if not contract.season_start <= day <= contract.season_end:
            return FAIL, _(
                "%(day)s is outside the season of contract %(contract)s (%(start)s to %(end)s).",
                day=day, contract=contract.name, start=contract.season_start, end=contract.season_end,
            )
        return PASS, _(
            "Contract %(contract)s is active, and %(day)s is inside its season (%(start)s to %(end)s).",
            contract=contract.name, day=day, start=contract.season_start, end=contract.season_end,
        )

    # 2 ----------------------------------------------------------------------
    def _readiness_area_covered(self):
        if not self.contract_id:
            return FAIL, _("No contract is linked, so the target area is not covered.")
        if self.area_id not in self.contract_id.area_ids:
            return FAIL, _(
                "Target area %(area)s is not part of contract %(contract)s.",
                area=self.area_id.name, contract=self.contract_id.name,
            )
        return PASS, _(
            "Target area %(area)s is covered by contract %(contract)s.",
            area=self.area_id.name, contract=self.contract_id.name,
        )

    # 3 ----------------------------------------------------------------------
    def _readiness_hour_budget(self):
        contract = self.contract_id
        if not contract:
            return FAIL, _("No contract is linked, so there is no flight-hour budget.")
        values = {
            'contract': contract.name,
            'used': _hours(contract.hours_used),
            'budget': _hours(contract.flight_hour_budget),
        }
        if contract.hours_used >= contract.flight_hour_budget:
            return FAIL, _(
                "Contract %(contract)s has used %(used)s of its %(budget)s budgeted flight hours. "
                "No hours are left.", **values,
            )
        return PASS, _(
            "Contract %(contract)s has used %(used)s of its %(budget)s budgeted flight hours.",
            **values,
        )

    # 4 ----------------------------------------------------------------------
    def _readiness_alert_valid(self):
        alert = self.alert_id
        if not alert:
            return NA, _("The mission was planned without a cloud alert.")
        if alert.state == 'dismissed':
            return FAIL, _("Alert %s was dismissed.", alert.name)
        expires_at = alert.expires_at or alert.window_end
        if alert.state == 'expired' or expires_at <= fields.Datetime.now():
            return FAIL, _(
                "Alert %(alert)s expired at %(time)s UTC.",
                alert=alert.name, time=expires_at.strftime('%d %b %Y %H:%M'),
            )
        return PASS, _(
            "Alert %(alert)s is valid until %(time)s UTC.",
            alert=alert.name, time=expires_at.strftime('%d %b %Y %H:%M'),
        )

    # 5 ----------------------------------------------------------------------
    def _readiness_aircraft_airworthy(self):
        aircraft = self.aircraft_id
        if not aircraft:
            return FAIL, _("No aircraft is assigned.")
        problems = []
        open_requests = aircraft._open_corrective_requests()
        if open_requests:
            problems.append(_(
                "it has an open corrective maintenance request (%s)",
                ', '.join(open_requests.mapped('name')),
            ))
        if aircraft.hours_since_service >= aircraft.service_interval_hours:
            problems.append(_(
                "it has flown %(flown)s h since its last service, and the interval is %(interval)s h",
                flown=_hours(aircraft.hours_since_service),
                interval=_hours(aircraft.service_interval_hours),
            ))
        if problems:
            return FAIL, _(
                "%(aircraft)s is not airworthy: %(problems)s.",
                aircraft=aircraft.display_name, problems='; '.join(problems),
            )
        return PASS, _(
            "%(aircraft)s has no open corrective request and has flown %(flown)s of %(interval)s h "
            "since its last service.",
            aircraft=aircraft.display_name, flown=_hours(aircraft.hours_since_service),
            interval=_hours(aircraft.service_interval_hours),
        )

    # 6 ----------------------------------------------------------------------
    def _readiness_aircraft_free(self):
        aircraft = self.aircraft_id
        if not aircraft:
            return FAIL, _("No aircraft is assigned.")
        flying = self.search([
            ('aircraft_id', '=', aircraft.id),
            ('state', '=', 'launched'),
            ('id', '!=', self._origin.id or 0),
        ], limit=1)
        if flying:
            return FAIL, _(
                "%(aircraft)s is flying mission %(mission)s.",
                aircraft=aircraft.display_name, mission=flying.name,
            )
        return PASS, _("%s is not on another launched mission.", aircraft.display_name)

    # 7 ----------------------------------------------------------------------
    def _readiness_rack_capacity(self):
        aircraft = self.aircraft_id
        if not aircraft:
            return FAIL, _("No aircraft is assigned.")
        if self.flares_planned <= 0:
            return FAIL, _("No flares are planned.")
        if self.flares_planned > aircraft.rack_capacity:
            return FAIL, _(
                "%(planned)s flares are planned, but the rack of %(aircraft)s holds %(capacity)s.",
                planned=self.flares_planned, aircraft=aircraft.display_name,
                capacity=aircraft.rack_capacity,
            )
        return PASS, _(
            "%(planned)s flares fit the rack of %(aircraft)s (capacity %(capacity)s).",
            planned=self.flares_planned, aircraft=aircraft.display_name,
            capacity=aircraft.rack_capacity,
        )

    # 8 ----------------------------------------------------------------------
    def _readiness_pilot_licence(self):
        pilot = self.pilot_id
        if not pilot:
            return FAIL, _("No pilot is assigned.")
        if not pilot.is_pilot:
            return FAIL, _("%s is not registered as a pilot or drone operator.", pilot.name)
        role = pilot.pilot_role or 'pilot'
        if self.aircraft_type == 'drone' and role != 'drone_operator':
            return FAIL, _(
                "%(pilot)s holds a pilot licence, and %(aircraft)s is a drone: "
                "a drone operator must fly it.",
                pilot=pilot.name, aircraft=self.aircraft_id.display_name,
            )
        if self.aircraft_type == 'crewed' and role != 'pilot':
            return FAIL, _(
                "%(pilot)s is a drone operator, and %(aircraft)s is a crewed aircraft.",
                pilot=pilot.name, aircraft=self.aircraft_id.display_name,
            )
        day = self._flight_date()
        if not pilot.licence_expiry:
            return FAIL, _("%s has no licence expiry date on file.", pilot.name)
        if pilot.licence_expiry < day:
            return FAIL, _(
                "The licence of %(pilot)s expired on %(expiry)s.",
                pilot=pilot.name, expiry=pilot.licence_expiry,
            )
        return PASS, _(
            "The licence of %(pilot)s is valid until %(expiry)s.",
            pilot=pilot.name, expiry=pilot.licence_expiry,
        )

    # 9 ----------------------------------------------------------------------
    def _readiness_pilot_medical(self):
        pilot = self.pilot_id
        if not pilot:
            return FAIL, _("No pilot is assigned.")
        if pilot.pilot_role == 'drone_operator':
            return NA, _("%s is a drone operator and needs no aviation medical.", pilot.name)
        day = self._flight_date()
        if not pilot.medical_expiry:
            return FAIL, _("%s has no medical expiry date on file.", pilot.name)
        if pilot.medical_expiry < day:
            return FAIL, _(
                "The medical certificate of %(pilot)s expired on %(expiry)s.",
                pilot=pilot.name, expiry=pilot.medical_expiry,
            )
        return PASS, _(
            "The medical certificate of %(pilot)s is valid until %(expiry)s.",
            pilot=pilot.name, expiry=pilot.medical_expiry,
        )

    # 10 ---------------------------------------------------------------------
    def _readiness_duty_hours(self):
        pilot = self.pilot_id
        if not pilot:
            return FAIL, _("No pilot is assigned.")
        values = {
            'pilot': pilot.name,
            'duty': _hours(pilot.duty_hours_7d),
            'limit': _hours(pilot.weekly_duty_limit),
        }
        if pilot.duty_hours_7d >= pilot.weekly_duty_limit:
            return FAIL, _(
                "%(pilot)s has %(duty)s duty hours in the last 7 days, which reaches the "
                "weekly limit of %(limit)s h.", **values,
            )
        return PASS, _(
            "%(pilot)s has %(duty)s duty hours in the last 7 days, under the weekly limit of %(limit)s h.",
            **values,
        )

    # 11 ---------------------------------------------------------------------
    def _readiness_flares_in_stock(self):
        if not self.base_id:
            return FAIL, _("No base is set.")
        if not self.base_id.location_id:
            return FAIL, _("Base %s has no stock location.", self.base_id.name)
        if not self.flare_product_id:
            return FAIL, _("No flare product is chosen.")
        on_hand = self.flare_product_id.with_context(
            location=self.base_id.location_id.id,
        ).qty_available
        values = {
            'base': self.base_id.name,
            'on_hand': int(on_hand) if on_hand == int(on_hand) else on_hand,
            'flare': self.flare_product_id.display_name,
            'planned': self.flares_planned,
        }
        if on_hand < self.flares_planned:
            return FAIL, _(
                "%(base)s holds %(on_hand)s %(flare)s, and %(planned)s are planned.", **values,
            )
        return PASS, _(
            "%(base)s holds %(on_hand)s %(flare)s, enough for the %(planned)s planned.", **values,
        )
