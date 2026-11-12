import os
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.service.model import call_kw
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.serein_ops.models.ai_service import API_KEY_ENV


@tagged('post_install', '-at_install')
class TestReadiness(TransactionCase):
    """Phase 3: missions and the eleven readiness checks."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Mark Ready and Complete draft texts with the AI writer. Without a
        # key it calls nothing and uses the template.
        cls.startClassPatcher(patch.dict(os.environ, {API_KEY_ENV: ''}))
        today = fields.Date.context_today(cls.env.user)
        cls.today = today
        cls.now = fields.Datetime.now().replace(second=0, microsecond=0)

        cls.client = cls.env['res.partner'].create({'name': 'Northwind Water Authority', 'is_company': True})
        cls.area = cls.env['seeding.target.area'].create({
            'name': 'Northern Highlands', 'partner_id': cls.client.id,
            'latitude': 24.5, 'longitude': 55.9, 'radius_km': 40,
        })
        cls.area_outside = cls.env['seeding.target.area'].create({
            'name': 'Eastern Plateau', 'partner_id': cls.client.id,
            'latitude': 24.1, 'longitude': 56.2, 'radius_km': 20,
        })
        cls.contract = cls.env['seeding.contract'].create({
            'partner_id': cls.client.id,
            'area_ids': [(6, 0, cls.area.ids)],
            'season_start': today - timedelta(days=30),
            'season_end': today + timedelta(days=120),
            'flight_hour_budget': 100,
            'standby_fee': 1500,
            'flight_hour_rate': 2500,
        })
        cls.contract.action_activate()

        cls.base = cls.env['seeding.base'].create({'name': 'Coastal Airfield'})
        cls.flare = cls.env['product.product'].create({
            'name': 'Hygroscopic Flare 1 kg', 'type': 'consu', 'is_storable': True,
            'is_seeding_flare': True, 'grams_per_flare': 1000,
        })
        cls.env['stock.quant']._update_available_quantity(cls.flare, cls.base.location_id, 40)

        aircraft = {'is_aircraft': True, 'home_base_id': cls.base.id, 'service_interval_hours': 100}
        cls.king_air = cls.env['maintenance.equipment'].create({
            **aircraft, 'name': 'King Air 350', 'aircraft_type': 'crewed',
            'tail_number': 'A6-SRA', 'rack_capacity': 24,
        })
        cls.cessna = cls.env['maintenance.equipment'].create({
            **aircraft, 'name': 'Cessna 340', 'aircraft_type': 'crewed',
            'tail_number': 'A6-SRB', 'rack_capacity': 16,
        })
        cls.drone = cls.env['maintenance.equipment'].create({
            **aircraft, 'name': 'Seeder Drone 1', 'aircraft_type': 'drone',
            'tail_number': 'A6-SRD', 'rack_capacity': 4,
        })

        crew = {'is_pilot': True, 'weekly_duty_limit': 30,
                'licence_expiry': today + timedelta(days=365)}
        cls.pilot = cls.env['hr.employee'].create({
            **crew, 'name': 'Mara Okafor', 'pilot_role': 'pilot',
            'pilot_licence_number': 'CPL-20431', 'medical_expiry': today + timedelta(days=180),
        })
        cls.operator_crew = cls.env['hr.employee'].create({
            **crew, 'name': 'Tomas Lind', 'pilot_role': 'drone_operator',
            'pilot_licence_number': 'RPAS-118',
        })

        cls.snapshot = cls.env['seeding.weather.snapshot'].create({
            'area_id': cls.area.id, 'valid_for': cls.now,
            'cloud_cover_low': 80, 'cloud_cover_mid': 60, 'cape': 800, 'wind_speed': 15,
        })

    # Helpers ---------------------------------------------------------------

    def _alert(self, **vals):
        return self.env['seeding.cloud.alert'].create({
            'snapshot_id': self.snapshot.id,
            'window_start': self.now,
            'window_end': self.now + timedelta(hours=3),
            **vals,
        })

    def _mission(self, **vals):
        values = {
            'alert_id': self._alert().id,
            'aircraft_id': self.king_air.id,
            'pilot_id': self.pilot.id,
            'flares_planned': 12,
        }
        values.update(vals)
        return self.env['seeding.mission'].create(values)

    def _check(self, mission, code):
        line = mission.readiness_ids.filtered(lambda c: c.code == code)
        self.assertEqual(len(line), 1, "Check %s should appear once" % code)
        return line

    def assertCheck(self, mission, code, result, contains=None):
        line = self._check(mission, code)
        self.assertEqual(line.result, result, "%s: %s" % (code, line.explanation))
        if contains:
            self.assertIn(contains, line.explanation)

    def _ground(self, aircraft):
        return self.env['maintenance.request'].create({
            'name': 'Hydraulic leak', 'equipment_id': aircraft.id, 'maintenance_type': 'corrective',
        })

    # A sound plan ----------------------------------------------------------

    def test_sound_plan_passes_all_eleven_checks(self):
        mission = self._mission()
        self.assertTrue(mission.name.startswith('MSN/'))
        self.assertEqual(mission.state, 'proposed')
        self.assertEqual(mission.planned_by, 'human')
        self.assertEqual(mission.area_id, self.area, "Area comes from the alert")
        self.assertEqual(mission.contract_id, self.contract, "Contract found from the area")
        self.assertEqual(mission.base_id, self.base, "Base comes from the aircraft")
        self.assertEqual(mission.flare_product_id, self.flare)
        self.assertEqual(mission.readiness_ids.mapped('sequence'), list(range(1, 12)))
        self.assertEqual(set(mission.readiness_ids.mapped('result')), {'pass'},
                         mission.readiness_ids.mapped('explanation'))
        self.assertEqual(mission.readiness_state, 'pass')

    # 1 Contract active -----------------------------------------------------

    def test_check_1_contract_active_passes(self):
        self.assertCheck(self._mission(), 'contract_active', 'pass', self.contract.name)

    def test_check_1_fails_on_inactive_contract(self):
        mission = self._mission()
        self.contract.action_close()
        mission.action_rerun_checks()
        self.assertCheck(mission, 'contract_active', 'fail', 'closed, not active')

    def test_check_1_fails_outside_season(self):
        mission = self._mission()
        self.contract.season_end = self.today - timedelta(days=1)
        self.contract.season_start = self.today - timedelta(days=60)
        mission.action_rerun_checks()
        self.assertCheck(mission, 'contract_active', 'fail', 'outside the season')

    # 2 Area covered --------------------------------------------------------

    def test_check_2_area_covered_passes(self):
        self.assertCheck(self._mission(), 'area_covered', 'pass')

    def test_check_2_fails_when_area_not_in_contract(self):
        mission = self._mission(alert_id=False, area_id=self.area_outside.id, contract_id=self.contract.id)
        self.assertEqual(mission.contract_id, self.contract)
        self.assertCheck(mission, 'area_covered', 'fail', 'is not part of contract')

    # 3 Flight-hour budget --------------------------------------------------

    def test_check_3_hour_budget_passes(self):
        self.assertCheck(self._mission(), 'hour_budget', 'pass', 'used 0.0 of its 100.0')

    def test_check_3_fails_when_budget_is_used_up(self):
        self.contract.flight_hour_budget = 2
        self.env['seeding.mission'].create({
            'area_id': self.area.id, 'state': 'completed', 'flares_planned': 10, 'flares_fired': 8,
            'takeoff_at': self.now - timedelta(days=2, hours=3),
            'landing_at': self.now - timedelta(days=2, hours=1),
        })
        self.assertEqual(self.contract.hours_used, 2.0)
        self.assertCheck(self._mission(), 'hour_budget', 'fail', 'No hours are left')

    # 4 Alert still valid ---------------------------------------------------

    def test_check_4_alert_valid_passes(self):
        self.assertCheck(self._mission(), 'alert_valid', 'pass', 'is valid until')

    def test_check_4_fails_on_expired_alert(self):
        mission = self._mission()
        mission.alert_id.expires_at = self.now - timedelta(minutes=1)
        mission.action_rerun_checks()
        self.assertCheck(mission, 'alert_valid', 'fail', 'expired at')

    def test_check_4_not_applicable_without_alert(self):
        mission = self._mission(alert_id=False, area_id=self.area.id)
        self.assertCheck(mission, 'alert_valid', 'na')
        self.assertEqual(mission.readiness_state, 'pass')

    # 5 Aircraft airworthy --------------------------------------------------

    def test_check_5_airworthy_passes(self):
        self.assertCheck(self._mission(), 'aircraft_airworthy', 'pass', 'no open corrective request')

    def test_check_5_fails_with_open_corrective_request(self):
        self._ground(self.king_air)
        self.assertCheck(self._mission(), 'aircraft_airworthy', 'fail', 'open corrective maintenance request')

    def test_check_5_fails_past_service_interval(self):
        self.king_air.service_interval_hours = 0
        self.assertCheck(self._mission(), 'aircraft_airworthy', 'fail', 'since its last service')

    def test_check_5_passes_again_when_request_is_done(self):
        request = self._ground(self.king_air)
        mission = self._mission()
        self.assertCheck(mission, 'aircraft_airworthy', 'fail')
        request.stage_id = self.env['maintenance.stage'].search([('done', '=', True)], limit=1)
        mission.action_rerun_checks()
        self.assertCheck(mission, 'aircraft_airworthy', 'pass')

    # 6 Aircraft free -------------------------------------------------------

    def test_check_6_aircraft_free_passes(self):
        self.assertCheck(self._mission(), 'aircraft_free', 'pass')

    def test_check_6_fails_when_aircraft_is_flying(self):
        flying = self._mission()
        flying.action_ready()
        flying.action_launch()
        self.assertEqual(flying.state, 'launched')
        mission = self._mission(flares_planned=6)
        self.assertCheck(mission, 'aircraft_free', 'fail', flying.name)

    # 7 Rack capacity -------------------------------------------------------

    def test_check_7_rack_capacity_passes_at_full_rack(self):
        self.assertCheck(self._mission(flares_planned=24), 'rack_capacity', 'pass')

    def test_check_7_fails_above_rack_capacity(self):
        self.assertCheck(self._mission(flares_planned=25), 'rack_capacity', 'fail', 'holds 24')

    def test_check_7_fails_with_no_flares(self):
        self.assertCheck(self._mission(flares_planned=0), 'rack_capacity', 'fail', 'No flares')

    # 8 Pilot licence -------------------------------------------------------

    def test_check_8_licence_passes(self):
        self.assertCheck(self._mission(), 'pilot_licence', 'pass', 'is valid until')

    def test_check_8_fails_on_expired_licence(self):
        self.pilot.licence_expiry = self.today - timedelta(days=1)
        self.assertCheck(self._mission(), 'pilot_licence', 'fail', 'expired on')

    def test_check_8_fails_when_drone_operator_flies_crewed_aircraft(self):
        mission = self._mission(pilot_id=self.operator_crew.id)
        self.assertCheck(mission, 'pilot_licence', 'fail', 'is a drone operator')

    def test_check_8_fails_when_pilot_flies_drone(self):
        mission = self._mission(aircraft_id=self.drone.id, flares_planned=4)
        self.assertCheck(mission, 'pilot_licence', 'fail', 'a drone operator must fly it')

    def test_check_8_fails_with_no_pilot(self):
        self.assertCheck(self._mission(pilot_id=False), 'pilot_licence', 'fail', 'No pilot')

    # 9 Pilot medical -------------------------------------------------------

    def test_check_9_medical_passes(self):
        self.assertCheck(self._mission(), 'pilot_medical', 'pass')

    def test_check_9_fails_on_expired_medical(self):
        self.pilot.medical_expiry = self.today - timedelta(days=1)
        self.assertCheck(self._mission(), 'pilot_medical', 'fail', 'expired on')

    def test_check_9_not_applicable_for_drone_operator(self):
        mission = self._mission(aircraft_id=self.drone.id, pilot_id=self.operator_crew.id, flares_planned=4)
        self.assertCheck(mission, 'pilot_medical', 'na')
        self.assertEqual(mission.readiness_state, 'pass', mission.readiness_ids.mapped('explanation'))

    # 10 Pilot duty hours ---------------------------------------------------

    def test_check_10_duty_hours_passes(self):
        self.assertCheck(self._mission(), 'duty_hours', 'pass', 'under the weekly limit')

    def test_check_10_fails_at_weekly_limit(self):
        self.pilot.weekly_duty_limit = 0
        self.assertCheck(self._mission(), 'duty_hours', 'fail', 'reaches the weekly limit')

    # 11 Flares in stock ----------------------------------------------------

    def test_check_11_stock_passes(self):
        self.assertCheck(self._mission(), 'flares_in_stock', 'pass', 'holds 40')

    def test_check_11_fails_when_base_is_short(self):
        self.env['stock.quant']._update_available_quantity(self.flare, self.base.location_id, -35)
        self.assertCheck(self._mission(), 'flares_in_stock', 'fail', 'holds 5')

    def test_check_11_fails_without_flare_product(self):
        self.assertCheck(self._mission(flare_product_id=False), 'flares_in_stock', 'fail', 'No flare')

    # Blocking --------------------------------------------------------------

    def test_ready_is_blocked_then_unblocked_by_changing_aircraft(self):
        self._ground(self.king_air)
        mission = self._mission()
        action = call_kw(self.env['seeding.mission'], 'action_ready', [[mission.id]], {})
        self.assertEqual(mission.state, 'proposed')
        self.assertEqual(action['params']['type'], 'danger')
        self.assertIn('Aircraft airworthy', action['params']['message'])
        self.assertEqual(mission.readiness_state, 'fail')
        self.assertEqual(mission.failed_check_count, 1)

        mission.aircraft_id = self.cessna
        self.assertEqual(mission.readiness_state, 'pass', "Changing the aircraft re-runs the checks")
        call_kw(self.env['seeding.mission'], 'action_ready', [[mission.id]], {})
        self.assertEqual(mission.state, 'ready')

    def test_status_cannot_be_written_past_a_failed_check(self):
        mission = self._mission(flares_planned=30)
        with self.assertRaises(UserError):
            mission.write({'state': 'ready'})
        with self.assertRaises(UserError):
            mission.write({'state': 'launched'})

    def test_mission_cannot_be_created_ready_with_a_failed_check(self):
        with self.assertRaises(UserError):
            self._mission(flares_planned=30, state='ready')

    def test_ready_rechecks_conditions_that_changed_after_saving(self):
        mission = self._mission()
        self.assertEqual(mission.readiness_state, 'pass')
        self._ground(self.king_air)  # grounded after the last check run
        mission.action_ready()
        self.assertEqual(mission.state, 'proposed')
        self.assertCheck(mission, 'aircraft_airworthy', 'fail')

    def test_launch_is_refused_and_mission_returns_to_proposed(self):
        mission = self._mission()
        mission.action_ready()
        mission.alert_id.expires_at = self.now - timedelta(minutes=1)
        action = mission.action_launch()
        self.assertEqual(action['params']['type'], 'danger')
        self.assertEqual(mission.state, 'proposed')
        self.assertFalse(mission.takeoff_at)

    def test_rerun_on_ready_mission_with_failure_returns_it_to_proposed(self):
        mission = self._mission()
        mission.action_ready()
        self.pilot.medical_expiry = self.today - timedelta(days=1)
        mission.action_rerun_checks()
        self.assertEqual(mission.state, 'proposed')

    def test_plan_is_locked_once_ready(self):
        mission = self._mission()
        mission.action_ready()
        with self.assertRaises(UserError):
            mission.aircraft_id = self.cessna
        mission.action_reset_to_proposed()
        mission.aircraft_id = self.cessna
        self.assertEqual(mission.aircraft_id, self.cessna)

    # Lifecycle -------------------------------------------------------------

    def test_full_flight_counts_against_contract_hours(self):
        mission = self._mission()
        mission.action_ready()
        mission.action_launch()
        self.assertEqual(mission.state, 'launched')
        self.assertTrue(mission.takeoff_at)
        mission.write({
            'takeoff_at': self.now - timedelta(hours=2, minutes=30),
            'landing_at': self.now,
            'flares_fired': 10,
        })
        mission.action_complete()
        self.assertEqual(mission.state, 'completed')
        self.assertEqual(mission.flight_hours, 2.5)
        self.assertEqual(self.contract.hours_used, 2.5)
        self.assertEqual(self.contract.hours_remaining, 97.5)

    def test_complete_refuses_more_flares_than_loaded(self):
        mission = self._mission()
        mission.action_ready()
        mission.action_launch()
        mission.flares_fired = 13
        with self.assertRaises(UserError):
            mission.action_complete()

    def test_transitions_follow_the_order(self):
        mission = self._mission()
        with self.assertRaises(UserError):
            mission.action_launch()
        with self.assertRaises(UserError):
            mission.action_complete()
        mission.action_ready()
        with self.assertRaises(UserError):
            mission.action_ready()

    def test_alert_is_planned_then_released_on_cancel(self):
        mission = self._mission()
        alert = mission.alert_id
        self.assertEqual(alert.state, 'planned')
        self.assertEqual(alert.mission_count, 1)
        mission.action_cancel()
        self.assertEqual(alert.state, 'new')
        mission.action_reset_to_proposed()
        self.assertEqual(alert.state, 'planned')

    def test_launched_mission_cannot_be_deleted(self):
        mission = self._mission()
        mission.action_ready()
        mission.action_launch()
        with self.assertRaises(UserError):
            mission.unlink()

    def test_kanban_shows_every_status_column(self):
        self._mission()
        groups = self.env['seeding.mission'].with_context(read_group_expand=True).formatted_read_group(
            [], ['state'], ['__count'],
        )
        self.assertEqual(
            [g['state'] for g in groups],
            ['proposed', 'ready', 'launched', 'completed', 'billed', 'cancelled'],
        )

    # Access ----------------------------------------------------------------

    def test_operator_plans_and_readies_but_cannot_forge_a_check(self):
        operator = new_test_user(self.env, login='op_mission', groups='serein_ops.group_serein_operator')
        Employee = self.env['hr.employee'].with_user(operator)
        self.assertIn(self.pilot, Employee.search([('is_pilot', '=', True)]))
        mission = self.env['seeding.mission'].with_user(operator).create({
            'alert_id': self._alert().id,
            'aircraft_id': self.king_air.id,
            'pilot_id': self.pilot.id,
            'flares_planned': 12,
        })
        self.assertEqual(mission.readiness_state, 'pass')
        mission.action_ready()
        self.assertEqual(mission.state, 'ready')
        with self.assertRaises(AccessError):
            mission.readiness_ids[0].result = 'fail'
        with self.assertRaises(AccessError):
            mission.unlink()
