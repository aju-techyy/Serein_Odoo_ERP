import json
import os
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.service.model import call_kw
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.serein_ops.models.ai_service import API_KEY_ENV, DEFAULT_MODEL

TEST_KEY = 'test-key-not-real-0123'


@tagged('post_install', '-at_install')
class TestPlanner(TransactionCase):
    """Phase 4: the AI planner, its validation, the rule-based fallback,
    re-plan, and the AI activity log. Gemini is never called: the service's
    `_request` is mocked."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        today = fields.Date.context_today(cls.env.user)
        cls.now = fields.Datetime.now().replace(second=0, microsecond=0)

        cls.client = cls.env['res.partner'].create({'name': 'Northwind Water Authority', 'is_company': True})
        cls.area = cls.env['seeding.target.area'].create({
            'name': 'Northern Highlands', 'partner_id': cls.client.id,
            'latitude': 24.5, 'longitude': 55.9, 'radius_km': 40,
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
        cls.base2 = cls.env['seeding.base'].create({'name': 'Mountain Strip'})
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

        crew = {'is_pilot': True, 'weekly_duty_limit': 30, 'licence_expiry': today + timedelta(days=365)}
        cls.pilot = cls.env['hr.employee'].create({
            **crew, 'name': 'Mara Okafor', 'pilot_role': 'pilot',
            'medical_expiry': today + timedelta(days=180),
        })
        cls.operator_crew = cls.env['hr.employee'].create({
            **crew, 'name': 'Tomas Lind', 'pilot_role': 'drone_operator',
        })

        cls.snapshot = cls.env['seeding.weather.snapshot'].create({
            'area_id': cls.area.id, 'valid_for': cls.now,
            'cloud_cover_low': 80, 'cloud_cover_mid': 60, 'cape': 800, 'wind_speed': 15,
        })
        cls.Service = cls.registry['seeding.ai.service']

    # Helpers ---------------------------------------------------------------

    def _alert(self, **vals):
        return self.env['seeding.cloud.alert'].create({
            'snapshot_id': self.snapshot.id,
            'window_start': self.now,
            'window_end': self.now + timedelta(hours=3),
            **vals,
        })

    def _plan(self, **overrides):
        plan = {
            'aircraft_id': self.king_air.id,
            'pilot_id': self.pilot.id,
            'base_id': self.base.id,
            'flares_planned': 12,
            'launch_start': (self.now + timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M'),
            'launch_end': (self.now + timedelta(hours=2)).strftime('%Y-%m-%d %H:%M'),
            'reasoning': "Low cloud at 80 % and CAPE of 800 J/kg favour seeding. "
                         "King Air 350 carries the largest load.",
            'risks': ["Gusts may rise late in the window"],
        }
        plan.update(overrides)
        return plan

    def _gemini(self, plan=None, text=None, status=200):
        """A Gemini generateContent answer carrying `plan` as its JSON text."""
        body = {
            'candidates': [{
                'content': {'role': 'model', 'parts': [{'text': text if text is not None else json.dumps(plan)}]},
                'finishReason': 'STOP',
            }],
            'usageMetadata': {'promptTokenCount': 1500, 'candidatesTokenCount': 180},
            'modelVersion': DEFAULT_MODEL,
        }
        return status, json.dumps(body)

    def _ai(self, *answers, side_effect=None, key=TEST_KEY):
        """Patch the network call and the API key. Returns (request patch, env patch)."""
        env = {API_KEY_ENV: key} if key else {}
        request = patch.object(
            self.Service, '_request', autospec=True,
            side_effect=side_effect or list(answers),
        )
        return request, env

    def _run(self, func, *answers, side_effect=None, key=TEST_KEY):
        request, env = self._ai(*answers, side_effect=side_effect, key=key)
        with patch.dict(os.environ, env, clear=False), request as mocked:
            if not key:
                os.environ.pop(API_KEY_ENV, None)
            result = func()
        return result, mocked

    def _plan_alert(self, alert, *answers, **kwargs):
        return self._run(lambda: alert.env['seeding.mission.planner'].plan_alert(alert), *answers, **kwargs)

    def _sent_body(self, mocked, call=-1):
        # autospec: args are (self, model, body, api_key)
        return mocked.call_args_list[call].args[2]

    def _sent_input(self, mocked, call=-1):
        return json.loads(self._sent_body(mocked, call)['contents'][0]['parts'][0]['text'])

    def _ground(self, aircraft):
        return self.env['maintenance.request'].create({
            'name': 'Hydraulic leak', 'equipment_id': aircraft.id, 'maintenance_type': 'corrective',
        })

    def assertRulesFallback(self, mission, log_outcome, reason_contains):
        self.assertEqual(mission.planned_by, 'rules')
        log = mission.ai_log_ids
        self.assertEqual(len(log), 1)
        self.assertEqual(log.outcome, log_outcome)
        self.assertIn(reason_contains, log.outcome_reason)
        self.assertIn(reason_contains, mission.fallback_reason)

    # Valid reply -----------------------------------------------------------

    def test_valid_reply_creates_ai_planned_mission(self):
        alert = self._alert()
        mission, mocked = self._plan_alert(alert, self._gemini(self._plan()))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mission.state, 'proposed')
        self.assertEqual(mission.planned_by, 'ai')
        self.assertEqual(mission.alert_id, alert)
        self.assertEqual(alert.state, 'planned')
        self.assertEqual(mission.aircraft_id, self.king_air)
        self.assertEqual(mission.pilot_id, self.pilot)
        self.assertEqual(mission.base_id, self.base)
        self.assertEqual(mission.flare_product_id, self.flare)
        self.assertEqual(mission.flares_planned, 12)
        self.assertEqual(mission.planned_takeoff, self.now + timedelta(minutes=30))
        self.assertEqual(mission.launch_by, self.now + timedelta(hours=2))
        self.assertIn('King Air 350 carries', mission.plan_reasoning)
        self.assertIn('Gusts', mission.plan_risks)
        self.assertFalse(mission.fallback_reason)
        self.assertEqual(mission.readiness_state, 'pass')
        self.assertEqual(mission.ai_log_count, 1)
        log = mission.ai_log_ids
        self.assertEqual((log.purpose, log.outcome), ('plan', 'accepted'))
        self.assertEqual(log.alert_id, alert)
        self.assertEqual(log.model_name, DEFAULT_MODEL)
        self.assertEqual(log.http_status, 200)
        self.assertEqual((log.prompt_tokens, log.output_tokens), (1500, 180))
        self.assertIn('King Air 350 carries', log.response)
        self.assertTrue(any(DEFAULT_MODEL in body for body in mission.message_ids.mapped('body')))

    def test_request_body_uses_schema_and_operational_input(self):
        alert = self._alert()
        mission, mocked = self._plan_alert(alert, self._gemini(self._plan()))
        model, body, key = mocked.call_args.args[1:]
        self.assertEqual(model, DEFAULT_MODEL)
        self.assertEqual(key, TEST_KEY)
        config = body['generationConfig']
        self.assertEqual(config['responseMimeType'], 'application/json')
        self.assertEqual(config['responseSchema']['type'], 'OBJECT')
        self.assertIn('aircraft_id', config['responseSchema']['required'])
        self.assertIn('Aircraft airworthy', body['systemInstruction']['parts'][0]['text'])

        data = self._sent_input(mocked)
        self.assertEqual(data['alert']['reference'], alert.name)
        self.assertEqual(data['alert']['readings']['cloud_cover_low_pct'], 80)
        self.assertEqual(data['alert']['seedability_score'], alert.score)
        self.assertEqual(data['contract']['reference'], self.contract.name)
        self.assertEqual({a['id'] for a in data['aircraft']}, {self.king_air.id, self.cessna.id, self.drone.id})
        self.assertEqual({p['id'] for p in data['pilots']}, {self.pilot.id, self.operator_crew.id})
        base = [b for b in data['bases'] if b['id'] == self.base.id][0]
        self.assertEqual(base['flares_on_hand'], 40)
        self.assertNotIn('failed_checks', data)
        # The key travels only in the header argument, never in the logged body.
        self.assertNotIn(TEST_KEY, mission.ai_log_ids.request)
        self.assertNotIn(TEST_KEY, mission.ai_log_ids.response)

    def test_accepts_iso_times_with_zone(self):
        start = (self.now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:00Z')
        mission, _m = self._plan_alert(self._alert(), self._gemini(self._plan(launch_start=start)))
        self.assertEqual(mission.planned_by, 'ai')
        self.assertEqual(mission.planned_takeoff, self.now + timedelta(hours=1))

    def test_launch_in_the_past_is_moved_to_now(self):
        alert = self._alert(window_start=self.now - timedelta(hours=1))
        start = (self.now - timedelta(minutes=50)).strftime('%Y-%m-%d %H:%M')
        mission, _m = self._plan_alert(alert, self._gemini(self._plan(launch_start=start)))
        self.assertEqual(mission.planned_by, 'ai')
        self.assertGreaterEqual(mission.planned_takeoff, self.now)

    # Rejected replies ------------------------------------------------------

    def test_malformed_reply_is_rejected_and_rules_plan(self):
        mission, _m = self._plan_alert(self._alert(), self._gemini(text='{"aircraft_id": 3, "pilot'))
        self.assertRulesFallback(mission, 'rejected', 'not valid JSON')
        self.assertEqual(mission.readiness_state, 'pass')

    def test_reply_missing_a_field_is_rejected(self):
        plan = self._plan()
        del plan['flares_planned']
        mission, _m = self._plan_alert(self._alert(), self._gemini(plan))
        self.assertRulesFallback(mission, 'rejected', 'missing: flares_planned')

    def test_unknown_aircraft_is_rejected(self):
        mission, _m = self._plan_alert(self._alert(), self._gemini(self._plan(aircraft_id=999999)))
        self.assertRulesFallback(mission, 'rejected', 'Aircraft id 999999 is not in the fleet')
        self.assertNotEqual(mission.aircraft_id.id, 999999)

    def test_unknown_pilot_is_rejected(self):
        mission, _m = self._plan_alert(self._alert(), self._gemini(self._plan(pilot_id=888888)))
        self.assertRulesFallback(mission, 'rejected', 'Pilot id 888888 is not in the crew')

    def test_base_other_than_home_base_is_rejected(self):
        mission, _m = self._plan_alert(self._alert(), self._gemini(self._plan(base_id=self.base2.id)))
        self.assertRulesFallback(mission, 'rejected', 'is not the home base of King Air 350')

    def test_impossible_flare_load_is_rejected(self):
        mission, _m = self._plan_alert(self._alert(), self._gemini(self._plan(flares_planned=0)))
        self.assertRulesFallback(mission, 'rejected', 'not a possible load')

    def test_text_instead_of_number_is_rejected(self):
        mission, _m = self._plan_alert(self._alert(), self._gemini(self._plan(flares_planned='twelve')))
        self.assertRulesFallback(mission, 'rejected', 'is not a whole number')

    def test_launch_outside_alert_window_is_rejected(self):
        start = (self.now + timedelta(hours=5)).strftime('%Y-%m-%d %H:%M')
        end = (self.now + timedelta(hours=6)).strftime('%Y-%m-%d %H:%M')
        mission, _m = self._plan_alert(
            self._alert(), self._gemini(self._plan(launch_start=start, launch_end=end)),
        )
        self.assertRulesFallback(mission, 'rejected', 'outside the alert window')

    def test_blocked_answer_without_text_is_rejected(self):
        body = json.dumps({'candidates': [], 'promptFeedback': {'blockReason': 'OTHER'}})
        mission, _m = self._plan_alert(self._alert(), (200, body))
        self.assertRulesFallback(mission, 'rejected', 'contained no text (OTHER)')

    # Failed calls ----------------------------------------------------------

    def test_network_failure_falls_back_to_rules(self):
        mission, mocked = self._plan_alert(
            self._alert(), side_effect=requests.exceptions.ConnectionError("Name resolution failed"),
        )
        self.assertEqual(mocked.call_count, 1)
        self.assertRulesFallback(mission, 'failed', 'Could not reach Gemini: ConnectionError')
        self.assertEqual(mission.state, 'proposed')

    def test_rate_limit_falls_back_without_retry(self):
        error = json.dumps({'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED', 'message': 'Quota exceeded.'}})
        mission, mocked = self._plan_alert(self._alert(), (429, error))
        self.assertEqual(mocked.call_count, 1, "A rate limit is never retried")
        self.assertRulesFallback(mission, 'failed', 'HTTP 429: RESOURCE_EXHAUSTED Quota exceeded.')
        self.assertEqual(mission.ai_log_ids.http_status, 429)

    def test_missing_key_plans_by_rules_without_calling(self):
        alert = self._alert()
        mission, mocked = self._plan_alert(alert, key=None)
        self.assertEqual(mocked.call_count, 0)
        self.assertRulesFallback(mission, 'failed', 'No Gemini API key')
        self.assertTrue(mission.ai_log_ids.request, "The request that would have been sent is logged")
        self.assertEqual(alert.state, 'planned')

    # The rule-based planner ------------------------------------------------

    def test_rules_pick_first_assets_that_pass_every_check(self):
        self._ground(self.king_air)
        mission, _m = self._plan_alert(self._alert(), key=None)
        self.assertEqual(mission.aircraft_id, self.cessna, "King Air is grounded, Cessna is next by rack size")
        self.assertEqual(mission.pilot_id, self.pilot, "A drone operator cannot fly the Cessna")
        self.assertEqual(mission.flares_planned, 16, "Full rack, as stock allows")
        self.assertEqual(mission.readiness_state, 'pass')
        self.assertIn('first aircraft and pilot that pass all eleven', mission.plan_reasoning)
        self.assertEqual(mission.launch_by, mission.alert_id.window_end)

    def test_rules_load_only_what_the_base_holds(self):
        self.env['stock.quant']._update_available_quantity(self.flare, self.base.location_id, -30)
        mission, _m = self._plan_alert(self._alert(), key=None)
        self.assertEqual(mission.aircraft_id, self.king_air)
        self.assertEqual(mission.flares_planned, 10)
        self.assertEqual(mission.readiness_state, 'pass')

    def test_rules_still_propose_when_nothing_passes(self):
        self.contract.action_close()
        mission, _m = self._plan_alert(self._alert(), key=None)
        self.assertEqual(mission.planned_by, 'rules')
        self.assertEqual(mission.readiness_state, 'fail')
        self.assertIn('No aircraft and pilot pass every readiness check', mission.plan_reasoning)
        self.assertIn('Contract active', mission.plan_reasoning)

    def test_evaluating_candidates_writes_nothing(self):
        alert = self._alert()
        missions = self.env['seeding.mission'].search_count([])
        checks = self.env['seeding.readiness.check'].search_count([])
        failed = self.env['seeding.mission']._evaluate_plan({
            'alert_id': alert.id, 'aircraft_id': self.drone.id, 'pilot_id': self.pilot.id,
            'base_id': self.base.id, 'flare_product_id': self.flare.id, 'flares_planned': 12,
        })
        self.assertEqual({name for name, _e in failed}, {'Rack capacity', 'Pilot licence'})
        self.assertEqual(self.env['seeding.mission'].search_count([]), missions)
        self.assertEqual(self.env['seeding.readiness.check'].search_count([]), checks)

    # Re-plan ---------------------------------------------------------------

    def _failed_ai_mission(self):
        """An AI-planned mission on the King Air, then the King Air grounded."""
        mission, _m = self._plan_alert(self._alert(), self._gemini(self._plan()))
        self._ground(self.king_air)
        mission.action_rerun_checks()
        self.assertEqual(mission.readiness_state, 'fail')
        self.assertTrue(mission.can_replan)
        return mission

    def _replan(self, mission, *answers, **kwargs):
        return self._run(lambda: mission.action_replan(), *answers, **kwargs)

    def test_replan_sends_failed_checks_and_applies_new_plan(self):
        mission = self._failed_ai_mission()
        new_plan = self._plan(aircraft_id=self.cessna.id, flares_planned=16,
                              reasoning="King Air 350 is grounded. Cessna 340 is airworthy.")
        _r, mocked = self._replan(mission, self._gemini(new_plan))

        data = self._sent_input(mocked)
        self.assertEqual(data['previous_plan']['aircraft_id'], self.king_air.id)
        self.assertEqual(data['failed_checks'][0]['check'], 'Aircraft airworthy')
        self.assertIn('Hydraulic leak', data['failed_checks'][0]['explanation'])
        self.assertIn('This is a re-plan', self._sent_body(mocked)['systemInstruction']['parts'][0]['text'])

        self.assertEqual(mission.aircraft_id, self.cessna)
        self.assertEqual(mission.flares_planned, 16)
        self.assertEqual(mission.planned_by, 'ai')
        self.assertEqual(mission.replan_count, 1)
        self.assertEqual(mission.readiness_state, 'pass')
        self.assertFalse(mission.can_replan)
        self.assertIn('Cessna 340 is airworthy', mission.plan_reasoning)
        logs = mission.ai_log_ids.sorted('id')
        self.assertEqual(logs.mapped('purpose'), ['plan', 'replan'])
        self.assertEqual(logs.mapped('outcome'), ['accepted', 'accepted'])
        self.assertTrue(any('Re-plan round 1 of 2' in body for body in mission.message_ids.mapped('body')))
        mission.action_ready()
        self.assertEqual(mission.state, 'ready')

    def test_replan_repeating_the_failed_plan_is_rejected(self):
        mission = self._failed_ai_mission()
        self._replan(mission, self._gemini(self._plan()))
        replan_log = mission.ai_log_ids.sorted('id')[-1]
        self.assertEqual(replan_log.outcome, 'rejected')
        self.assertIn('same as the plan that failed', replan_log.outcome_reason)
        self.assertEqual(mission.planned_by, 'rules')
        self.assertEqual(mission.aircraft_id, self.cessna)
        self.assertEqual(mission.readiness_state, 'pass')
        self.assertEqual(mission.replan_count, 1)

    def test_replan_failure_falls_back_to_rules(self):
        mission = self._failed_ai_mission()
        self._replan(mission, key=None)
        self.assertEqual(mission.planned_by, 'rules')
        self.assertIn('No Gemini API key', mission.fallback_reason)
        self.assertEqual(mission.aircraft_id, self.cessna)
        self.assertEqual(mission.readiness_state, 'pass')

    def test_replan_limited_to_two_rounds(self):
        mission = self._failed_ai_mission()
        # Round 1 and 2: the model insists on a drone with too many flares.
        bad = self._plan(aircraft_id=self.drone.id, pilot_id=self.operator_crew.id, flares_planned=12)
        bad2 = dict(bad, flares_planned=10)
        self._replan(mission, self._gemini(bad))
        self.assertEqual((mission.replan_count, mission.readiness_state), (1, 'fail'))
        self._replan(mission, self._gemini(bad2))
        self.assertEqual((mission.replan_count, mission.readiness_state), (2, 'fail'))
        self.assertFalse(mission.can_replan)
        with self.assertRaisesRegex(UserError, 'the maximum'):
            self._replan(mission, self._gemini(self._plan(aircraft_id=self.cessna.id)))
        self.assertEqual(len(mission.ai_log_ids), 3, "The refused third round calls nothing")

    def test_replan_with_passing_checks_calls_nothing(self):
        mission, _m = self._plan_alert(self._alert(), self._gemini(self._plan()))
        result, mocked = self._replan(mission)
        self.assertEqual(mocked.call_count, 0)
        self.assertEqual(result['params']['title'], 'Nothing to re-plan')
        self.assertEqual(mission.replan_count, 0)

    def test_replan_not_offered_when_only_contract_checks_fail(self):
        mission, _m = self._plan_alert(self._alert(), self._gemini(self._plan()))
        self.contract.action_close()
        mission.action_rerun_checks()
        self.assertEqual(mission.readiness_state, 'fail')
        self.assertFalse(mission.can_replan, "No aircraft or pilot can fix a closed contract")
        result, mocked = self._replan(mission)
        self.assertEqual(mocked.call_count, 0)
        self.assertEqual(result['params']['title'], 'Re-plan cannot fix these checks')
        self.assertIn('Contract active', result['params']['message'])
        self.assertEqual(mission.replan_count, 0, "No round is used")
        self.assertEqual(len(mission.ai_log_ids), 1)

    def test_replan_offered_when_an_asset_check_also_fails(self):
        mission, _m = self._plan_alert(self._alert(), self._gemini(self._plan()))
        self.contract.action_close()
        self._ground(self.king_air)
        mission.action_rerun_checks()
        self.assertTrue(mission.can_replan)
        _r, mocked = self._replan(mission, self._gemini(self._plan(aircraft_id=self.cessna.id)))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mission.aircraft_id, self.cessna)
        self.assertEqual(mission.replan_count, 1)
        self.assertEqual([c.name for c in mission.readiness_ids if c.result == 'fail'], ['Contract active'])

    def test_replan_refused_outside_proposed_or_without_alert(self):
        mission, _m = self._plan_alert(self._alert(), self._gemini(self._plan()))
        mission.action_ready()
        with self.assertRaisesRegex(UserError, 'Only a proposed mission'):
            self._replan(mission)
        manual = self.env['seeding.mission'].create({
            'area_id': self.area.id, 'aircraft_id': self.drone.id, 'pilot_id': self.pilot.id,
            'flares_planned': 12,
        })
        self.assertFalse(manual.can_replan)
        with self.assertRaisesRegex(UserError, 'no cloud alert'):
            self._replan(manual)

    # Alert rules -----------------------------------------------------------

    def test_only_a_new_unexpired_alert_can_be_planned(self):
        dismissed = self._alert()
        dismissed.action_dismiss()
        with self.assertRaisesRegex(UserError, 'Only a new alert'):
            self._plan_alert(dismissed, self._gemini(self._plan()))
        stale = self._alert(window_start=self.now - timedelta(hours=3), window_end=self.now - timedelta(hours=1))
        with self.assertRaisesRegex(UserError, 'has expired'):
            self._plan_alert(stale, self._gemini(self._plan()))
        self.assertFalse(self.env['seeding.ai.log'].search([('alert_id', 'in', (dismissed + stale).ids)]))

    def test_plan_with_ai_button_through_call_kw(self):
        alert = self._alert()
        action, _m = self._run(
            lambda: call_kw(self.env['seeding.cloud.alert'], 'action_plan_with_ai', [alert.ids], {}),
            self._gemini(self._plan()),
        )
        self.assertEqual(action['res_model'], 'seeding.mission')
        mission = self.env['seeding.mission'].browse(action['res_id'])
        self.assertEqual(mission.planned_by, 'ai')
        self.assertEqual(mission.alert_id, alert)

    def test_replan_button_through_call_kw(self):
        mission = self._failed_ai_mission()
        new_plan = self._plan(aircraft_id=self.cessna.id)
        self._run(
            lambda: call_kw(self.env['seeding.mission'], 'action_replan', [mission.ids], {}),
            self._gemini(new_plan),
        )
        self.assertEqual(mission.aircraft_id, self.cessna)

    # Access ----------------------------------------------------------------

    def test_operator_plans_with_ai_and_reads_the_log_but_cannot_change_it(self):
        operator = new_test_user(self.env, login='op_planner', groups='serein_ops.group_serein_operator')
        alert = self._alert().with_user(operator)
        mission, _m = self._plan_alert(alert, self._gemini(self._plan()))
        self.assertEqual(mission.env.user, operator)
        self.assertEqual(mission.planned_by, 'ai')
        log = self.env['seeding.ai.log'].with_user(operator).search([('mission_id', '=', mission.id)])
        self.assertEqual(len(log), 1)
        self.assertEqual(log.user_id, operator)
        with self.assertRaises(AccessError):
            log.outcome = 'accepted'
        with self.assertRaises(AccessError):
            log.unlink()
        with self.assertRaises(AccessError):
            self.env['seeding.ai.log'].with_user(operator).create({'purpose': 'plan', 'outcome': 'accepted'})

    def test_manager_cannot_edit_the_log_either(self):
        mission, _m = self._plan_alert(self._alert(), self._gemini(self._plan()))
        manager = new_test_user(self.env, login='mgr_planner', groups='serein_ops.group_serein_manager')
        with self.assertRaises(AccessError):
            mission.ai_log_ids.with_user(manager).write({'outcome': 'failed'})
        with self.assertRaises(AccessError):
            mission.ai_log_ids.with_user(manager).unlink()
