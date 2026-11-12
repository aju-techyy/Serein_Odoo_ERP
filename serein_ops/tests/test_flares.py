import os
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import Form, TransactionCase, new_test_user, tagged

from odoo.addons.serein_ops.models.ai_service import API_KEY_ENV

REPORT = 'serein_ops.action_report_seeding_mission'


@tagged('post_install', '-at_install')
class TestFlaresAndMaintenance(TransactionCase):
    """Phase 6: flares by batch, the release transfer, purchasing, service
    intervals, and duty hours."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Mark Ready and Complete must never call Gemini here.
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
            'flight_hour_budget': 500,
            'standby_fee': 1500,
            'flight_hour_rate': 2500,
        })
        cls.contract.action_activate()

        cls.base = cls.env['seeding.base'].create({'name': 'Coastal Airfield'})
        cls.vendor = cls.env['res.partner'].create({'name': 'Altostratus Pyrotechnics', 'is_company': True})
        cls.flare = cls.env['product.product'].create({
            'name': 'Hygroscopic Flare 1 kg', 'type': 'consu', 'is_storable': True,
            'tracking': 'lot', 'is_seeding_flare': True, 'grams_per_flare': 1000,
            'purchase_ok': True,
            'seller_ids': [(0, 0, {'partner_id': cls.vendor.id, 'price': 85.0})],
        })
        # Two batches: the older one (8 flares) is used first.
        cls.lot_a = cls._receive('HF-2611-A', 8, days_ago=20)
        cls.lot_b = cls._receive('HF-2611-B', 30, days_ago=5)

        cls.king_air = cls.env['maintenance.equipment'].create({
            'is_aircraft': True, 'home_base_id': cls.base.id, 'service_interval_hours': 100,
            'name': 'King Air 350', 'aircraft_type': 'crewed', 'tail_number': 'A6-SRA',
            'rack_capacity': 24,
        })
        cls.pilot = cls.env['hr.employee'].create({
            'name': 'Mara Okafor', 'is_pilot': True, 'pilot_role': 'pilot', 'weekly_duty_limit': 30,
            'pilot_licence_number': 'CPL-20431', 'licence_expiry': today + timedelta(days=365),
            'medical_expiry': today + timedelta(days=180),
        })
        cls.snapshot = cls.env['seeding.weather.snapshot'].create({
            'area_id': cls.area.id, 'valid_for': cls.now,
            'cloud_cover_low': 80, 'cloud_cover_mid': 60, 'cape': 800, 'wind_speed': 15,
        })

    @classmethod
    def _receive(cls, name, qty, days_ago=0, location=None):
        lot = cls.env['stock.lot'].create({'name': name, 'product_id': cls.flare.id})
        cls.env['stock.quant']._update_available_quantity(
            cls.flare, location or cls.base.location_id, qty, lot_id=lot,
            in_date=fields.Datetime.now() - timedelta(days=days_ago),
        )
        return lot

    # Helpers ---------------------------------------------------------------

    def _on_hand(self, lot=None):
        product = self.flare.with_context(location=self.base.location_id.id)
        if lot:
            product = product.with_context(lot_id=lot.id)
        return product.qty_available

    def _mission(self, **vals):
        alert = self.env['seeding.cloud.alert'].create({
            'snapshot_id': self.snapshot.id,
            'window_start': self.now, 'window_end': self.now + timedelta(hours=3),
        })
        return self.env['seeding.mission'].create({
            'alert_id': alert.id, 'aircraft_id': self.king_air.id, 'pilot_id': self.pilot.id,
            'flares_planned': 16, **vals,
        })

    def _fly(self, mission=None, fired=12, hours=2.0, landed_ago=timedelta(0)):
        mission = mission or self._mission()
        mission.action_ready()
        self.assertEqual(mission.state, 'ready', mission.readiness_ids.mapped('explanation'))
        mission.action_launch()
        landing = self.now - landed_ago
        mission.write({
            'takeoff_at': landing - timedelta(hours=hours),
            'landing_at': landing,
            'flares_fired': fired,
        })
        mission.action_complete()
        return mission

    def _check(self, mission, code):
        return mission.readiness_ids.filtered(lambda c: c.code == code)

    def _close(self, request):
        request.stage_id = self.env['maintenance.stage'].search([('done', '=', True)], limit=1)

    def _past_flight(self, hours, landed_ago, aircraft=None, pilot=None, state='completed'):
        """A flown mission recorded directly, without the completion flow."""
        landing = self.now - landed_ago
        return self.env['seeding.mission'].create({
            'area_id': self.area.id, 'state': state, 'flares_planned': 4, 'flares_fired': 0,
            'aircraft_id': (aircraft or self.king_air).id, 'pilot_id': (pilot or self.pilot).id,
            'takeoff_at': landing - timedelta(hours=hours), 'landing_at': landing,
        })

    # Setup -------------------------------------------------------------------

    def test_lots_and_storage_locations_are_on(self):
        implied = self.env.ref('base.group_user').all_implied_ids
        self.assertIn(self.env.ref('stock.group_production_lot'), implied)
        self.assertIn(self.env.ref('stock.group_stock_multi_locations'), implied)

    def test_release_location_and_operation_type_exist(self):
        location = self.env.company._seeding_release_location()
        self.assertEqual(location.name, 'Released to Atmosphere')
        self.assertEqual(location.usage, 'inventory')
        warehouse = self.base.location_id.warehouse_id
        picking_type = self.env.company._seeding_release_picking_type(warehouse)
        self.assertTrue(picking_type.is_seeding_release)
        # Internal, so the transfer form and traceability show the destination.
        self.assertEqual(picking_type.code, 'internal')
        self.assertEqual(picking_type.default_location_dest_id, location)
        # Asking again returns the same records.
        self.assertEqual(self.env.company._seeding_release_location(), location)
        self.assertEqual(self.env.company._seeding_release_picking_type(warehouse), picking_type)

    def test_setup_turns_an_old_delivery_release_type_internal(self):
        warehouse = self.base.location_id.warehouse_id
        picking_type = self.env.company._seeding_release_picking_type(warehouse)
        picking_type.code = 'outgoing'
        self.env['res.company']._seeding_setup_flare_stock()
        self.assertEqual(picking_type.code, 'internal')
        self.assertEqual(picking_type.default_location_dest_id, self.env.company._seeding_release_location())
        self.assertEqual(picking_type.default_location_src_id, warehouse.lot_stock_id)

    def test_flares_menu_opens_the_product_form_with_stock_buttons(self):
        action = self.env.ref('serein_ops.product_template_action_flare')
        form = action.view_ids.filtered(lambda v: v.view_mode == 'form').view_id
        self.assertEqual(form, self.env.ref('product.product_template_only_form_view'))
        arch = self.env['product.template'].get_views([(form.id, 'form')])['views']['form']['arch']
        self.assertIn('action_open_quants', arch)  # On Hand
        self.assertIn('action_view_po', arch)  # Purchased
        self.assertIn('serein_flare', arch)  # Cloud Seeding tab

    def test_base_location_is_inside_warehouse_stock(self):
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', self.env.company.id)], limit=1)
        self.assertEqual(self.base.location_id.location_id, warehouse.lot_stock_id)
        self.assertEqual(self.base.location_id.warehouse_id, warehouse)

    def test_setup_moves_old_base_location_into_stock(self):
        warehouse = self.base.location_id.warehouse_id
        old = self.env['stock.location'].create({
            'name': 'Mountain Strip', 'usage': 'internal', 'location_id': warehouse.view_location_id.id,
        })
        base = self.env['seeding.base'].create({'name': 'Mountain Strip', 'location_id': old.id})
        self.env['res.company']._seeding_setup_flare_stock()
        self.assertEqual(base.location_id, old)
        self.assertEqual(old.location_id, warehouse.lot_stock_id)

    def test_new_flare_form_is_lot_tracked_and_bought(self):
        form = Form(self.env['product.template'].with_context(default_is_seeding_flare=True))
        form.name = 'Glaciogenic Flare 150 g'
        flare = form.save()
        self.assertTrue(flare.is_storable)
        self.assertEqual(flare.tracking, 'lot')
        self.assertTrue(flare.purchase_ok)

    # Release transfer --------------------------------------------------------

    def test_completion_lowers_base_stock_by_flares_fired_by_batch(self):
        self.assertEqual(self._on_hand(), 38)
        mission = self._fly(fired=12)

        self.assertEqual(mission.state, 'completed')
        self.assertEqual(self._on_hand(), 26)
        # First in, first out: all 8 of batch A, then 4 of batch B.
        self.assertEqual(self._on_hand(self.lot_a), 0)
        self.assertEqual(self._on_hand(self.lot_b), 26)

        picking = mission.picking_id
        self.assertEqual(picking.state, 'done')
        self.assertTrue(picking.picking_type_id.is_seeding_release)
        self.assertEqual(picking.location_id, self.base.location_id)
        self.assertEqual(picking.location_dest_id, self.env.company._seeding_release_location())
        self.assertEqual(picking.origin, mission.name)
        self.assertEqual(picking.seeding_mission_id, mission)
        lines = {line.lot_id.name: line.quantity for line in picking.move_line_ids}
        self.assertEqual(lines, {'HF-2611-A': 8, 'HF-2611-B': 4})
        self.assertEqual(mission._flare_batch_lines(), [
            {'batch': 'HF-2611-A', 'flares': 8}, {'batch': 'HF-2611-B', 'flares': 4},
        ])
        self.assertEqual(mission.flare_batches, 'HF-2611-A x 8, HF-2611-B x 4')
        self.assertEqual(mission.material_released_g, 12000)
        self.assertTrue(any(picking.name in body for body in mission.message_ids.mapped('body')))

    def test_batch_history_shows_the_release(self):
        mission = self._fly(fired=12)
        released = self.env.company._seeding_release_location()
        history = self.env['stock.move.line'].search([
            ('lot_id', '=', self.lot_b.id), ('state', '=', 'done'), ('location_dest_id', '=', released.id),
        ])
        self.assertEqual(history.picking_id, mission.picking_id)
        self.assertEqual(sum(history.mapped('quantity')), 4)

    def test_no_transfer_when_no_flare_is_fired(self):
        mission = self._fly(fired=0)
        self.assertEqual(mission.state, 'completed')
        self.assertFalse(mission.picking_id)
        self.assertEqual(self._on_hand(), 38)

    def test_completion_refused_when_base_cannot_supply_the_flares(self):
        mission = self._mission(flares_planned=16)
        mission.action_ready()
        mission.action_launch()
        # Stock was taken elsewhere after launch.
        self.env['stock.quant']._update_available_quantity(self.flare, self.base.location_id, -28, lot_id=self.lot_b)
        mission.write({'takeoff_at': self.now - timedelta(hours=2), 'landing_at': self.now, 'flares_fired': 12})
        with self.assertRaisesRegex(UserError, 'can supply 10'):
            mission.action_complete()

    def test_completion_refused_for_stock_without_batch(self):
        lot_free = self.env['seeding.base'].create({'name': 'Desert Strip'})
        self.env['stock.quant']._update_available_quantity(self.flare, lot_free.location_id, 20)
        crewed = self.env['maintenance.equipment'].create({
            'is_aircraft': True, 'home_base_id': lot_free.id, 'name': 'Cessna 340',
            'aircraft_type': 'crewed', 'tail_number': 'A6-SRB', 'rack_capacity': 16,
        })
        mission = self._mission(aircraft_id=crewed.id, flares_planned=10)
        mission.action_ready()
        mission.action_launch()
        mission.write({'takeoff_at': self.now - timedelta(hours=1), 'landing_at': self.now, 'flares_fired': 5})
        with self.assertRaisesRegex(UserError, 'can supply 0'):
            mission.action_complete()

    def test_release_is_not_repeated(self):
        mission = self._fly(fired=12)
        picking = mission.picking_id
        self.assertEqual(mission._release_fired_flares(), picking)
        self.assertEqual(self._on_hand(), 26)

    def test_operator_completes_mission_without_stock_rights(self):
        operator = new_test_user(self.env, login='op_flares', groups='serein_ops.group_serein_operator')
        self.assertFalse(operator.has_group('stock.group_stock_user'))
        mission = self._mission().with_user(operator)
        mission.action_ready()
        mission.action_launch()
        mission.write({'takeoff_at': self.now - timedelta(hours=1), 'landing_at': self.now, 'flares_fired': 6})
        mission.action_complete()
        self.assertEqual(mission.state, 'completed')
        self.assertEqual(mission.flare_batches, 'HF-2611-A x 6')
        self.assertEqual(self._on_hand(), 32)

    def test_report_and_summary_input_carry_the_batches(self):
        mission = self._fly(fired=12)
        mission.action_approve_summary()
        html, _type = self.env['ir.actions.report']._render_qweb_html(REPORT, mission.ids)
        html = html.decode()
        self.assertIn('HF-2611-A', html)
        self.assertIn('HF-2611-B', html)
        self.assertIn(mission.picking_id.name, html)
        payload = self.env['seeding.mission.writer']._summary_input(mission)
        self.assertEqual(payload['flares']['batches'], mission._flare_batch_lines())

    # Purchasing --------------------------------------------------------------

    def test_reorder_rule_proposes_a_purchase_into_the_base(self):
        orderpoint = self.env['stock.warehouse.orderpoint'].create({
            'product_id': self.flare.id,
            'location_id': self.base.location_id.id,
            'warehouse_id': self.base.location_id.warehouse_id.id,
            'product_min_qty': 30, 'product_max_qty': 60, 'trigger': 'auto',
        })
        self.assertEqual(self.base.orderpoint_count, 1)
        self._fly(fired=12)  # 38 - 12 = 26, below the minimum of 30

        # The warehouse's Buy route and the vendor on the flare make it a purchase.
        self.assertIn(self.env.ref('purchase_stock.route_warehouse0_buy'), orderpoint.warehouse_id.route_ids)
        line = self.env['purchase.order.line'].search([('orderpoint_id', '=', orderpoint.id)])
        self.assertEqual(len(line), 1)
        order = line.order_id
        self.assertEqual(order.state, 'draft')
        self.assertEqual(order.partner_id, self.vendor)
        self.assertEqual(line.product_qty, 34)  # back up to the maximum of 60

        order.button_confirm()
        receipt = order.picking_ids
        self.assertEqual(receipt.move_ids.location_dest_id, self.base.location_id)
        receipt.move_ids.move_line_ids.write({'lot_name': 'HF-2612-C', 'quantity': 34})
        receipt.move_ids.picked = True
        receipt.button_validate()
        self.assertEqual(receipt.state, 'done')
        self.assertEqual(self._on_hand(), 60)

    # Hours since service and the service request ------------------------------

    def test_hours_since_service_from_flown_missions(self):
        self.assertEqual(self.king_air.hours_since_service, 0)
        self._past_flight(3.5, timedelta(days=10))
        self._past_flight(2.0, timedelta(days=4), state='billed')
        # Not flown yet: not counted.
        self._mission()
        self.assertEqual(self.king_air.hours_since_service, 5.5)

    def test_crossing_the_interval_opens_a_request_and_grounds_the_aircraft(self):
        self.king_air.service_interval_hours = 10
        self._past_flight(9.0, timedelta(days=10))
        self.assertTrue(self.king_air.is_airworthy)
        self.assertFalse(self.king_air.maintenance_ids)

        self._fly(fired=4, hours=1.5)  # 10.5 h: the interval is crossed
        request = self.king_air.maintenance_ids
        self.assertEqual(len(request), 1)
        self.assertTrue(request.is_scheduled_service)
        self.assertEqual(request.maintenance_type, 'preventive')
        self.assertIn('10.5', request.name)
        self.assertTrue(request.maintenance_team_id)
        self.assertFalse(self.king_air.is_airworthy)

        mission = self._mission()
        check = self._check(mission, 'aircraft_airworthy')
        self.assertEqual(check.result, 'fail')
        self.assertIn('scheduled service is due', check.explanation)
        self.assertIn('10.5 h since its last service', check.explanation)
        # A different aircraft fixes it, so Re-plan is offered.
        self.assertTrue(mission.can_replan)

        # Closing the request resets the counter, and check 5 passes again.
        self._close(request)
        self.assertTrue(request.service_completed_at)
        self.assertEqual(self.king_air.hours_since_service, 0)
        self.assertEqual(self.king_air.last_service_at, request.service_completed_at)
        mission.action_rerun_checks()
        self.assertEqual(self._check(mission, 'aircraft_airworthy').result, 'pass')

    def test_no_second_request_while_one_is_open(self):
        self.king_air.service_interval_hours = 2
        self._fly(fired=2, hours=2.5)
        self.assertEqual(len(self.king_air.maintenance_ids), 1)
        self.king_air._check_service_due()
        self.assertEqual(len(self.king_air.maintenance_ids), 1)

    def test_under_the_interval_no_request(self):
        self._fly(fired=4, hours=2.0)
        self.assertFalse(self.king_air.maintenance_ids)
        self.assertEqual(self.king_air.hours_since_service, 2.0)

    def test_open_service_request_grounds_even_under_the_interval(self):
        self.env['maintenance.request'].create({
            'name': '100 h inspection', 'equipment_id': self.king_air.id,
            'maintenance_type': 'preventive', 'is_scheduled_service': True,
        })
        self.assertFalse(self.king_air.is_airworthy)
        check = self._check(self._mission(), 'aircraft_airworthy')
        self.assertEqual(check.result, 'fail')
        self.assertIn('100 h inspection is open', check.explanation)

    def test_planner_input_shows_hours_and_open_service_request(self):
        self._past_flight(3.0, timedelta(days=2))
        self.env['maintenance.request'].create({
            'name': '100 h inspection', 'equipment_id': self.king_air.id,
            'maintenance_type': 'preventive', 'is_scheduled_service': True,
        })
        alert = self.env['seeding.cloud.alert'].create({
            'snapshot_id': self.snapshot.id,
            'window_start': self.now, 'window_end': self.now + timedelta(hours=3),
        })
        data = self.env['seeding.mission.planner']._planning_input(alert, self.king_air, self.pilot)
        plane = data['aircraft'][0]
        self.assertEqual(plane['hours_since_service'], 3.0)
        self.assertEqual(plane['open_service_requests'], ['100 h inspection'])
        self.assertFalse(plane['airworthy'])
        self.assertEqual(data['pilots'][0]['duty_hours_last_7_days'], 3.0)

    def test_other_preventive_request_neither_grounds_nor_resets(self):
        self._past_flight(3.0, timedelta(days=2))
        request = self.env['maintenance.request'].create({
            'name': 'Tyre check', 'equipment_id': self.king_air.id, 'maintenance_type': 'preventive',
        })
        self.assertTrue(self.king_air.is_airworthy)
        self._close(request)
        self.assertFalse(request.service_completed_at)
        self.assertEqual(self.king_air.hours_since_service, 3.0)

    def test_reopened_service_brings_the_hours_back(self):
        self._past_flight(3.0, timedelta(days=2))
        request = self.env['maintenance.request'].create({
            'name': 'Scheduled service', 'equipment_id': self.king_air.id,
            'maintenance_type': 'preventive', 'is_scheduled_service': True,
        })
        self._close(request)
        self.assertEqual(self.king_air.hours_since_service, 0)
        request.stage_id = self.env['maintenance.stage'].search([('done', '=', False)], limit=1)
        self.assertFalse(request.service_completed_at)
        self.assertEqual(self.king_air.hours_since_service, 3.0)
        self.assertFalse(self.king_air.is_airworthy)

    def test_flights_after_the_service_count_again(self):
        self._past_flight(3.0, timedelta(days=2))
        request = self.env['maintenance.request'].create({
            'name': 'Scheduled service', 'equipment_id': self.king_air.id,
            'maintenance_type': 'preventive', 'is_scheduled_service': True,
        })
        self._close(request)
        # Serviced yesterday; the old flight landed two days ago.
        request.service_completed_at = self.now - timedelta(days=1)
        self.assertEqual(self.king_air.hours_since_service, 0)
        self._fly(fired=2, hours=1.25)
        self.assertEqual(self.king_air.hours_since_service, 1.25)

    def test_aircraft_missions_button(self):
        mission = self._fly(fired=2)
        self.assertEqual(self.king_air.seeding_mission_count, 1)
        action = self.king_air.action_view_seeding_missions()
        self.assertEqual(self.env['seeding.mission'].search(action['domain']), mission)

    # Duty hours ----------------------------------------------------------------

    def test_duty_hours_count_the_last_seven_days(self):
        self._past_flight(4.0, timedelta(days=8))
        self._past_flight(2.5, timedelta(days=3))
        self._past_flight(1.5, timedelta(hours=5), state='billed')
        self.assertEqual(self.pilot.duty_hours_7d, 4.0)

    def test_duty_hours_follow_a_completed_mission(self):
        self._fly(fired=2, hours=3.0)
        self.assertEqual(self.pilot.duty_hours_7d, 3.0)

    def test_check_10_fails_once_missions_reach_the_weekly_limit(self):
        self.pilot.weekly_duty_limit = 6
        self._past_flight(4.0, timedelta(days=1))
        mission = self._mission()
        self.assertEqual(self._check(mission, 'duty_hours').result, 'pass')
        self._past_flight(2.0, timedelta(days=2))
        mission.action_rerun_checks()
        check = self._check(mission, 'duty_hours')
        self.assertEqual(check.result, 'fail')
        self.assertIn('6.0 duty hours', check.explanation)
