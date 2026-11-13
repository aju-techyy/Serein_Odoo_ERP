from datetime import date, datetime, timedelta

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, TransactionCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestBilling(TransactionCase):
    """Phase 7: contract invoices for a period, billed missions, no double
    billing, and mission cost."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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
        # Season 15 Oct 2026 to 31 Jan 2027: 1000 a standby day, 2500 a flight hour.
        cls.contract = cls.env['seeding.contract'].create({
            'partner_id': cls.client.id,
            'area_ids': [(6, 0, cls.area.ids)],
            'season_start': date(2026, 10, 15),
            'season_end': date(2027, 1, 31),
            'flight_hour_budget': 500,
            'standby_fee': 1000,
            'flight_hour_rate': 2500,
        })
        cls.contract.action_activate()
        cls.other_contract = cls.env['seeding.contract'].create({
            'partner_id': cls.other_client.id,
            'area_ids': [(6, 0, cls.other_area.ids)],
            'season_start': date(2026, 10, 1),
            'season_end': date(2027, 3, 31),
            'flight_hour_budget': 100,
            'standby_fee': 500,
            'flight_hour_rate': 1800,
        })
        cls.other_contract.action_activate()

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
            'name': 'Mara Okafor', 'is_pilot': True, 'pilot_role': 'pilot',
            'pilot_hourly_cost': 120.0,
        })
        cls.november = (date(2026, 11, 1), date(2026, 11, 30))

    # Helpers ---------------------------------------------------------------

    def _flown(self, landing, hours, contract=None, state='completed', fired=10):
        """A completed mission recorded directly, landing at the given UTC time."""
        contract = contract or self.contract
        return self.env['seeding.mission'].create({
            'area_id': contract.area_ids[:1].id, 'contract_id': contract.id,
            'aircraft_id': self.king_air.id, 'pilot_id': self.pilot.id,
            'flare_product_id': self.flare.id, 'flares_planned': 16, 'flares_fired': fired,
            'takeoff_at': landing - timedelta(hours=hours), 'landing_at': landing,
            'state': state,
        })

    def _three_november_missions(self):
        return (
            self._flown(datetime(2026, 11, 3, 9, 0), 1.5)
            | self._flown(datetime(2026, 11, 12, 14, 30), 2.0)
            | self._flown(datetime(2026, 11, 28, 23, 15), 0.75)
        )

    def _invoice(self, date_from, date_to, contract=None):
        return (contract or self.contract)._create_invoice(date_from, date_to)

    def _standby_lines(self, invoice):
        return invoice.invoice_line_ids.filtered('seeding_standby_start')

    # Products --------------------------------------------------------------

    def test_service_products_exist(self):
        standby = self.env.ref('serein_ops.product_seeding_standby_day')
        flight = self.env.ref('serein_ops.product_seeding_flight_hour')
        self.assertEqual((standby.type, flight.type), ('service', 'service'))
        self.assertEqual(standby.uom_id, self.env.ref('uom.product_uom_day'))
        self.assertEqual(flight.uom_id, self.env.ref('uom.product_uom_hour'))
        self.assertTrue(standby.sale_ok and flight.sale_ok)

    # Totals ----------------------------------------------------------------

    def test_month_with_three_missions_invoices_to_the_hand_total(self):
        missions = self._three_november_missions()
        # Not on the November invoice: another month, not flown, another contract.
        october = self._flown(datetime(2026, 10, 31, 23, 30), 1.0)
        december = self._flown(datetime(2026, 12, 1, 0, 10), 1.0)
        proposed = self.env['seeding.mission'].create({
            'area_id': self.area.id, 'contract_id': self.contract.id,
        })
        other = self._flown(datetime(2026, 11, 10, 10, 0), 3.0, contract=self.other_contract)

        invoice = self._invoice(*self.november)

        # By hand: 30 standby days x 1000 = 30000; (1.5 + 2.0 + 0.75) h x 2500 = 10625.
        self.assertEqual(invoice.amount_untaxed, 40625.0)
        self.assertEqual(invoice.move_type, 'out_invoice')
        self.assertEqual(invoice.state, 'draft')
        self.assertEqual(invoice.partner_id, self.client)
        self.assertEqual(invoice.seeding_contract_id, self.contract)
        self.assertEqual(invoice.invoice_origin, self.contract.name)
        self.assertEqual((invoice.seeding_period_start, invoice.seeding_period_end), self.november)

        standby = self._standby_lines(invoice)
        self.assertEqual(len(standby), 1)
        self.assertEqual(standby.quantity, 30)
        self.assertEqual(standby.price_unit, 1000)
        self.assertEqual(standby.product_id, self.env.ref('serein_ops.product_seeding_standby_day'))
        self.assertEqual((standby.seeding_standby_start, standby.seeding_standby_end), self.november)

        flight_lines = invoice.invoice_line_ids.filtered('seeding_mission_id')
        self.assertEqual(flight_lines.seeding_mission_id, missions)
        self.assertEqual(sorted(flight_lines.mapped('quantity')), [0.75, 1.5, 2.0])
        self.assertEqual(set(flight_lines.mapped('price_unit')), {2500})
        self.assertEqual(flight_lines.product_id, self.env.ref('serein_ops.product_seeding_flight_hour'))
        self.assertIn(missions[0].name, flight_lines.filtered(lambda l: l.seeding_mission_id == missions[0]).name)

        self.assertEqual(set(missions.mapped('state')), {'billed'})
        self.assertEqual(missions.invoice_id, invoice)
        for mission in october | december | other:
            self.assertEqual((mission.state, mission.invoice_id.id), ('completed', False))
        self.assertEqual(proposed.state, 'proposed')
        self.assertEqual(self.contract.invoice_count, 1)

    def test_running_again_creates_nothing(self):
        self._three_november_missions()
        self._invoice(*self.november)
        moves_before = self.env['account.move'].search_count([])

        again = self._invoice(*self.november)

        self.assertFalse(again)
        self.assertEqual(self.env['account.move'].search_count([]), moves_before)
        self.assertEqual(self.contract.invoice_count, 1)

    def test_wizard_previews_the_total_and_reports_nothing_left(self):
        missions = self._three_november_missions()
        wizard = self.env['seeding.contract.invoice.wizard'].create({
            'contract_id': self.contract.id,
            'date_from': self.november[0], 'date_to': self.november[1],
        })
        self.assertEqual(wizard.standby_days, 30)
        self.assertEqual(wizard.mission_ids, missions)
        self.assertEqual(wizard.flight_hours, 4.25)
        self.assertEqual(wizard.standby_amount, 30000)
        self.assertEqual(wizard.flight_amount, 10625)
        self.assertEqual(wizard.amount_untaxed, 40625)

        action = wizard.action_create_invoice()
        invoice = self.env['account.move'].browse(action['res_id'])
        self.assertEqual(action['res_model'], 'account.move')
        self.assertEqual(invoice.amount_untaxed, wizard.amount_untaxed)

        again = self.env['seeding.contract.invoice.wizard'].create({
            'contract_id': self.contract.id,
            'date_from': self.november[0], 'date_to': self.november[1],
        })
        self.assertEqual((again.standby_days, again.amount_untaxed), (0, 0))
        result = again.action_create_invoice()
        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(self.contract.invoice_count, 1)

    def test_wizard_defaults_to_the_current_month(self):
        action = self.contract.action_create_invoice()
        wizard = Form(self.env[action['res_model']].with_context(action['context']))
        self.assertEqual(wizard.date_from.day, 1)
        self.assertEqual((wizard.date_to + timedelta(days=1)).day, 1)
        self.assertEqual(wizard.date_from.month, wizard.date_to.month)

    def test_later_mission_in_an_invoiced_month_is_billed_alone(self):
        self._three_november_missions()
        self._invoice(*self.november)
        late = self._flown(datetime(2026, 11, 29, 8, 0), 1.25)

        invoice = self._invoice(*self.november)

        self.assertFalse(self._standby_lines(invoice))
        self.assertEqual(invoice.invoice_line_ids.seeding_mission_id, late)
        self.assertEqual(invoice.amount_untaxed, 3125.0)
        self.assertEqual(late.state, 'billed')

    def test_overlapping_period_bills_only_new_standby_days(self):
        self._invoice(*self.november)
        december = self._flown(datetime(2026, 12, 10, 12, 0), 2.0)

        invoice = self._invoice(date(2026, 11, 15), date(2026, 12, 15))

        standby = self._standby_lines(invoice)
        self.assertEqual(standby.quantity, 15)
        self.assertEqual((standby.seeding_standby_start, standby.seeding_standby_end),
                         (date(2026, 12, 1), date(2026, 12, 15)))
        self.assertEqual(invoice.invoice_line_ids.seeding_mission_id, december)
        self.assertEqual(invoice.amount_untaxed, 15 * 1000 + 2.0 * 2500)

    def test_gap_between_invoiced_periods_gets_its_own_standby_line(self):
        self._invoice(date(2026, 11, 1), date(2026, 11, 10))
        self._invoice(date(2026, 11, 21), date(2026, 11, 30))

        invoice = self._invoice(date(2026, 11, 1), date(2026, 11, 30))

        standby = self._standby_lines(invoice)
        self.assertEqual(len(standby), 1)
        self.assertEqual((standby.seeding_standby_start, standby.seeding_standby_end, standby.quantity),
                         (date(2026, 11, 11), date(2026, 11, 20), 10))

    def test_standby_days_stay_inside_the_season(self):
        invoice = self._invoice(date(2026, 10, 1), date(2026, 10, 31))
        standby = self._standby_lines(invoice)
        # The season starts on 15 October: 17 days.
        self.assertEqual(standby.quantity, 17)
        self.assertEqual(standby.seeding_standby_start, date(2026, 10, 15))
        self.assertEqual(invoice.amount_untaxed, 17000)

        self.assertFalse(self._invoice(date(2027, 2, 1), date(2027, 2, 28)))

    def test_no_standby_fee_bills_flight_hours_only(self):
        self.contract.standby_fee = 0
        missions = self._three_november_missions()
        invoice = self._invoice(*self.november)
        self.assertFalse(self._standby_lines(invoice))
        self.assertEqual(invoice.invoice_line_ids.seeding_mission_id, missions)
        self.assertEqual(invoice.amount_untaxed, 10625)

    def test_invoice_posts_and_missions_stay_billed(self):
        missions = self._three_november_missions()
        invoice = self._invoice(*self.november)
        invoice.action_post()
        self.assertEqual(invoice.state, 'posted')
        self.assertEqual(invoice.amount_untaxed, 40625)
        self.assertAlmostEqual(invoice.amount_total, invoice.amount_untaxed + invoice.amount_tax)
        self.assertEqual(set(missions.mapped('state')), {'billed'})
        self.assertFalse(self._invoice(*self.november))

    # No double billing ---------------------------------------------------------

    def test_cancelled_invoice_frees_its_missions_and_days(self):
        missions = self._three_november_missions()
        invoice = self._invoice(*self.november)
        invoice.action_post()

        invoice.button_cancel()

        self.assertEqual(invoice.state, 'cancel')
        self.assertEqual(set(missions.mapped('state')), {'completed'})
        self.assertFalse(missions.invoice_id)
        with self.assertRaises(UserError):
            invoice.button_draft()

        again = self._invoice(*self.november)
        self.assertEqual(again.amount_untaxed, 40625)
        self.assertEqual(missions.invoice_id, again)

    def test_deleted_draft_invoice_frees_its_missions(self):
        missions = self._three_november_missions()
        invoice = self._invoice(*self.november)

        invoice.unlink()

        self.assertEqual(set(missions.mapped('state')), {'completed'})
        self.assertFalse(missions.invoice_id)
        self.assertEqual(self._invoice(*self.november).amount_untaxed, 40625)

    def test_removed_line_frees_only_its_mission(self):
        missions = self._three_november_missions()
        invoice = self._invoice(*self.november)
        line = invoice.invoice_line_ids.filtered(lambda l: l.seeding_mission_id == missions[1])

        invoice.write({'invoice_line_ids': [(2, line.id)]})

        self.assertEqual(missions[1].state, 'completed')
        self.assertFalse(missions[1].invoice_id)
        self.assertEqual((missions - missions[1]).mapped('state'), ['billed', 'billed'])
        again = self._invoice(*self.november)
        self.assertEqual(again.invoice_line_ids.seeding_mission_id, missions[1])
        self.assertEqual(again.amount_untaxed, 5000)

    def test_a_mission_cannot_go_on_a_second_invoice(self):
        mission = self._flown(datetime(2026, 11, 3, 9, 0), 1.5)
        self._invoice(*self.november)
        with self.assertRaises(ValidationError):
            self.env['account.move'].create({
                'move_type': 'out_invoice', 'partner_id': self.client.id,
                'invoice_line_ids': [(0, 0, {
                    'name': 'Flight hours', 'quantity': 1.5, 'price_unit': 2500,
                    'seeding_mission_id': mission.id,
                })],
            })

    def test_billed_missions_are_not_invoiced_by_another_contract(self):
        self._three_november_missions()
        self._invoice(*self.november)
        other = self._invoice(*self.november, contract=self.other_contract)
        self.assertFalse(other.invoice_line_ids.seeding_mission_id)
        self.assertEqual(other.amount_untaxed, 30 * 500)

    # Refusals ----------------------------------------------------------------

    def test_only_active_or_closed_contracts_are_invoiced(self):
        draft = self.env['seeding.contract'].create({
            'partner_id': self.client.id, 'area_ids': [(6, 0, self.area.ids)],
            'season_start': date(2026, 11, 1), 'season_end': date(2026, 12, 31),
            'flight_hour_budget': 10, 'standby_fee': 100, 'flight_hour_rate': 100,
        })
        with self.assertRaises(UserError):
            draft._create_invoice(*self.november)
        with self.assertRaises(UserError):
            draft.action_create_invoice()

        self._three_november_missions()
        self.contract.action_close()
        self.assertEqual(self._invoice(*self.november).amount_untaxed, 40625)

    def test_period_must_end_after_it_starts(self):
        with self.assertRaises(UserError):
            self._invoice(date(2026, 11, 30), date(2026, 11, 1))

    # Access ------------------------------------------------------------------

    def test_serein_manager_can_invoice(self):
        manager = new_test_user(self.env, login='serein_billing_mgr', groups='serein_ops.group_serein_manager')
        missions = self._three_november_missions()
        wizard = self.env['seeding.contract.invoice.wizard'].with_user(manager).create({
            'contract_id': self.contract.id,
            'date_from': self.november[0], 'date_to': self.november[1],
        })
        action = wizard.action_create_invoice()
        invoice = self.env['account.move'].browse(action['res_id'])
        self.assertEqual(invoice.amount_untaxed, 40625)
        self.assertEqual(set(missions.mapped('state')), {'billed'})

    def test_operator_cannot_invoice(self):
        operator = new_test_user(self.env, login='serein_billing_op', groups='serein_ops.group_serein_operator')
        with self.assertRaises(AccessError):
            self.env['seeding.contract.invoice.wizard'].with_user(operator).create({
                'contract_id': self.contract.id,
                'date_from': self.november[0], 'date_to': self.november[1],
            })

    # Mission cost --------------------------------------------------------------

    def test_mission_cost_from_flares_aircraft_and_pilot(self):
        mission = self._flown(datetime(2026, 11, 3, 9, 0), 2.0, fired=12)
        # 12 flares x 85 = 1020; 2 h x 400 = 800; 2 h x 120 = 240.
        self.assertEqual(mission.flare_cost, 1020)
        self.assertEqual(mission.aircraft_cost, 800)
        self.assertEqual(mission.crew_cost, 240)
        self.assertEqual(mission.mission_cost, 2060)

    def test_mission_cost_keeps_the_rates_of_its_flight(self):
        mission = self._flown(datetime(2026, 11, 3, 9, 0), 2.0, fired=12)
        self.env.flush_all()
        self.assertEqual(mission.mission_cost, 2060)
        self.king_air.aircraft_hourly_cost = 999
        self.pilot.pilot_hourly_cost = 999
        self.flare.standard_price = 999
        self.env.invalidate_all()
        self.assertEqual(mission.mission_cost, 2060)

    def test_mission_cost_is_zero_before_the_flight(self):
        mission = self.env['seeding.mission'].create({
            'area_id': self.area.id, 'contract_id': self.contract.id,
            'aircraft_id': self.king_air.id, 'pilot_id': self.pilot.id,
            'flare_product_id': self.flare.id, 'flares_planned': 12,
        })
        self.assertEqual(mission.mission_cost, 0)
        mission.write({'state': 'cancelled'})
        mission.write({
            'takeoff_at': datetime(2026, 11, 3, 9, 0), 'landing_at': datetime(2026, 11, 3, 10, 30),
            'flares_fired': 4,
        })
        self.assertEqual(mission.mission_cost, 4 * 85 + 1.5 * 400 + 1.5 * 120)
