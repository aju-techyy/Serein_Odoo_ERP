import os
from datetime import datetime, timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import UserError
from odoo.service.model import call_kw
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.serein_ops.models import demo_world
from odoo.addons.serein_ops.models.ai_service import API_KEY_ENV


def any_archive(service, latitude, longitude, start_date, end_date):
    """An archive answer with 0.2 mm in every hour of the requested days."""
    times = []
    moment = datetime.combine(start_date, datetime.min.time())
    while moment.date() <= end_date:
        times.append(moment.strftime('%Y-%m-%dT%H:%M'))
        moment += timedelta(hours=1)
    return {'hourly': {'time': times, 'precipitation': [0.2] * len(times)}}


class DemoWorldCase(TransactionCase):
    archive_side_effect = staticmethod(any_archive)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.startClassPatcher(patch.dict(os.environ, {API_KEY_ENV: ''}))
        cls.ai_request = cls.startClassPatcher(patch.object(
            cls.registry['seeding.ai.service'], '_request', autospec=True))
        cls.forecast_request = cls.startClassPatcher(patch.object(
            cls.registry['seeding.weather.service'], '_request', autospec=True))
        cls.archive_request = cls.startClassPatcher(patch.object(
            cls.registry['seeding.weather.service'], '_request_archive', autospec=True,
            side_effect=cls.archive_side_effect))
        cls.today = fields.Date.context_today(cls.env.user)
        cls.now = fields.Datetime.now()
        cls.counts = cls.env['seeding.demo.world'].load()
        cls.missions = cls.env['seeding.mission'].search([])


