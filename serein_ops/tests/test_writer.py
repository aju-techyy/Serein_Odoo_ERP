import json
import os
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import UserError
from odoo.service.model import call_kw
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.serein_ops.models.ai_service import API_KEY_ENV, DEFAULT_MODEL
from odoo.addons.serein_ops.models.mission_writer import unknown_figures

TEST_KEY = 'test-key-not-real-0123'
REPORT = 'serein_ops.action_report_seeding_mission'


@tagged('post_install', '-at_install')
class TestWriter(TransactionCase):
    """Phase 5: the crew briefing, the client summary and its review, and the
    mission report. Gemini is never called: the service's `_request` is mocked."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.startClassPatcher(patch.dict(os.environ, {API_KEY_ENV: ''}))
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
        cls.flare = cls.env['product.product'].create({
            'name': 'Hygroscopic Flare 1 kg', 'type': 'consu', 'is_storable': True,
            'is_seeding_flare': True, 'grams_per_flare': 1000,
        })
        cls.env['stock.quant']._update_available_quantity(cls.flare, cls.base.location_id, 40)
        cls.king_air = cls.env['maintenance.equipment'].create({
            'name': 'King Air 350', 'is_aircraft': True, 'aircraft_type': 'crewed',
            'home_base_id': cls.base.id, 'service_interval_hours': 100,
            'tail_number': 'A6-SRA', 'rack_capacity': 24,
        })
        cls.pilot = cls.env['hr.employee'].create({
            'name': 'Mara Okafor', 'is_pilot': True, 'pilot_role': 'pilot', 'weekly_duty_limit': 30,
            'licence_expiry': today + timedelta(days=365), 'medical_expiry': today + timedelta(days=180),
        })
        cls.snapshot = cls.env['seeding.weather.snapshot'].create({
            'area_id': cls.area.id, 'valid_for': cls.now,
            'cloud_cover_low': 80, 'cloud_cover_mid': 60, 'cloud_cover_high': 20, 'cape': 800,
            'lifted_index': -2.5, 'wind_speed': 15, 'wind_gusts': 30, 'wind_direction': 220,
            'relative_humidity': 70, 'precipitation_probability': 40, 'freezing_level': 4200,
        })
        cls.Service = cls.registry['seeding.ai.service']

    # Helpers ---------------------------------------------------------------

    def _mission(self):
        alert = self.env['seeding.cloud.alert'].create({
            'snapshot_id': self.snapshot.id,
            'window_start': self.now,
            'window_end': self.now + timedelta(hours=3),
        })
        mission = self.env['seeding.mission'].create({
            'alert_id': alert.id, 'aircraft_id': self.king_air.id, 'pilot_id': self.pilot.id,
            'flares_planned': 12, 'launch_by': self.now + timedelta(hours=2),
        })
        self.assertEqual(mission.readiness_state, 'pass')
        return mission

    def _gemini(self, data=None, text=None, status=200):
        body = {
            'candidates': [{
                'content': {'role': 'model', 'parts': [{'text': text if text is not None else json.dumps(data)}]},
                'finishReason': 'STOP',
            }],
            'usageMetadata': {'promptTokenCount': 900, 'candidatesTokenCount': 250},
            'modelVersion': DEFAULT_MODEL,
        }
        return status, json.dumps(body)

    def _run(self, func, *answers, side_effect=None, key=TEST_KEY):
        request = patch.object(self.Service, '_request', autospec=True,
                               side_effect=side_effect or list(answers))
        with patch.dict(os.environ, {API_KEY_ENV: key or ''}), request as mocked:
            result = func()
        return result, mocked

    def _sent_input(self, mocked, call=-1):
        body = mocked.call_args_list[call].args[2]
        return json.loads(body['contents'][0]['parts'][0]['text'])

    def _ready(self, mission, *answers, **kwargs):
        return self._run(mission.action_ready, *answers, **kwargs)

    def _fly(self, mission, fired=10):
        """Ready (template briefing), launched 90 minutes ago, `fired` flares."""
        if mission.state == 'proposed':
            mission.action_ready()
        mission.action_launch()
        mission.write({
            'takeoff_at': self.now - timedelta(minutes=90),
            'landing_at': self.now,
            'flares_fired': fired,
        })
        return mission

    def _complete(self, mission, *answers, **kwargs):
        return self._run(mission.action_complete, *answers, **kwargs)

    def _briefing(self, **overrides):
        data = {
            'overview': "Mission over Northern Highlands for Northwind Water Authority, "
                        "flown by Mara Okafor in the King Air 350.",
            'weather': "Low cloud 80 % and mid cloud 60 % with CAPE of 800 J/kg. "
                       "Wind 15 km/h from 220 degrees, gusts 30 km/h.",
            'seeding_run': "Take off at the planned time; the seedable window closes after the launch-by time.",
            'aircraft_and_load': "King Air 350, tail A6-SRA, from Coastal Airfield with 12 flares "
                                 "of 1000 g, rack capacity 24.",
            'hazards': ["Gusts to 30 km/h", "Freezing level 4200 m"],
            'abort_criteria': ["Seedable cloud dissipates before the launch-by time"],
        }
        data.update(overrides)
        return data

    def _summary_text(self, flares='10'):
        return (
            "The King Air 350 flew a seeding mission over Northern Highlands for Northwind Water "
            "Authority, with Mara Okafor at the controls. The flight lasted 1.5 hours.\n\n"
            "FIRED of the 12 flares loaded were fired, releasing 10,000 g of seeding material. "
            "Low cloud stood at 80 % and mid cloud at 60 %, with CAPE of 800 J/kg and a "
            "seedability score of 84 out of 100.\n\n"
            "Observed rainfall over the area has not been recorded yet."
        ).replace('FIRED', flares)

    def _summary_answer(self, text):
        """The three-paragraph answer shape, from a text with blank-line breaks."""
        flight, conditions, results = text.split('\n\n')
        return {'flight': flight, 'conditions': conditions, 'results': results}

    def _report_html(self, mission):
        html, _type = self.env['ir.actions.report']._render_qweb_html(REPORT, mission.ids)
        return html.decode()

    # Figure check ----------------------------------------------------------

    def test_figure_check_accepts_input_figures_and_rounding(self):
        payload = {'hours': 1.52, 'grams': 10000.0, 'takeoff': '2026-11-12 14:05', 'aircraft': 'King Air 350'}
        self.assertEqual(unknown_figures("1.5 hours, about 2 hours, 1.52 h", payload), [])
        self.assertEqual(unknown_figures("10,000 g and 10000 g", payload), [])
        self.assertEqual(unknown_figures("took off at 14:05 UTC on 12 November 2026", payload), [])
        self.assertEqual(unknown_figures("the King Air 350", payload), [])

    def test_figure_check_reports_invented_figures(self):
        payload = {'hours': 1.52, 'fired': 10}
        self.assertEqual(unknown_figures("1.6 hours", payload), ['1.6'])
        self.assertEqual(unknown_figures("fired 96 flares at 3000 m", payload), ['96', '3000'])

    # Crew briefing ----------------------------------------------------------

    def test_ready_drafts_ai_briefing(self):
        mission = self._mission()
        _r, mocked = self._ready(mission, self._gemini(self._briefing()))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mission.state, 'ready')
        self.assertEqual(mission.briefing_source, 'ai')
        self.assertEqual(mission.briefing_model, DEFAULT_MODEL)
        self.assertFalse(mission.briefing_fallback_reason)
        for heading in ('MISSION', 'WEATHER', 'SEEDING RUN', 'AIRCRAFT AND LOAD', 'HAZARDS', 'ABORT CRITERIA'):
            self.assertIn(heading, mission.briefing)
        self.assertIn('• Gusts to 30 km/h', mission.briefing)
        self.assertIn('AI-drafted (%s)' % DEFAULT_MODEL, mission.briefing_note)
        self.assertIn('Not yet reviewed', mission.briefing_note)
        log = mission.ai_log_ids.filtered(lambda l: l.purpose == 'briefing')
        self.assertEqual((log.outcome, log.alert_id), ('accepted', mission.alert_id))
        self.assertTrue(any('Crew briefing drafted by AI' in b for b in mission.message_ids.mapped('body')))

    def test_briefing_input_is_the_plan_and_the_readings(self):
        mission = self._mission()
        _r, mocked = self._ready(mission, self._gemini(self._briefing()))
        sent = self._sent_input(mocked)
        self.assertEqual(sent['mission'], mission.name)
        self.assertEqual(sent['aircraft']['name'], 'King Air 350')
        self.assertEqual(sent['crew'], {'name': 'Mara Okafor', 'role': 'pilot'})
        self.assertEqual(sent['load']['flares_planned'], 12)
        self.assertEqual(sent['load']['material_planned_g'], 12000)
        self.assertEqual(sent['conditions_at_alert']['cloud_cover_low_pct'], 80)
        self.assertEqual(sent['readiness']['checks_failed'], 0)
        body = mocked.call_args.args[2]
        self.assertEqual(body['generationConfig']['responseSchema']['required'][0], 'overview')

    def test_briefing_with_invented_figure_falls_back_to_template(self):
        mission = self._mission()
        bad = self._briefing(abort_criteria=["Abort if gusts exceed 65 km/h"])
        self._ready(mission, self._gemini(bad))
        self.assertEqual(mission.state, 'ready', "A rejected briefing never blocks the move")
        self.assertEqual(mission.briefing_source, 'template')
        log = mission.ai_log_ids.filtered(lambda l: l.purpose == 'briefing')
        self.assertEqual(log.outcome, 'rejected')
        self.assertIn('65', log.outcome_reason)
        self.assertIn('rejected', mission.briefing_fallback_reason)
        self.assertNotIn('65 km/h', mission.briefing)
        self.assertIn('Written from the mission record', mission.briefing_note)

    def test_briefing_missing_section_or_no_abort_criteria_is_rejected(self):
        for bad in (self._briefing(abort_criteria=[]), {'overview': 'Short.'}, self._briefing(weather=7)):
            mission = self._mission()
            self._ready(mission, self._gemini(bad))
            self.assertEqual(mission.briefing_source, 'template')
            self.assertEqual(mission.ai_log_ids.filtered(lambda l: l.purpose == 'briefing').outcome, 'rejected')

    def test_abort_criterion_repeating_a_hazard_is_rejected(self):
        # Exact repeat of a hazard, a hazard inside a longer line, and a planner risk.
        cases = [
            self._briefing(abort_criteria=["Gusts to 30 km/h"]),
            self._briefing(abort_criteria=["Abort: gusts to 30 km/h."]),
            self._briefing(hazards=[], abort_criteria=["Gusts may strengthen late in the window"]),
        ]
        for bad in cases:
            mission = self._mission()
            mission.plan_risks = '• Gusts may strengthen late in the window'
            self._ready(mission, self._gemini(bad))
            log = mission.ai_log_ids.filtered(lambda l: l.purpose == 'briefing')
            self.assertEqual(log.outcome, 'rejected', bad['abort_criteria'])
            self.assertIn('repeats a hazard', log.outcome_reason)
            self.assertEqual(mission.briefing_source, 'template')

    def test_ai_abort_criteria_start_with_launch_by_from_the_record(self):
        mission = self._mission()
        launch_by = (self.now + timedelta(hours=2)).strftime('%Y-%m-%d %H:%M')
        data = self._briefing(abort_criteria=[
            "Do not take off after %s UTC" % launch_by[-5:],
            "Seedable cloud dissipates before takeoff",
        ])
        self._ready(mission, self._gemini(data))
        self.assertEqual(mission.briefing_source, 'ai')
        abort = mission.briefing.split('ABORT CRITERIA\n')[1].splitlines()
        self.assertEqual(abort, [
            '• Do not take off after %s UTC.' % launch_by,
            '• Seedable cloud dissipates before takeoff',
        ], "The record's launch-by line comes first and the model's copy of it is dropped")

    def test_briefing_network_failure_or_no_key_uses_template(self):
        mission = self._mission()
        _r, mocked = self._ready(mission, side_effect=requests.ConnectionError("offline"))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual((mission.state, mission.briefing_source), ('ready', 'template'))
        self.assertIn('unavailable', mission.briefing_fallback_reason)

        other = self._mission()
        _r, mocked = self._ready(other, key=None)
        self.assertEqual(mocked.call_count, 0)
        self.assertEqual(other.briefing_source, 'template')
        self.assertIn('No Gemini API key', other.briefing_fallback_reason)

    def test_template_briefing_holds_only_record_figures(self):
        mission = self._mission()
        mission.action_ready()
        writer = self.env['seeding.mission.writer']
        self.assertEqual(unknown_figures(mission.briefing, writer._briefing_input(mission)), [])
        for expected in ('King Air 350', 'A6-SRA', 'Coastal Airfield', '12 x', '12000 g', 'low cloud 80 %',
                         'CAPE 800 J/kg', 'Seedability score 84 of 100', 'Gusts up to 30 km/h'):
            self.assertIn(expected, mission.briefing)

    def test_blocked_ready_drafts_no_briefing(self):
        mission = self._mission()
        self.env['maintenance.request'].create({
            'name': 'Hydraulic leak', 'equipment_id': self.king_air.id, 'maintenance_type': 'corrective',
        })
        _r, mocked = self._ready(mission, self._gemini(self._briefing()))
        self.assertEqual(mocked.call_count, 0)
        self.assertEqual(mission.state, 'proposed')
        self.assertFalse(mission.briefing)

    def test_briefing_review_locks_it(self):
        mission = self._mission()
        mission.action_ready()
        mission.write({'briefing': mission.briefing + '\nCrew note: fuel to tabs.'})
        call_kw(self.env['seeding.mission'], 'action_review_briefing', [[mission.id]], {})
        self.assertEqual(mission.briefing_reviewed_by, self.env.user)
        self.assertIn('Reviewed by %s' % self.env.user.name, mission.briefing_note)
        with self.assertRaisesRegex(UserError, 'reviewed'):
            mission.write({'briefing': 'Changed'})
        with self.assertRaisesRegex(UserError, 'unreviewed briefing'):
            mission.action_redraft_briefing()

    def test_redraft_briefing_calls_again(self):
        mission = self._mission()
        mission.action_ready()
        self.assertEqual(mission.briefing_source, 'template')
        _r, mocked = self._run(mission.action_redraft_briefing, self._gemini(self._briefing()))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mission.briefing_source, 'ai')

    # Client summary ----------------------------------------------------------

    def test_complete_drafts_ai_summary_for_review(self):
        mission = self._fly(self._mission())
        _r, mocked = self._complete(mission, self._gemini(self._summary_answer(self._summary_text())))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mission.state, 'completed')
        self.assertEqual(mission.summary_source, 'ai')
        self.assertEqual(mission.summary_state, 'to_review')
        self.assertIn('10 of the 12 flares', mission.client_summary)
        self.assertIn('Not yet reviewed', mission.summary_note)
        log = mission.ai_log_ids.filtered(lambda l: l.purpose == 'summary')
        self.assertEqual(log.outcome, 'accepted')

    def test_summary_input_is_the_flown_mission(self):
        mission = self._fly(self._mission())
        _r, mocked = self._complete(mission, self._gemini(self._summary_answer(self._summary_text())))
        sent = self._sent_input(mocked)
        self.assertEqual(sent['flight']['flight_time_hours'], 1.5)
        self.assertEqual(sent['flight']['flight_time_minutes'], 90)
        self.assertEqual(sent['flares'], {
            'product': 'Hygroscopic Flare 1 kg', 'loaded': 12, 'fired': 10, 'grams_per_flare': 1000,
            'material_released_g': 10000, 'material_released_kg': 10,
        })
        self.assertEqual(sent['client'], 'Northwind Water Authority')
        self.assertIsNone(sent['observed_rainfall_mm'])
        self.assertEqual(sent['readiness_before_launch']['checks_failed'], 0)

    def test_summary_with_wrong_figure_falls_back_to_template(self):
        mission = self._fly(self._mission())
        self._complete(mission, self._gemini(self._summary_answer(self._summary_text(flares='96'))))
        self.assertEqual(mission.state, 'completed')
        self.assertEqual(mission.summary_source, 'template')
        log = mission.ai_log_ids.filtered(lambda l: l.purpose == 'summary')
        self.assertEqual(log.outcome, 'rejected')
        self.assertIn('96', log.outcome_reason)
        self.assertNotIn('96', mission.client_summary)
        self.assertIn('10 of the 12', mission.client_summary)

    def test_summary_must_state_flares_fired(self):
        mission = self._fly(self._mission())
        answer = {
            'flight': "The King Air 350 flew over Northern Highlands for 1.5 hours.",
            'conditions': "Low cloud stood at 80 %.",
            'results': "Seeding material was released over the area.",
        }
        self._complete(mission, self._gemini(answer))
        log = mission.ai_log_ids.filtered(lambda l: l.purpose == 'summary')
        self.assertEqual(log.outcome, 'rejected')
        self.assertIn('10 flares fired', log.outcome_reason)
        self.assertEqual(mission.summary_source, 'template')

    def test_summary_comes_as_three_paragraphs(self):
        mission = self._fly(self._mission())
        answer = self._summary_answer(self._summary_text())
        answer['flight'] = '  ' + answer['flight'].replace(' flew ', '\n flew ') + '  '
        _r, mocked = self._complete(mission, self._gemini(answer))
        self.assertEqual(mission.summary_source, 'ai')
        paragraphs = mission.client_summary.split('\n\n')
        self.assertEqual(len(paragraphs), 3)
        self.assertTrue(paragraphs[0].startswith('The King Air 350 flew a seeding mission'))
        self.assertIn('10 of the 12 flares', paragraphs[1])
        schema = mocked.call_args.args[2]['generationConfig']['responseSchema']
        self.assertEqual(schema['required'], ['flight', 'conditions', 'results'])

        other = self._fly(self._mission())
        answer = self._summary_answer(self._summary_text())
        del answer['results']
        self._complete(other, self._gemini(answer))
        log = other.ai_log_ids.filtered(lambda l: l.purpose == 'summary')
        self.assertEqual(log.outcome, 'rejected')
        self.assertIn('paragraph results', log.outcome_reason)
        self.assertEqual(other.summary_source, 'template')

    def test_texts_are_told_what_the_score_and_wind_mean(self):
        mission = self._fly(self._mission())
        _r, mocked = self._complete(mission, self._gemini(self._summary_answer(self._summary_text())))
        conditions = self._sent_input(mocked)['conditions_at_alert']
        self.assertIn('High cloud does not count', conditions['score_counts'])
        self.assertEqual(conditions['wind_light_up_to_kmh'], 20)
        self.assertEqual(conditions['wind_scores_nothing_from_kmh'], 50)
        instructions = mocked.call_args.args[2]['systemInstruction']['parts'][0]['text']
        self.assertIn('never give high cloud as a reason for the score', instructions)
        planner = self.env['seeding.mission.planner']._instructions(replan=False)
        self.assertIn('up to 20 km/h are light', planner)
        self.assertIn('is not a risk', planner)
        # The thresholds are record figures, so a briefing may quote them.
        payload = self.env['seeding.mission.writer']._briefing_input(mission)
        self.assertEqual(unknown_figures("Gusts of 30 km/h are above the 20 km/h light-wind level.", payload), [])

    def test_summary_failure_never_blocks_completion(self):
        for kwargs in ({'side_effect': requests.Timeout("slow")}, {'key': None},
                       {'side_effect': [(503, '{"error": {"status": "UNAVAILABLE"}}')]},
                       {'side_effect': [(200, 'not json')]}):
            mission = self._fly(self._mission())
            self._complete(mission, **kwargs)
            self.assertEqual(mission.state, 'completed')
            self.assertEqual(mission.summary_source, 'template')
            self.assertEqual(mission.summary_state, 'to_review')

    def test_template_summary_matches_the_mission_figures(self):
        mission = self._fly(self._mission())
        mission.action_complete()
        summary = mission.client_summary
        writer = self.env['seeding.mission.writer']
        self.assertEqual(unknown_figures(summary, writer._summary_input(mission)), [])
        for expected in (mission.name, 'Northwind Water Authority', 'Northern Highlands', 'King Air 350',
                         'Mara Okafor', '1.5 hours', '10 of the 12', '10000 g (10 kg)',
                         'low cloud at 80 %', 'CAPE of 800 J/kg', '84 out of 100',
                         'has not been recorded yet', mission.contract_id.name):
            self.assertIn(expected, summary)

    def test_material_released_from_flares_fired(self):
        mission = self._fly(self._mission(), fired=7)
        self.assertEqual(mission.material_released_g, 7000)

    # Review and report ---------------------------------------------------------

    def test_summary_edit_approve_reopen(self):
        mission = self._fly(self._mission())
        self._complete(mission, self._gemini(self._summary_answer(self._summary_text())))
        edited = mission.client_summary + "\n\nThank you for your continued trust."
        mission.write({'client_summary': edited})
        call_kw(self.env['seeding.mission'], 'action_approve_summary', [[mission.id]], {})
        self.assertEqual(mission.summary_state, 'approved')
        self.assertEqual(mission.summary_reviewed_by, self.env.user)
        self.assertIn('Reviewed and approved by %s' % self.env.user.name, mission.summary_note)
        with self.assertRaisesRegex(UserError, 'Reopen it'):
            mission.write({'client_summary': 'Changed'})
        with self.assertRaisesRegex(UserError, 'unapproved summary'):
            mission.action_redraft_summary()
        mission.action_reopen_summary()
        self.assertEqual(mission.summary_state, 'to_review')
        self.assertFalse(mission.summary_reviewed_at)
        mission.write({'client_summary': edited.replace('continued', 'ongoing')})
        self.assertIn('ongoing', mission.client_summary)

    def test_report_shows_summary_only_after_approval(self):
        mission = self._fly(self._mission())
        self._complete(mission, self._gemini(self._summary_answer(self._summary_text())))
        html = self._report_html(mission)
        self.assertIn('awaiting review', html)
        self.assertNotIn('10 of the 12 flares', html)
        with self.assertRaisesRegex(UserError, 'Approve the client summary'):
            mission.action_print_report()

        mission.action_approve_summary()
        html = self._report_html(mission)
        self.assertNotIn('awaiting review', html)
        self.assertIn('10 of the 12 flares', html)
        self.assertIn('AI-drafted (%s)' % DEFAULT_MODEL, html)
        self.assertIn('Reviewed and approved by %s' % self.env.user.name, html)
        # (An admin whose company has no document layout is first sent to the
        # standard layout configurator; skip that here.)
        action = mission.with_context(discard_logo_check=True).action_print_report()
        self.assertEqual(action['report_name'], 'serein_ops.report_seeding_mission')

    def test_report_carries_the_mission_record(self):
        mission = self._fly(self._mission())
        mission.action_complete()
        mission.action_approve_summary()
        html = self._report_html(mission)
        for expected in (mission.name, 'Northwind Water Authority', mission.contract_id.name,
                         'Northern Highlands', 'King Air 350', 'A6-SRA', 'Coastal Airfield', 'Mara Okafor',
                         'Hygroscopic Flare 1 kg', '10000', '1.50', mission.alert_id.name,
                         'Weather data by Open-Meteo.com', 'Not recorded yet', 'Readiness Checks',
                         'Aircraft airworthy', 'Pilot medical', 'Written from the mission record'):
            self.assertIn(expected, html)
        self.assertIn(mission._utc_text(mission.takeoff_at), html)
        self.assertIn(mission._utc_text(mission.landing_at), html)

    def test_report_shows_recorded_rainfall(self):
        mission = self._fly(self._mission())
        mission.action_complete()
        mission.write({'observed_rainfall_mm': 6.4, 'rainfall_recorded_at': self.now})
        self.assertIn('6.4 mm over the target area', self._report_html(mission))

    def test_print_refused_before_completion(self):
        mission = self._mission()
        with self.assertRaisesRegex(UserError, 'completed missions'):
            mission.action_print_report()

    def test_operator_reviews_and_approves(self):
        operator = new_test_user(self.env, login='op_writer', groups='serein_ops.group_serein_operator')
        mission = self._mission().with_user(operator)
        mission.action_ready()
        mission.action_review_briefing()
        self.assertEqual(mission.briefing_reviewed_by, operator)
        self._fly(mission)
        mission.action_complete()
        mission.write({'client_summary': mission.client_summary + ' Edited by operations.'})
        mission.action_approve_summary()
        self.assertEqual(mission.summary_reviewed_by, operator)
        self.assertTrue(self.env['seeding.ai.log'].with_user(operator).search([('mission_id', '=', mission.id)]))
