import os
from collections import defaultdict
from datetime import datetime, timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import UserError
from odoo.service.model import call_kw
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.serein_ops.models import demo_world
from odoo.addons.serein_ops.models.ai_service import API_KEY_ENV
from odoo.addons.serein_ops.models.demo_world_schedule import CHECKS_BEFORE_TAKEOFF
from odoo.addons.serein_ops.models.weather_service import HOURLY_FIELDS


def _hours(start_date, end_date):
    moment = datetime.combine(start_date, datetime.min.time())
    while moment.date() <= end_date:
        yield moment
        moment += timedelta(hours=1)


def any_archive(service, latitude, longitude, start_date, end_date):
    """A rainfall archive answer with 0.2 mm in every hour of the requested days."""
    times = [m.strftime('%Y-%m-%dT%H:%M') for m in _hours(start_date, end_date)]
    return {'hourly': {'time': times, 'precipitation': [0.2] * len(times)}}


def some_history(service, latitude, longitude, start_date, end_date):
    """A forecast archive answer: seedable (score 92) from 09:00 to 11:59 local
    solar time on every third day, poor (score 13) otherwise."""
    offset = round(longitude / 15.0)
    hourly = {name: [] for name in HOURLY_FIELDS}
    hourly['time'] = []
    for moment in _hours(start_date, end_date):
        local = moment + timedelta(hours=offset)
        seedable = local.toordinal() % 3 == 0 and 9 <= local.hour <= 11
        hourly['time'].append(moment.strftime('%Y-%m-%dT%H:%M'))
        for name in HOURLY_FIELDS:
            hourly[name].append(0.0)
        hourly['cloud_cover_low'][-1] = 90.0 if seedable else 0.0
        hourly['cape'][-1] = 900.0 if seedable else 0.0
        hourly['wind_speed_10m'][-1] = 10.0 if seedable else 35.0
    return {'latitude': latitude, 'longitude': longitude, 'hourly': hourly}


def live_forecast(service, latitude, longitude):
    """A live forecast answer: seedable from the current hour for 4 hours."""
    hour = fields.Datetime.now().replace(minute=0, second=0, microsecond=0)
    hourly = {name: [] for name in HOURLY_FIELDS}
    hourly['time'] = []
    for offset in range(-3, 31):
        moment = hour + timedelta(hours=offset)
        seedable = 0 <= offset < 4
        hourly['time'].append(moment.strftime('%Y-%m-%dT%H:%M'))
        for name in HOURLY_FIELDS:
            hourly[name].append(0.0)
        hourly['cloud_cover_low'][-1] = 90.0 if seedable else 0.0
        hourly['cape'][-1] = 900.0 if seedable else 0.0
        hourly['wind_speed_10m'][-1] = 10.0 if seedable else 35.0
    return {'latitude': latitude, 'longitude': longitude, 'hourly': hourly}


class DemoWorldCase(TransactionCase):
    archive_side_effect = staticmethod(any_archive)
    history_side_effect = staticmethod(some_history)
    forecast_side_effect = staticmethod(live_forecast)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.startClassPatcher(patch.dict(os.environ, {API_KEY_ENV: ''}))
        cls.ai_request = cls.startClassPatcher(patch.object(
            cls.registry['seeding.ai.service'], '_request', autospec=True))
        cls.forecast_request = cls.startClassPatcher(patch.object(
            cls.registry['seeding.weather.service'], '_request', autospec=True,
            side_effect=cls.forecast_side_effect))
        cls.archive_request = cls.startClassPatcher(patch.object(
            cls.registry['seeding.weather.service'], '_request_archive', autospec=True,
            side_effect=cls.archive_side_effect))
        cls.history_request = cls.startClassPatcher(patch.object(
            cls.registry['seeding.weather.service'], '_request_history', autospec=True,
            side_effect=cls.history_side_effect))
        cls.today = fields.Date.context_today(cls.env.user)
        cls.now = fields.Datetime.now()
        cls.counts = cls.env['seeding.demo.world'].load()
        cls.missions = cls.env['seeding.mission'].search([])
        cls.flown = cls.missions.filtered(lambda m: m.state in ('completed', 'billed'))