@tagged('post_install', '-at_install')
class TestDemoWorld(DemoWorldCase):
    """Phase 8: Load Demo World builds the full world in an empty database."""

    def test_counts(self):
        self.assertEqual(self.counts['clients'], 3)
        self.assertEqual(self.counts['areas'], 6)
        self.assertEqual(self.counts['aircraft'], 4)
        self.assertEqual(self.counts['pilots'], 5)
        self.assertEqual(self.counts['missions'], 25)
        self.assertEqual(self.counts['snapshots'], 6)

    def test_three_clients_in_three_regions_with_active_contracts(self):
        contracts = self.env['seeding.contract'].search([])
        self.assertEqual(len(contracts), 3)
        self.assertEqual(set(contracts.mapped('state')), {'active'})
        self.assertEqual(len(contracts.partner_id.country_id), 3)
        self.assertEqual(set(contracts.partner_id.country_id.mapped('code')), {'AE', 'MY', 'NO'})
        for contract in contracts:
            self.assertEqual(len(contract.area_ids), 2)
            self.assertLess(contract.season_start, self.today)
            self.assertGreater(contract.season_end, self.today)
            self.assertLess(contract.hours_used, contract.flight_hour_budget)

    def test_bases_aircraft_and_the_one_near_its_interval(self):
        self.assertEqual(self.env['seeding.base'].search_count([]), 2)
        aircraft = self.env['maintenance.equipment'].search([('is_aircraft', '=', True)])
        self.assertEqual(sorted(aircraft.mapped('aircraft_type')), ['crewed', 'crewed', 'crewed', 'drone'])
        self.assertTrue(all(aircraft.mapped('is_airworthy')))
        self.assertTrue(all(aircraft.mapped('last_service_at')))
        near = aircraft.filtered(
            lambda a: a.service_interval_hours - a.hours_since_service < 2.0)
        self.assertEqual(near.name, 'King Air 350')
        self.assertAlmostEqual(near.hours_since_service, 48.33, places=2)
        self.assertEqual(near.service_interval_hours, 50)
        # Nothing is due yet: no scheduled service is open.
        self.assertFalse(aircraft.maintenance_ids.filtered(
            lambda r: r.is_scheduled_service and not r.stage_id.done))

    def test_five_pilots_one_with_an_expired_medical(self):
        pilots = self.env['hr.employee'].search([('is_pilot', '=', True)])
        self.assertEqual(len(pilots), 5)
        self.assertEqual(len(pilots.filtered(lambda p: p.pilot_role == 'drone_operator')), 1)
        expired = pilots.filtered(lambda p: p.medical_expiry and p.medical_expiry < self.today)
        self.assertEqual(expired.name, 'Ingrid Solberg')
        self.assertTrue(all(p.licence_expiry > self.today for p in pilots))
        self.assertTrue(all(p.duty_hours_7d < p.weekly_duty_limit for p in pilots))

    def test_flare_batches_vendor_and_stock(self):
        flare = self.env['product.product'].search([('is_seeding_flare', '=', True)])
        self.assertEqual(len(flare), 1)
        self.assertEqual(flare.tracking, 'lot')
        self.assertEqual(flare.seller_ids.partner_id.name, 'Altostratus Pyrotechnics')
        lots = self.env['stock.lot'].search([('product_id', '=', flare.id)])
        self.assertEqual(sorted(lots.mapped('name')), sorted(demo_world.BATCHES))
        for base in self.env['seeding.base'].search([]):
            received = sum(qty for b, qty, _d in demo_world.BATCHES.values()
                           if demo_world.BASES[b] == base.name)
            fired = sum(self.missions.filtered(lambda m: m.base_id == base).mapped('flares_fired'))
            on_hand = flare.with_context(location=base.location_id.id).qty_available
            self.assertEqual(on_hand, received - fired, base.name)
        self.assertEqual(self.env['stock.warehouse.orderpoint'].search_count(
            [('product_id', '=', flare.id), ('trigger', '=', 'auto')]), 2)

    def test_reorder_rule_proposed_a_purchase(self):
        line = self.env['purchase.order.line'].search([('orderpoint_id', '!=', False)])
        self.assertTrue(line)
        self.assertEqual(line.order_id.partner_id.name, 'Altostratus Pyrotechnics')
        self.assertEqual(line.order_id.state, 'draft')

    def test_past_missions_went_through_the_whole_flow(self):
        self.assertEqual(len(self.missions), 25)
        self.assertEqual(set(self.missions.mapped('state')), {'completed', 'billed'})
        self.assertEqual(set(self.missions.mapped('planned_by')), {'rules', 'human'})
        self.assertEqual(set(self.missions.mapped('readiness_state')), {'pass'})
        for mission in self.missions:
            self.assertEqual(len(mission.readiness_ids), 11)
            self.assertLess(mission.readiness_checked_at, mission.takeoff_at)
            self.assertLess(mission.landing_at, self.now)
            self.assertEqual(mission.picking_id.state, 'done')
            self.assertEqual(mission.picking_id.date_done, mission.landing_at)
            self.assertTrue(mission.flare_batches)
            self.assertEqual(mission.material_released_g, mission.flares_fired * 1000)
            self.assertGreater(mission.mission_cost, 0)
            self.assertEqual((mission.summary_source, mission.summary_state), ('template', 'to_review'))
        rules = self.missions.filtered(lambda m: m.planned_by == 'rules')
        self.assertTrue(all(m.plan_reasoning.startswith('Rule-based plan') for m in rules))

    def test_no_ai_call_and_no_ai_log(self):
        self.ai_request.assert_not_called()
        self.forecast_request.assert_not_called()
        self.assertEqual(self.env['seeding.ai.log'].search_count([]), 0)

    def test_saved_real_snapshots(self):
        snapshots = self.env['seeding.weather.snapshot'].search([])
        self.assertEqual(len(snapshots), 6)
        self.assertEqual(len(snapshots.area_id), 6)
        threshold = snapshots._threshold()
        self.assertTrue(snapshots.filtered(lambda s: s.score >= threshold))
        self.assertTrue(snapshots.filtered(lambda s: s.score < threshold))
        for snap in snapshots:
            self.assertIn('"hourly"', snap.raw_response)
            self.assertEqual(snap.valid_for, snap.fetched_at.replace(minute=0, second=0))
        # A saved snapshot can be replayed into an alert.
        best = snapshots.sorted('score', reverse=True)[0]
        action = best.action_replay()
        alert = self.env['seeding.cloud.alert'].browse(action['res_id'])
        self.assertEqual(alert.source, 'replayed')
        self.assertEqual(self.env['seeding.cloud.alert'].search_count([]), 1)

    def test_rainfall_for_missions_older_than_two_weeks(self):
        cutoff = self.now.replace(minute=0, second=0, microsecond=0) - timedelta(
            days=demo_world.RAINFALL_RECORDED_AFTER_DAYS)
        old = self.missions.filtered(lambda m: m.landing_at <= cutoff)
        young = self.missions - old
        self.assertTrue(old and young)
        self.assertEqual(self.counts['rainfall'], len(old))
        self.assertTrue(all(old.mapped('rainfall_recorded_at')))
        self.assertFalse(any(young.mapped('rainfall_recorded_at')))
        # One archive call per area that has old missions.
        self.assertEqual(self.archive_request.call_count, len(old.area_id))
        for mission in old:
            start, end = mission._rainfall_period()
            self.assertAlmostEqual(mission.observed_rainfall_mm, round(0.2 * (end - start).total_seconds() / 3600, 1))
            self.assertIn('Observed rainfall over the area', mission.client_summary)

    def test_past_months_invoiced_current_month_left_to_invoice(self):
        month_start = datetime.combine(self.today.replace(day=1), datetime.min.time())
        before = self.missions.filtered(lambda m: m.landing_at < month_start)
        this_month = self.missions - before
        self.assertEqual(set(before.mapped('state')), {'billed'} if before else set())
        self.assertTrue(all(before.mapped('invoice_id')))
        self.assertEqual(set(this_month.mapped('state')), {'completed'} if this_month else set())
        invoices = self.env['account.move'].search([('seeding_contract_id', '!=', False)])
        self.assertEqual(len(invoices), self.counts['invoices'])
        self.assertIn('posted', invoices.mapped('state'))
        self.assertIn('draft', invoices.mapped('state'))
        for invoice in invoices.filtered(lambda i: i.state == 'posted'):
            # Dated the day after its period, and due on that date (no payment term).
            self.assertEqual(invoice.invoice_date, invoice.seeding_period_end + timedelta(days=1))
            self.assertEqual(invoice.invoice_date_due, invoice.invoice_date)
        sale_tax = self.env.company.account_sale_tax_id
        if sale_tax:
            # The service products carry the company's sale tax, mapped by the
            # client's fiscal position (clients abroad: the export tax).
            for invoice in invoices:
                expected = invoice.fiscal_position_id.map_tax(sale_tax) if invoice.fiscal_position_id else sale_tax
                self.assertEqual(invoice.invoice_line_ids.tax_ids, expected, invoice.partner_id.name)
        for contract in self.env['seeding.contract'].search([]):
            latest = contract.invoice_ids.sorted('seeding_period_start')[-1]
            self.assertEqual(latest.state, 'draft')
            # Running the past months again creates nothing.
            self.assertFalse(contract._create_invoice(contract.season_start, month_start.date() - timedelta(days=1)))

    def test_company_and_report_layout(self):
        company = self.env.company
        self.assertEqual(company.name, demo_world.COMPANY_NAME)
        self.assertTrue(company.external_report_layout_id)

    def test_a_new_mission_can_be_planned_on_the_world(self):
        snapshot = self.env['seeding.weather.snapshot'].search([], order='score desc', limit=1)
        alert = self.env['seeding.cloud.alert'].browse(snapshot.action_replay()['res_id'])
        mission = self.env['seeding.mission.planner'].plan_alert(alert)
        self.assertEqual(mission.planned_by, 'rules')
        self.assertEqual(mission.readiness_state, 'pass')

    def test_second_load_is_refused(self):
        with self.assertRaisesRegex(UserError, 'already holds Serein records'):
            self.env['seeding.demo.world'].load()
        wizard = self.env['seeding.demo.world'].create({})
        self.assertIn('25 missions', wizard.blocking_reason)
        with self.assertRaises(UserError):
            call_kw(self.env['seeding.demo.world'], 'action_load', [wizard.ids], {})
        self.assertEqual(self.env['seeding.mission'].search_count([]), 25)

    def test_only_managers_load(self):
        operator = new_test_user(self.env, login='demo_operator', groups='base.group_user,serein_ops.group_serein_operator')
        with self.assertRaises(Exception):
            self.env['seeding.demo.world'].with_user(operator).create({})
        with self.assertRaisesRegex(UserError, 'Serein manager'):
            self.env['seeding.demo.world'].with_user(operator).load()


@tagged('post_install', '-at_install')
class TestDemoWorldOffline(DemoWorldCase):
    """Without the rainfall archive the world still loads, without rainfall."""

    @staticmethod
    def archive_side_effect(*args):
        raise requests.exceptions.ConnectionError('offline')

    def test_loads_without_rainfall(self):
        self.assertEqual(self.counts['missions'], 25)
        self.assertEqual(self.counts['rainfall'], 0)
        self.assertFalse(any(self.missions.mapped('rainfall_recorded_at')))
        self.assertTrue(self.archive_request.called)
        self.assertTrue(all(self.missions.mapped('client_summary')))
