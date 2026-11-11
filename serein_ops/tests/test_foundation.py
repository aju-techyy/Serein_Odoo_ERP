from datetime import date

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger


@tagged('post_install', '-at_install')
class TestFoundation(TransactionCase):
    """Phase 1: the master data the rest of Serein builds on."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env['res.partner'].create({'name': 'Northwind Water Authority', 'is_company': True})
        cls.other_client = cls.env['res.partner'].create({'name': 'Lakeside Agritech', 'is_company': True})
        cls.base = cls.env['seeding.base'].create({'name': 'Coastal Airfield'})
        cls.area_north = cls.env['seeding.target.area'].create({
            'name': 'Northern Highlands', 'partner_id': cls.client.id,
            'latitude': 24.5, 'longitude': 55.9, 'radius_km': 40,
        })
        cls.area_south = cls.env['seeding.target.area'].create({
            'name': 'Southern Valley', 'partner_id': cls.client.id,
            'latitude': 23.9, 'longitude': 55.4, 'radius_km': 30,
        })

    def _contract(self, **vals):
        return self.env['seeding.contract'].create({
            'partner_id': self.client.id,
            'area_ids': [(6, 0, (self.area_north | self.area_south).ids)],
            'season_start': date(2026, 11, 1),
            'season_end': date(2027, 3, 31),
            'flight_hour_budget': 120,
            'standby_fee': 1500,
            'flight_hour_rate': 2500,
            **vals,
        })

    # Bases -----------------------------------------------------------------

    def test_base_gets_internal_stock_location(self):
        self.assertTrue(self.base.location_id)
        self.assertEqual(self.base.location_id.usage, 'internal')
        self.assertEqual(self.base.location_id.name, 'Coastal Airfield')

    def test_base_keeps_given_location(self):
        location = self.env['stock.location'].create({'name': 'Hangar 2', 'usage': 'internal'})
        base = self.env['seeding.base'].create({'name': 'Mountain Strip', 'location_id': location.id})
        self.assertEqual(base.location_id, location)

    # Target areas ----------------------------------------------------------

    @mute_logger('odoo.sql_db')
    def test_area_rejects_bad_latitude(self):
        with self.assertRaises(IntegrityError):
            self.env['seeding.target.area'].create({
                'name': 'Nowhere', 'partner_id': self.client.id,
                'latitude': 95, 'longitude': 10,
            })

    # Contracts -------------------------------------------------------------

    def test_contract_with_two_areas(self):
        contract = self._contract()
        self.assertTrue(contract.name.startswith('CTR/'))
        self.assertEqual(len(contract.area_ids), 2)
        self.assertEqual(contract.state, 'draft')
        self.assertEqual(self.area_north.contract_ids, contract)
        self.assertEqual(contract.hours_used, 0)
        self.assertEqual(contract.hours_remaining, 120)

    def test_contract_lifecycle(self):
        contract = self._contract()
        contract.action_activate()
        self.assertEqual(contract.state, 'active')
        contract.action_close()
        self.assertEqual(contract.state, 'closed')
        with self.assertRaises(UserError):
            contract.action_activate()

    def test_contract_needs_area_to_activate(self):
        contract = self._contract(area_ids=[])
        with self.assertRaises(UserError):
            contract.action_activate()

    def test_contract_rejects_other_clients_area(self):
        foreign = self.env['seeding.target.area'].create({
            'name': 'Lakeside Plain', 'partner_id': self.other_client.id,
            'latitude': -1.3, 'longitude': 36.8,
        })
        with self.assertRaises(ValidationError):
            self._contract(area_ids=[(6, 0, foreign.ids)])

    def test_area_client_cannot_change_under_contract(self):
        self._contract()
        with self.assertRaises(ValidationError):
            self.area_north.partner_id = self.other_client

    @mute_logger('odoo.sql_db')
    def test_contract_season_order(self):
        with self.assertRaises(IntegrityError):
            self._contract(season_start=date(2027, 3, 31), season_end=date(2026, 11, 1))

    # Aircraft --------------------------------------------------------------

    def _aircraft(self, **vals):
        return self.env['maintenance.equipment'].create({
            'name': 'King Air 350', 'is_aircraft': True, 'aircraft_type': 'crewed',
            'tail_number': 'A6-SRA', 'home_base_id': self.base.id,
            'rack_capacity': 24, 'service_interval_hours': 100,
            'aircraft_hourly_cost': 900, **vals,
        })

    def test_crewed_aircraft_and_drone(self):
        plane = self._aircraft()
        drone = self._aircraft(name='Seeder Drone 1', aircraft_type='drone',
                               tail_number='UAS-001', rack_capacity=4)
        self.assertTrue(plane.is_airworthy)
        self.assertTrue(drone.is_airworthy)
        self.assertEqual(self.base.aircraft_ids, plane | drone)
        self.assertEqual(self.base.aircraft_count, 2)

    @mute_logger('odoo.sql_db')
    def test_tail_number_unique(self):
        self._aircraft()
        with self.assertRaises(IntegrityError):
            self._aircraft(name='Duplicate')

    def test_aircraft_gets_default_maintenance_team(self):
        plane = self._aircraft()
        self.assertTrue(plane.maintenance_team_id)
        # A request opened from the aircraft's Maintenance button inherits it.
        request = self.env['maintenance.request'].with_context(
            default_maintenance_team_id=plane.maintenance_team_id.id,
        ).create({'name': 'Check', 'equipment_id': plane.id})
        self.assertEqual(request.maintenance_team_id, plane.maintenance_team_id)

    def test_client_must_be_a_company(self):
        person = self.env['res.partner'].create({'name': 'Mara Okafor'})
        domain = self.env['seeding.target.area']._fields['partner_id'].domain
        self.assertFalse(person.filtered_domain(domain))
        self.assertTrue(self.client.filtered_domain(domain))

    def test_open_corrective_request_grounds_aircraft(self):
        plane = self._aircraft()
        request = self.env['maintenance.request'].create({
            'name': 'Hydraulic leak', 'equipment_id': plane.id,
            'maintenance_type': 'corrective',
        })
        self.assertFalse(plane.is_airworthy)
        done_stage = self.env['maintenance.stage'].search([('done', '=', True)], limit=1)
        request.stage_id = done_stage
        self.assertTrue(plane.is_airworthy)

    def test_preventive_request_does_not_ground(self):
        plane = self._aircraft()
        self.env['maintenance.request'].create({
            'name': 'Scheduled check', 'equipment_id': plane.id,
            'maintenance_type': 'preventive',
        })
        self.assertTrue(plane.is_airworthy)

    def test_zero_interval_is_not_airworthy(self):
        self.assertFalse(self._aircraft(service_interval_hours=0).is_airworthy)

    def test_plain_equipment_is_not_aircraft(self):
        printer = self.env['maintenance.equipment'].create({'name': 'Office Printer'})
        self.assertFalse(printer.is_aircraft)
        self.assertFalse(printer.is_airworthy)

    # Pilots and flares -----------------------------------------------------

    def test_pilot(self):
        pilot = self.env['hr.employee'].create({
            'name': 'Mara Okafor', 'is_pilot': True, 'pilot_role': 'pilot',
            'pilot_licence_number': 'CPL-20431', 'licence_expiry': date(2027, 6, 30),
            'medical_expiry': date(2027, 1, 31), 'pilot_hourly_cost': 120,
        })
        self.assertEqual(pilot.duty_hours_7d, 0)
        self.assertEqual(pilot.weekly_duty_limit, 30)

    def test_flare_product(self):
        flare = self.env['product.template'].create({
            'name': 'Hygroscopic Flare 1 kg', 'type': 'consu', 'is_storable': True,
            'is_seeding_flare': True, 'grams_per_flare': 1000,
        })
        self.assertTrue(flare.is_seeding_flare)
        self.assertEqual(flare.grams_per_flare, 1000)

    # Security --------------------------------------------------------------

    def test_operator_reads_but_cannot_create_contracts(self):
        operator = new_test_user(self.env, login='serein_op', groups='serein_ops.group_serein_operator')
        contract = self._contract()
        self.assertEqual(contract.with_user(operator).name, contract.name)
        with self.assertRaises(AccessError):
            self.env['seeding.contract'].with_user(operator).create({
                'partner_id': self.client.id, 'season_start': date(2026, 11, 1),
                'season_end': date(2027, 3, 31), 'flight_hour_budget': 10,
            })

    def test_operator_sees_aircraft(self):
        plane = self._aircraft()
        operator = new_test_user(self.env, login='serein_op2', groups='serein_ops.group_serein_operator')
        found = self.env['maintenance.equipment'].with_user(operator).search([('is_aircraft', '=', True)])
        self.assertIn(plane, found)

    def test_manager_creates_master_data(self):
        manager = new_test_user(self.env, login='serein_mgr', groups='serein_ops.group_serein_manager')
        env = self.env(user=manager)
        base = env['seeding.base'].create({'name': 'Desert Strip'})
        self.assertTrue(base.location_id)
        area = env['seeding.target.area'].create({
            'name': 'Ridge', 'partner_id': self.client.id, 'latitude': 25, 'longitude': 56,
        })
        env['seeding.contract'].create({
            'partner_id': self.client.id, 'area_ids': [(6, 0, area.ids)],
            'season_start': date(2026, 11, 1), 'season_end': date(2027, 3, 31),
            'flight_hour_budget': 50,
        })
        env['maintenance.equipment'].create({
            'name': 'Drone 7', 'is_aircraft': True, 'aircraft_type': 'drone', 'tail_number': 'UAS-007',
        })
