import base64
from ast import literal_eval
from datetime import date, timedelta

from odoo import fields
from odoo.tests import Form, TransactionCase, tagged
from odoo.tools import file_open


@tagged('post_install', '-at_install')
class TestPolish(TransactionCase):
    """Phase 9: Serein's own fleet forms, empty states, and the app icon."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = cls.env['seeding.base'].create({'name': 'Coastal Airfield'})

    def _action_form(self, xmlid):
        action = self.env.ref(xmlid)
        return action, action.view_ids.filtered(lambda v: v.view_mode == 'form').view_id

    # Aircraft ----------------------------------------------------------------

    def test_aircraft_menu_opens_the_aircraft_form(self):
        action, form = self._action_form('serein_ops.maintenance_equipment_action_aircraft')
        self.assertEqual(form, self.env.ref('serein_ops.maintenance_equipment_view_form_aircraft'))
        arch = self.env['maintenance.equipment'].get_views([(form.id, 'form')])['views']['form']['arch']
        self.assertIn('maintenance_open_count', arch)  # Maintenance button
        self.assertIn('action_view_seeding_missions', arch)  # Missions button
        # The standard equipment form keeps its Aircraft tab for the Maintenance app.
        self.assertIn('serein_aircraft', self.env['maintenance.equipment'].get_views(
            [(self.env.ref('maintenance.hr_equipment_view_form').id, 'form')])['views']['form']['arch'])

    def test_new_aircraft_from_the_aircraft_form(self):
        action, form_view = self._action_form('serein_ops.maintenance_equipment_action_aircraft')
        form = Form(self.env['maintenance.equipment'].with_context(default_is_aircraft=True), view=form_view)
        form.name = 'Cessna 340'
        form.aircraft_type = 'crewed'
        form.tail_number = 'A6-SRN'
        form.home_base_id = self.base
        form.rack_capacity = 16
        aircraft = form.save()
        self.assertTrue(aircraft.is_aircraft)
        self.assertTrue(aircraft.maintenance_team_id, "aircraft get the default maintenance team")
        self.assertTrue(aircraft.is_airworthy)
        self.assertEqual(aircraft.seeding_mission_count, 0)

    def test_aircraft_form_requires_type_and_tail_number(self):
        action, form_view = self._action_form('serein_ops.maintenance_equipment_action_aircraft')
        form = Form(self.env['maintenance.equipment'].with_context(default_is_aircraft=True), view=form_view)
        form.name = 'Unnamed'
        with self.assertRaises(AssertionError):
            form.save()

    # Pilots ------------------------------------------------------------------

    def test_pilot_menu_opens_the_pilot_form(self):
        action, form = self._action_form('serein_ops.hr_employee_action_pilot')
        self.assertEqual(form, self.env.ref('serein_ops.hr_employee_view_form_pilot'))
        arch = self.env['hr.employee'].get_views([(form.id, 'form')])['views']['form']['arch']
        for field in ('pilot_role', 'home_base_id', 'pilot_licence_number', 'licence_expiry', 'medical_expiry',
                      'duty_hours_7d', 'weekly_duty_limit', 'pilot_hourly_cost'):
            self.assertIn(field, arch)
        self.assertIn('serein_pilot', self.env['hr.employee'].get_views(
            [(self.env.ref('hr.view_employee_form').id, 'form')])['views']['form']['arch'])

    def test_new_pilot_from_the_pilot_form(self):
        action, form_view = self._action_form('serein_ops.hr_employee_action_pilot')
        form = Form(self.env['hr.employee'].with_context(literal_eval(action.context)),
                    view=form_view)
        form.name = 'Ayla Rahman'
        form.pilot_licence_number = 'ATPL-7781'
        form.licence_expiry = date(2028, 6, 30)
        form.medical_expiry = date(2027, 6, 30)
        pilot = form.save()
        self.assertTrue(pilot.is_pilot)
        self.assertEqual(pilot.pilot_role, 'pilot')
        self.assertEqual(pilot.seeding_mission_count, 0)
        missions = pilot.action_view_seeding_missions()
        self.assertEqual(missions['domain'], [('pilot_id', '=', pilot.id)])

    def test_drone_operator_form_hides_nothing_required(self):
        action, form_view = self._action_form('serein_ops.hr_employee_action_pilot')
        form = Form(self.env['hr.employee'].with_context(default_is_pilot=True), view=form_view)
        form.name = 'Tomas Lindqvist'
        form.pilot_role = 'drone_operator'
        form.pilot_licence_number = 'RPAS-118'
        form.licence_expiry = date(2028, 1, 31)
        operator = form.save()
        self.assertEqual(operator.pilot_role, 'drone_operator')
        self.assertFalse(operator.medical_expiry)

    # Flares ------------------------------------------------------------------

    def test_new_flare_from_the_flare_form(self):
        action, form_view = self._action_form('serein_ops.product_template_action_flare')
        ctx = literal_eval(action.context)
        form = Form(self.env['product.template'].with_context(ctx), view=form_view)
        form.name = 'Glaciogenic Flare 150 g'
        form.grams_per_flare = 150
        form.standard_price = 60
        with form.seller_ids.new() as line:
            line.partner_id = self.env['res.partner'].create({'name': 'Altostratus Pyrotechnics'})
            line.price = 60
        flare = form.save()
        self.assertTrue(flare.is_seeding_flare)
        self.assertTrue(flare.is_storable)
        self.assertEqual(flare.tracking, 'lot')
        self.assertTrue(flare.purchase_ok)
        self.assertEqual(flare.seller_ids.partner_id.name, 'Altostratus Pyrotechnics')

    # Empty states and icon ---------------------------------------------------

    def test_every_serein_window_action_explains_its_empty_state(self):
        data = self.env['ir.model.data'].search([
            ('module', '=', 'serein_ops'), ('model', '=', 'ir.actions.act_window'),
        ])
        actions = self.env['ir.actions.act_window'].browse(data.mapped('res_id'))
        self.assertTrue(actions)
        for action in actions:
            if action.target == 'new':
                continue  # dialogs
            self.assertTrue(action.help, f"{action.name} has no empty-state help")

    def test_app_icon_is_a_square_png(self):
        with file_open('serein_ops/static/description/icon.png', 'rb') as f:
            data = f.read()
        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
        width = int.from_bytes(data[16:20], 'big')
        height = int.from_bytes(data[20:24], 'big')
        self.assertEqual(width, height)
        self.assertGreaterEqual(width, 128)
        menu = self.env.ref('serein_ops.menu_serein_root')
        self.assertEqual(menu.web_icon, 'serein_ops,static/description/icon.png')
        self.assertTrue(base64.b64decode(menu.web_icon_data))

    def test_snapshot_and_area_score_bands_follow_the_threshold(self):
        self.env['ir.config_parameter'].set_param('serein_ops.seedability_threshold', 60)
        client = self.env['res.partner'].create({'name': 'Lakeside Agritech', 'is_company': True})
        area = self.env['seeding.target.area'].create({
            'name': 'Lakeside', 'partner_id': client.id, 'latitude': 1.4, 'longitude': 103.8,
        })
        Snapshot = self.env['seeding.weather.snapshot']

        def snap(low, hours_ago):
            return Snapshot.create({
                'area_id': area.id, 'valid_for': fields.Datetime.now() - timedelta(hours=hours_ago),
                'fetched_at': fields.Datetime.now() - timedelta(hours=hours_ago),
                'cloud_cover_low': low, 'cape': 1000, 'wind_speed': 10,
            })
        # 30 instability + 20 wind, plus 50 x low cloud.
        self.assertEqual(snap(100, 3).score_band, 'seedable')  # 100
        self.assertEqual(snap(0, 2).score_band, 'marginal')  # 50
        poor = Snapshot.create({'area_id': area.id, 'valid_for': fields.Datetime.now(),
                                'fetched_at': fields.Datetime.now(), 'wind_speed': 60})
        self.assertEqual(poor.score, 0)
        self.assertEqual(poor.score_band, 'poor')
        self.assertEqual(area.last_score_band, 'poor')
        self.env['ir.config_parameter'].set_param('serein_ops.seedability_threshold', 0)
        poor.invalidate_recordset(['score_band'])
        self.assertEqual(poor.score_band, 'seedable')

    def test_snapshots_search_by_seedability(self):
        self.env['ir.config_parameter'].set_param('serein_ops.seedability_threshold', 60)
        client = self.env['res.partner'].create({'name': 'Lakeside Agritech', 'is_company': True})
        area = self.env['seeding.target.area'].create({
            'name': 'Lakeside', 'partner_id': client.id, 'latitude': 1.4, 'longitude': 103.8,
        })
        Snapshot = self.env['seeding.weather.snapshot']
        made = {}
        for band, low in (('seedable', 100), ('marginal', 0)):
            made[band] = Snapshot.create({
                'area_id': area.id, 'valid_for': fields.Datetime.now(),
                'cloud_cover_low': low, 'cape': 1000, 'wind_speed': 10,
            })
        made['poor'] = Snapshot.create({'area_id': area.id, 'valid_for': fields.Datetime.now(), 'wind_speed': 60})
        for band, snap in made.items():
            found = Snapshot.search([('area_id', '=', area.id), ('score_band', '=', band)])
            self.assertEqual(found, snap, band)
        others = Snapshot.search([('area_id', '=', area.id), ('score_band', '!=', 'seedable')])
        self.assertEqual(others, made['marginal'] | made['poor'])
        self.assertEqual(Snapshot.search_count([('area_id', '=', area.id), ('score_band', 'in', ['seedable', 'poor'])]), 2)
