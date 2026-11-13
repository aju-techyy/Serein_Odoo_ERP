import json
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.exceptions import UserError
from odoo.service.model import call_kw
from odoo.tests import TransactionCase, tagged

from odoo.addons.serein_ops.models.ai_service import API_KEY_ENV
from odoo.addons.serein_ops.models.mission_writer import unknown_figures
from odoo.addons.serein_ops.models.weather_service import ARCHIVE_URL, RAINFALL_SOURCE

REPORT = 'serein_ops.action_report_seeding_mission'


def archive_response(start, end, value=0.5, outside=9.0, missing_from=None):
    """An archive answer with hourly precipitation for whole UTC days from
    start to end: `value` mm inside [start, end), `outside` mm elsewhere,
    and no value from `missing_from` on."""
    times, values = [], []
    moment = datetime.combine(start.date(), datetime.min.time())
    last = datetime.combine(end.date(), datetime.min.time()) + timedelta(hours=23)
    while moment <= last:
        times.append(moment.strftime('%Y-%m-%dT%H:%M'))
        if missing_from and moment >= missing_from:
            values.append(None)
        else:
            values.append(value if start <= moment < end else outside)
        moment += timedelta(hours=1)
    return {'hourly_units': {'precipitation': 'mm'}, 'hourly': {'time': times, 'precipitation': values}}