@tagged('post_install', '-at_install')
class TestDemoWorld(DemoWorldCase):
    """Load Demo World builds a regional operator whose history follows the
    archived weather of its target areas."""

    def test_counts(self):
        self.assertEqual(self.counts['history'], 'archive')
        self.assertEqual(self.counts['clients'], 6)
        self.assertEqual(self.counts['areas'], 16)
        self.assertEqual(self.counts['aircraft'], 10)
        self.assertEqual(self.counts['pilots'], 14)
        self.assertEqual(self.counts['missions'], len(self.flown))
        self.assertGreater(self.counts['missions'], 100)
        archive = [('data_source', '=', 'archive')]
        self.assertEqual(self.env['seeding.weather.snapshot'].search_count(archive),
                         self.env['seeding.cloud.alert'].search_count([('source', '=', 'archive')]))
        self.assertEqual(self.history_request.call_count, 16)

    def test_clients_in_six_countries_two_seasons_over(self):
        contracts = self.env['seeding.contract'].search([])
        self.assertEqual(len(contracts), 6)
        self.assertEqual(set(contracts.partner_id.country_id.mapped('code')),
                         {'AE', 'SA', 'MY', 'NO', 'AU', 'IN'})
        closed = contracts.filtered(lambda c: c.state == 'closed')
        self.assertEqual(set(closed.partner_id.country_id.mapped('code')), {'AU', 'IN'})
        self.assertTrue(all(c.season_end < self.today for c in closed))
        for contract in contracts - closed:
            self.assertEqual(contract.state, 'active')
            self.assertLess(contract.season_start, self.today)
            self.assertGreater(contract.season_end, self.today)
        for contract in contracts:
            self.assertLess(contract.hours_used, contract.flight_hour_budget)
            self.assertTrue(contract.mission_ids)

    def test_alerts_come_from_the_forecast_archive(self):
        alerts = self.env['seeding.cloud.alert'].search([('source', '=', 'archive')])
        self.assertTrue(alerts)
        self.assertEqual(set(alerts.mapped('state')), {'planned', 'dismissed', 'expired'})
        self.assertEqual(set(alerts.snapshot_id.mapped('data_source')), {'archive'})
        threshold = alerts.snapshot_id._threshold()
        per_day = defaultdict(int)
        for alert in alerts:
            self.assertGreaterEqual(alert.score, threshold)
            self.assertIn('"hourly"', alert.snapshot_id.raw_response)
            self.assertLessEqual(alert.window_end, self.now)
            contract = alert.area_id.contract_ids
            self.assertTrue(contract.season_start <= alert.window_start.date() <= contract.season_end)
            per_day[alert.area_id, alert.window_start.date()] += 1
        self.assertEqual(max(per_day.values()), 1)
        dismissed = alerts.filtered(lambda a: a.state == 'dismissed')
        self.assertTrue(dismissed)
        self.assertTrue(all(a.note.startswith('Not flown:') for a in dismissed))

    def test_missions_flew_against_their_alerts_through_the_whole_flow(self):
        self.assertEqual(set(self.missions.mapped('state')),
                         {'completed', 'billed', 'cancelled', 'proposed', 'ready', 'launched'})
        self.assertEqual(set(self.missions.mapped('planned_by')), {'rules', 'human'})
        glaciogenic = self.env['product.product'].search([('default_code', '=', 'SI-150')])
        for mission in self.flown:
            alert = mission.alert_id
            self.assertEqual(alert.state, 'planned')
            self.assertTrue(alert.window_start <= mission.takeoff_at < alert.window_end, mission.name)
            self.assertEqual(mission.readiness_state, 'pass')
            self.assertEqual(len(mission.readiness_ids), 11)
            self.assertEqual(mission.readiness_checked_at, mission.takeoff_at - CHECKS_BEFORE_TAKEOFF)
            self.assertEqual(mission.picking_id.state, 'done')
            self.assertEqual(mission.picking_id.date_done, mission.landing_at)
            self.assertTrue(mission.flare_batches)
            self.assertEqual((mission.summary_source, mission.summary_state), ('template', 'to_review'))
            cold = mission.base_id.name in ('Fjordline Airstrip', 'Snowy Plains Airfield')
            self.assertEqual(mission.flare_product_id == glaciogenic, cold, mission.name)
        rules = self.flown.filtered(lambda m: m.planned_by == 'rules')
        self.assertTrue(all(m.plan_reasoning.startswith('Rule-based plan') for m in rules))
        for mission in self.missions.filtered(lambda m: m.state == 'cancelled'):
            self.assertIn(mission.alert_id.state, ('expired', 'new'))

    def test_present_day_operations(self):
        live = self.env['seeding.cloud.alert'].search([('source', '=', 'live')])
        self.assertEqual(self.forecast_request.call_count, 16)
        self.assertTrue(live)
        self.assertEqual(self.counts['live_alerts'], len(live))
        current = self.missions.filtered(lambda m: m.state in ('proposed', 'ready', 'launched'))
        self.assertEqual(self.counts['current'], len(current))
        self.assertEqual(sorted(current.mapped('state')), ['launched', 'proposed', 'proposed', 'ready'])
        launched = current.filtered(lambda m: m.state == 'launched')
        self.assertLess(launched.takeoff_at, self.now)
        self.assertGreater(launched.takeoff_at, self.now - timedelta(hours=1))
        for mission in current.filtered(lambda m: m.state in ('ready', 'launched')):
            self.assertEqual(mission.briefing_source, 'template')
            self.assertTrue(mission.briefing)
            self.assertFalse(mission.briefing_reviewed_by)
        proposed = current.filtered(lambda m: m.state == 'proposed')
        passing = proposed.filtered(lambda m: m.readiness_state == 'pass')
        blocked = proposed - passing
        self.assertEqual(passing.planned_by, 'rules')
        self.assertEqual(blocked.pilot_id.name, 'Ingrid Solberg')
        failed = blocked.readiness_ids.filtered(lambda c: c.result == 'fail')
        self.assertEqual(failed.mapped('code'), ['pilot_medical'])
        # Live alerts come first: missions with an alert planned it.
        self.assertTrue(current.alert_id)
        self.assertTrue(all(a.state == 'planned' for a in current.alert_id))
        # The aircraft kept near its interval stays free for planning.
        self.assertNotIn('Piper Cheyenne II', current.aircraft_id.mapped('name'))
        self.assertEqual(len(current.area_id), 4)
        self.assertEqual(self.env['seeding.ai.log'].search_count([]), 0)

    def test_crew_and_aircraft_rules_held_at_every_takeoff(self):
        by_aircraft = defaultdict(list)
        by_pilot = defaultdict(list)
        for mission in self.flown.sorted('takeoff_at'):
            check = mission.takeoff_at - CHECKS_BEFORE_TAKEOFF
            pilot = mission.pilot_id
            self.assertEqual(pilot.home_base_id, mission.aircraft_id.home_base_id, mission.name)
            self.assertIn(mission.area_id, mission.aircraft_id.home_base_id.area_ids, mission.name)
            self.assertEqual(pilot.pilot_role == 'drone_operator', mission.aircraft_type == 'drone')
            self.assertGreaterEqual(pilot.licence_expiry, mission.takeoff_at.date())
            if pilot.pilot_role == 'pilot':
                self.assertGreaterEqual(pilot.medical_expiry, mission.takeoff_at.date())
            self.assertLess(pilot._duty_hours_until(check), pilot.weekly_duty_limit)
            by_aircraft[mission.aircraft_id].append(mission)
            by_pilot[pilot].append(mission)
        for flights in list(by_aircraft.values()) + list(by_pilot.values()):
            for before, after in zip(flights, flights[1:]):
                self.assertLessEqual(before.landing_at, after.takeoff_at)

    def test_fleet_services_breakdowns_and_the_king_air_near_its_interval(self):
        aircraft = self.env['maintenance.equipment'].search([('is_aircraft', '=', True)])
        self.assertEqual(len(aircraft.filtered(lambda a: a.aircraft_type == 'drone')), 3)
        for plane in aircraft:
            self.assertTrue(plane.last_service_at, plane.name)
            self.assertTrue(plane.seeding_mission_ids, plane.name)
        reserved = aircraft.filtered(lambda a: a.name == 'Piper Cheyenne II')
        self.assertTrue(reserved.is_airworthy)
        self.assertLess(reserved.hours_since_service, reserved.service_interval_hours)
        self.assertGreaterEqual(reserved.hours_since_service, 40)
        for base in self.env['seeding.base'].search([]):
            self.assertTrue(base.area_ids, base.name)
        grounded = aircraft.filtered(lambda a: not a.is_airworthy)
        self.assertIn('Cessna 421', grounded.mapped('name'))
        corrective = aircraft.maintenance_ids.filtered(lambda r: r.maintenance_type == 'corrective')
        self.assertEqual(len(corrective), len(demo_world.BREAKDOWNS))
        self.assertEqual(len(corrective.filtered(lambda r: not r.stage_id.done)), 1)

    def test_crew(self):
        crew = self.env['hr.employee'].search([('is_pilot', '=', True)])
        self.assertEqual(len(crew), 14)
        self.assertTrue(all(crew.mapped('home_base_id')))
        self.assertEqual(len(crew.home_base_id), 5)
        self.assertEqual(len(crew.filtered(lambda p: p.pilot_role == 'drone_operator')), 3)
        by_name = {p.name: p for p in crew}
        self.assertEqual(by_name['Ingrid Solberg'].flying_status, 'grounded')
        self.assertEqual(by_name['Erik Dahl'].flying_status, 'expiring')
        self.assertEqual(by_name['Omar Saleh'].flying_status, 'expiring')
        self.assertTrue(by_name['Ingrid Solberg'].seeding_mission_ids, "she flew before her medical expired")

    def test_flares_bought_by_batch_and_stock_never_negative(self):
        flares = self.env['product.product'].search([('is_seeding_flare', '=', True)])
        self.assertEqual(flares.mapped('default_code'), ['HF-1000', 'SI-150'])
        self.assertEqual(self.env['seeding.mission']._default_flare_product().default_code, 'HF-1000')
        self.assertEqual(self.env['stock.warehouse.orderpoint'].search_count([('trigger', '=', 'auto')]),
                         len(demo_world.REORDER))
        orders = self.env['purchase.order'].search([])
        self.assertGreater(len(orders), len(demo_world.REORDER) // 2)
        self.assertEqual(set(orders.mapped('state')), {'purchase'})
        received = orders.filtered(lambda o: o.receipt_status == 'full')
        self.assertTrue(received)
        self.assertTrue(all(o.invoice_ids.filtered(lambda b: b.state == 'posted') for o in received))
        self.assertTrue(self.env['account.move'].search([
            ('move_type', '=', 'in_invoice'), ('payment_state', 'in', ('paid', 'in_payment'))]))
        quants = self.env['stock.quant'].search([
            ('product_id', 'in', flares.ids), ('location_id.usage', '=', 'internal')])
        self.assertTrue(all(q.quantity >= 0 for q in quants))
        self.assertTrue(all(q.lot_id for q in quants))

    def test_invoices_by_season_and_payments(self):
        month_start = datetime.combine(self.today.replace(day=1), datetime.min.time())
        before = self.flown.filtered(lambda m: m.landing_at < month_start)
        self.assertEqual(set(before.mapped('state')), {'billed'})
        self.assertEqual(set((self.flown - before).mapped('state')), {'completed'})
        for contract in self.env['seeding.contract'].search([]):
            invoices = contract.invoice_ids.sorted('seeding_period_start')
            self.assertTrue(invoices)
            if contract.state == 'closed':
                self.assertEqual(set(invoices.mapped('state')), {'posted'})
            else:
                self.assertEqual(invoices[-1].state, 'draft')
                self.assertEqual(set(invoices[:-1].mapped('state')), {'posted'})
        paid = self.env['account.move'].search([
            ('seeding_contract_id', '!=', False), ('payment_state', 'in', ('paid', 'in_payment'))])
        self.assertTrue(paid)
        for invoice in paid:
            self.assertLessEqual(invoice.invoice_date + timedelta(days=demo_world.CLIENT_PAYMENT_DAYS[0]),
                                 self.today)

    def test_rainfall_for_missions_older_than_two_weeks(self):
        cutoff = self.now.replace(minute=0, second=0, microsecond=0) - timedelta(
            days=demo_world.RAINFALL_RECORDED_AFTER_DAYS)
        old = self.flown.filtered(lambda m: m.landing_at <= cutoff)
        young = self.flown - old
        self.assertTrue(old and young)
        self.assertEqual(self.counts['rainfall'], len(old))
        self.assertTrue(all(old.mapped('rainfall_recorded_at')))
        self.assertFalse(any(young.mapped('rainfall_recorded_at')))
        self.assertEqual(self.archive_request.call_count, len(old.area_id))

    def test_no_ai_call(self):
        self.ai_request.assert_not_called()
        self.assertEqual(self.env['seeding.ai.log'].search_count([]), 0)

    def test_a_new_mission_can_be_planned_on_the_world(self):
        active_areas = self.env['seeding.contract'].search([('state', '=', 'active')]).area_ids
        snapshot = self.env['seeding.weather.snapshot'].search(
            [('area_id', 'in', active_areas.ids)], order='score desc', limit=1)
        alert = self.env['seeding.cloud.alert'].browse(snapshot.action_replay()['res_id'])
        mission = self.env['seeding.mission.planner'].plan_alert(alert)
        self.assertEqual(mission.planned_by, 'rules')
        self.assertEqual(mission.readiness_state, 'pass', mission.readiness_ids.mapped('explanation'))

    def test_company_and_report_layout(self):
        self.assertEqual(self.env.company.name, demo_world.COMPANY_NAME)
        self.assertTrue(self.env.company.external_report_layout_id)

    def test_second_load_is_refused(self):
        with self.assertRaisesRegex(UserError, 'already holds Serein records'):
            self.env['seeding.demo.world'].load()
        wizard = self.env['seeding.demo.world'].create({})
        self.assertIn('16 target areas', wizard.blocking_reason)
        with self.assertRaises(UserError):
            call_kw(self.env['seeding.demo.world'], 'action_load', [wizard.ids], {})

    def test_only_managers_load(self):
        operator = new_test_user(self.env, login='demo_operator', groups='base.group_user,serein_ops.group_serein_operator')
        with self.assertRaises(Exception):
            self.env['seeding.demo.world'].with_user(operator).create({})
        with self.assertRaisesRegex(UserError, 'Serein manager'):
            self.env['seeding.demo.world'].with_user(operator).load()


@tagged('post_install', '-at_install')
class TestDemoWorldOffline(DemoWorldCase):
    """Without the archives the world still loads: the history falls back to
    the fixed missions and the recorded snapshots, without rainfall."""

    @staticmethod
    def archive_side_effect(*args):
        raise requests.exceptions.ConnectionError('offline')

    @staticmethod
    def history_side_effect(*args):
        raise requests.exceptions.ConnectionError('offline')

    @staticmethod
    def forecast_side_effect(*args):
        raise requests.exceptions.ConnectionError('offline')

    def test_fallback_history(self):
        self.assertEqual(self.counts['history'], 'fallback')
        self.assertEqual(self.counts['missions'], len(demo_world.FALLBACK_MISSIONS))
        self.assertEqual(self.counts['alerts'], 0)
        self.assertEqual(self.counts['live_alerts'], 0)
        self.assertEqual(self.counts['snapshots'], len(demo_world.FALLBACK_AREAS))
        self.assertEqual(self.counts['rainfall'], 0)
        self.assertEqual(set(self.flown.mapped('readiness_state')), {'pass'})
        self.assertFalse(self.flown.alert_id)
        for mission in self.flown:
            self.assertIn(mission.area_id, mission.aircraft_id.home_base_id.area_ids, mission.name)
        # Today's missions are planned without alerts.
        current = self.missions.filtered(lambda m: m.state in ('proposed', 'ready', 'launched'))
        self.assertEqual(sorted(current.mapped('state')), ['launched', 'proposed', 'proposed', 'ready'])
        self.assertFalse(current.alert_id)
        self.assertTrue(all(self.flown.mapped('client_summary')))
        for mission in self.flown:
            self.assertEqual(mission.pilot_id.home_base_id, mission.aircraft_id.home_base_id, mission.name)