@tagged('post_install', '-at_install')
class TestRainfallAndReports(TransactionCase):
    """Phase 8: observed rainfall on completed missions, and the reports."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.startClassPatcher(patch.dict(os.environ, {API_KEY_ENV: ''}))
        today = fields.Date.context_today(cls.env.user)
        cls.now = fields.Datetime.now().replace(second=0, microsecond=0)
        cls.client = cls.env['res.partner'].create({'name': 'Northwind Water Authority', 'is_company': True})
        cls.other_client = cls.env['res.partner'].create({'name': 'Lakeside Agritech', 'is_company': True})
        cls.area = cls.env['seeding.target.area'].create({
            'name': 'Northern Highlands', 'partner_id': cls.client.id,
            'latitude': 24.5, 'longitude': 55.9, 'radius_km': 40,
        })
        cls.other_area = cls.env['seeding.target.area'].create({
            'name': 'Lakeside Plain', 'partner_id': cls.other_client.id,
            'latitude': 1.35, 'longitude': 103.8, 'radius_km': 30,
        })
        cls.contract, cls.other_contract = cls.env['seeding.contract'].create([{
            'partner_id': partner.id, 'area_ids': [(6, 0, area.ids)],
            'season_start': today - timedelta(days=60), 'season_end': today + timedelta(days=60),
            'flight_hour_budget': 500, 'standby_fee': 1000, 'flight_hour_rate': 2500,
        } for partner, area in ((cls.client, cls.area), (cls.other_client, cls.other_area))])
        (cls.contract | cls.other_contract).action_activate()
        cls.base = cls.env['seeding.base'].create({'name': 'Coastal Airfield'})
        cls.flare = cls.env['product.product'].create({
            'name': 'Hygroscopic Flare 1 kg', 'type': 'consu', 'is_storable': True,
            'is_seeding_flare': True, 'grams_per_flare': 1000, 'standard_price': 85.0,
        })
        cls.king_air = cls.env['maintenance.equipment'].create({
            'name': 'King Air 350', 'is_aircraft': True, 'aircraft_type': 'crewed',
            'home_base_id': cls.base.id, 'rack_capacity': 24, 'service_interval_hours': 1000,
            'aircraft_hourly_cost': 400.0,
        })
        cls.pilot = cls.env['hr.employee'].create({
            'name': 'Mara Okafor', 'is_pilot': True, 'pilot_role': 'pilot', 'pilot_hourly_cost': 120.0,
        })
        cls.Service = cls.registry['seeding.weather.service']

    def _flown(self, landing, hours=2.5, area=None, fired=10, planned_by='human'):
        area = area or self.area
        return self.env['seeding.mission'].create({
            'area_id': area.id, 'aircraft_id': self.king_air.id, 'pilot_id': self.pilot.id,
            'flare_product_id': self.flare.id, 'flares_planned': 16, 'flares_fired': fired,
            'takeoff_at': landing - timedelta(hours=hours), 'landing_at': landing,
            'state': 'completed', 'planned_by': planned_by,
        })

    def _ten_days_ago(self):
        # Takeoff 09:00, landing 11:30 UTC: the period is 09:00 to 12:00 the next day.
        day = (self.now - timedelta(days=10)).replace(hour=11, minute=30)
        return self._flown(day, hours=2.5)

    def _patch_archive(self, **kwargs):
        return patch.object(self.Service, '_request_archive', autospec=True, **kwargs)

    # Observed rainfall -----------------------------------------------------

    def test_fetch_sums_the_takeoff_hour_to_24_hours_after_landing(self):
        mission = self._ten_days_ago()
        start = mission.takeoff_at.replace(minute=0)
        end = mission.landing_at.replace(minute=0) + timedelta(hours=25)
        with self._patch_archive(return_value=archive_response(start, end)) as request:
            mission.action_fetch_rainfall()
        request.assert_called_once()
        _self, lat, lon, start_date, end_date = request.call_args.args
        self.assertEqual((lat, lon), (24.5, 55.9))
        self.assertEqual((start_date, end_date), (start.date(), end.date()))
        # 27 hours of 0.5 mm; the 9 mm hours outside the period are left out.
        self.assertEqual(mission.observed_rainfall_mm, 13.5)
        self.assertEqual((mission.rainfall_period_start, mission.rainfall_period_end), (start, end))
        self.assertEqual(mission.rainfall_source, RAINFALL_SOURCE)
        self.assertTrue(mission.rainfall_recorded_at)
        self.assertIn('precipitation', json.loads(mission.rainfall_raw_response)['hourly'])
        self.assertIn('Observed rainfall recorded: 13.5 mm', mission.message_ids[0].body)
        self.assertIn('Open-Meteo.com', mission.message_ids[0].body)

    def test_landing_on_the_hour_starts_the_24_hours_there(self):
        mission = self._flown((self.now - timedelta(days=10)).replace(hour=12, minute=0), hours=2)
        start, end = mission._rainfall_period()
        self.assertEqual(start, mission.takeoff_at)
        self.assertEqual(end, mission.landing_at + timedelta(hours=24))

    def test_archive_request_is_pinned_to_era5(self):
        mission = self._ten_days_ago()
        start, end = mission._rainfall_period()
        answer = MagicMock()
        answer.json.return_value = archive_response(start, end, value=0.0)
        with patch('odoo.addons.serein_ops.models.weather_service.requests.get',
                   return_value=answer) as get:
            mission.action_fetch_rainfall()
        url = get.call_args.args[0]
        params = get.call_args.kwargs['params']
        self.assertEqual(url, ARCHIVE_URL)
        self.assertEqual(params['models'], 'era5')
        self.assertEqual(params['hourly'], 'precipitation')
        self.assertEqual(params['timezone'], 'GMT')
        self.assertEqual(params['start_date'], start.date().isoformat())
        self.assertEqual(params['end_date'], end.date().isoformat())
        self.assertEqual(mission.observed_rainfall_mm, 0.0)
        self.assertTrue(mission.rainfall_recorded_at)

    def test_refused_before_the_period_is_over(self):
        mission = self._flown(self.now - timedelta(hours=2))
        with self._patch_archive() as request, self.assertRaisesRegex(UserError, 'runs until'):
            mission.action_fetch_rainfall()
        request.assert_not_called()
        self.assertFalse(mission.rainfall_recorded_at)

    def test_refused_while_the_archive_does_not_cover_the_period(self):
        mission = self._ten_days_ago()
        start, end = mission._rainfall_period()
        data = archive_response(start, end, missing_from=end - timedelta(hours=3))
        with self._patch_archive(return_value=data), \
                self.assertRaisesRegex(UserError, 'does not cover'):
            mission.action_fetch_rainfall()
        self.assertFalse(mission.rainfall_recorded_at)

    def test_refused_for_a_mission_not_flown(self):
        mission = self.env['seeding.mission'].create({'area_id': self.area.id})
        with self._patch_archive() as request, self.assertRaisesRegex(UserError, 'completed missions'):
            mission.action_fetch_rainfall()
        request.assert_not_called()

    def test_network_failure_is_a_user_error(self):
        mission = self._ten_days_ago()
        with self._patch_archive(side_effect=requests.exceptions.ConnectionError('offline')), \
                self.assertRaisesRegex(UserError, 'Could not reach the rainfall archive'):
            mission.action_fetch_rainfall()
        self.assertFalse(mission.rainfall_recorded_at)

    def test_billed_mission_can_fetch_and_fetch_again(self):
        mission = self._ten_days_ago()
        mission.state = 'billed'
        start, end = mission._rainfall_period()
        with self._patch_archive(return_value=archive_response(start, end, value=0.2)):
            mission.action_fetch_rainfall()
        self.assertEqual(mission.observed_rainfall_mm, 5.4)
        with self._patch_archive(return_value=archive_response(start, end, value=0.0)):
            mission.action_fetch_rainfall()
        self.assertEqual(mission.observed_rainfall_mm, 0.0)

    def test_approved_summary_suggests_reopen_and_redraft(self):
        mission = self._ten_days_ago()
        self.env['seeding.mission.writer'].write_summary_from_record(mission)
        mission.action_approve_summary()
        start, end = mission._rainfall_period()
        with self._patch_archive(return_value=archive_response(start, end)):
            mission.action_fetch_rainfall()
        self.assertEqual(mission.summary_state, 'approved')
        self.assertIn('Reopen Summary, Redraft Summary, then Approve Summary', mission.message_ids[0].body)

    def test_button_through_call_kw(self):
        mission = self._ten_days_ago()
        start, end = mission._rainfall_period()
        with self._patch_archive(return_value=archive_response(start, end)):
            result = call_kw(self.env['seeding.mission'], 'action_fetch_rainfall', [mission.ids], {})
        self.assertEqual(result['tag'], 'display_notification')
        self.assertIn('13.5 mm', result['params']['message'])

    def test_summary_input_and_template_carry_the_rainfall(self):
        mission = self._ten_days_ago()
        writer = self.env['seeding.mission.writer']
        self.assertIsNone(writer._summary_input(mission)['observed_rainfall_period'])
        start, end = mission._rainfall_period()
        with self._patch_archive(return_value=archive_response(start, end)):
            mission.action_fetch_rainfall()
        payload = writer._summary_input(mission)
        self.assertEqual(payload['observed_rainfall_mm'], 13.5)
        self.assertEqual(payload['observed_rainfall_period']['hours_after_landing'], 24)
        self.assertEqual(payload['observed_rainfall_period']['source'], RAINFALL_SOURCE)
        template = writer._summary_template(mission)
        self.assertIn('was 13.5 mm', template)
        self.assertEqual(unknown_figures(template, payload), [])

    def test_summary_from_record_makes_no_ai_call(self):
        mission = self._ten_days_ago()
        with patch.object(self.registry['seeding.ai.service'], '_request', autospec=True) as request:
            self.env['seeding.mission.writer'].write_summary_from_record(mission)
        request.assert_not_called()
        self.assertEqual((mission.summary_source, mission.summary_state), ('template', 'to_review'))
        self.assertFalse(mission.summary_fallback_reason)
        self.assertFalse(mission.ai_log_ids)

    def test_report_shows_the_rainfall_period_and_source(self):
        mission = self._ten_days_ago()
        start, end = mission._rainfall_period()
        with self._patch_archive(return_value=archive_response(start, end)):
            mission.action_fetch_rainfall()
        html, _type = self.env['ir.actions.report']._render_qweb_html(REPORT, mission.ids)
        html = html.decode()
        self.assertIn('13.5', html)
        self.assertIn(mission._utc_text(start), html)
        self.assertIn(mission._utc_text(end), html)
        self.assertIn(RAINFALL_SOURCE, html)
        self.assertIn('Rainfall data by Open-Meteo.com', html)

    # Reports ---------------------------------------------------------------

    REPORT_ACTIONS = {
        'serein_ops.seeding_report_hours_action': ('partner_id', 'flight_hours'),
        'serein_ops.seeding_report_material_action': ('area_id', 'material_released_g'),
        'serein_ops.seeding_report_cost_action': ('name', 'mission_cost'),
        'serein_ops.seeding_report_planning_action': ('planned_by', None),
    }

    def test_report_actions_open_their_graph_and_pivot(self):
        Mission = self.env['seeding.mission']
        for xmlid, (groupby, measure) in self.REPORT_ACTIONS.items():
            action = self.env.ref(xmlid)
            views = {v.view_mode: v.view_id for v in action.view_ids}
            self.assertEqual(set(views), {'graph', 'pivot'}, xmlid)
            result = Mission.get_views([(views['graph'].id, 'graph'), (views['pivot'].id, 'pivot')])
            graph = result['views']['graph']['arch']
            self.assertIn('name="%s"' % groupby, graph, xmlid)
            if measure:
                self.assertIn('name="%s" type="measure"' % measure, graph, xmlid)
        # The Missions menu has graph and pivot views too.
        self.assertIn('graph', self.env.ref('serein_ops.seeding_mission_action').view_mode)
        self.assertIn('pivot', self.env.ref('serein_ops.seeding_mission_action').view_mode)

    def test_report_figures(self):
        landing = self.now - timedelta(days=3)
        self._flown(landing, hours=1.5, fired=10, planned_by='ai')
        self._flown(landing, hours=2.0, fired=12, planned_by='rules')
        self._flown(landing, hours=3.0, area=self.other_area, fired=20, planned_by='human')
        self.env['seeding.mission'].create({'area_id': self.area.id, 'planned_by': 'human'})
        Mission = self.env['seeding.mission']
        flown = [('state', 'in', ('completed', 'billed'))]
        hours = dict(Mission._read_group(flown, ['partner_id'], ['flight_hours:sum']))
        self.assertEqual(hours, {self.client: 3.5, self.other_client: 3.0})
        material = dict(Mission._read_group(flown, ['area_id'], ['material_released_g:sum']))
        self.assertEqual(material, {self.area: 22000.0, self.other_area: 20000.0})
        cost = dict(Mission._read_group(flown, ['area_id'], ['mission_cost:sum']))
        # 22 flares x 85 + 3.5 h x (400 + 120)
        self.assertEqual(cost[self.area], 22 * 85.0 + 3.5 * 520.0)
        planners = dict(Mission._read_group([('state', '!=', 'cancelled')], ['planned_by'], ['__count']))
        self.assertEqual(planners, {'ai': 1, 'rules': 1, 'human': 2})

    def test_cost_report_menu_is_for_managers(self):
        menu = self.env.ref('serein_ops.menu_serein_report_cost')
        self.assertEqual(menu.group_ids, self.env.ref('serein_ops.group_serein_manager'))
        for xmlid in ('menu_serein_report_hours', 'menu_serein_report_material', 'menu_serein_report_planning'):
            self.assertFalse(self.env.ref('serein_ops.%s' % xmlid).group_ids)
