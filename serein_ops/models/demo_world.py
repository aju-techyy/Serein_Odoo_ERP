"""The demo world: a complete, fictional cloud seeding operator.

Loaded once into an empty Serein database from Configuration > Load Demo
World. Dates are relative to the moment of loading.

The history is built from real weather. For each target area the loader
fetches the forecasts that were issued for the past HISTORY_DAYS days (Open-
Meteo's historical forecast archive), plans which alerts the operator flew
(demo_world_schedule), and plays that plan through the module's own code:
alerts are raised from the archived forecasts, missions pass the readiness
engine as it stood before their takeoff, fly, and release their flares by
batch; reorder rules buy flares, receipts arrive by batch, aircraft go through
their services and breakdowns, and contracts are invoiced and paid.

If the archive cannot be reached, the history falls back to a fixed set of
past missions with the recorded snapshots in data/demo_world, without alerts.
"""
import json
import logging
import random
from datetime import datetime, time, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import file_open

from .demo_world_schedule import (
    CHECKS_BEFORE_TAKEOFF, Aircraft as PlanAircraft, Area as PlanArea, Breakdown, Contract as PlanContract,
    Event, Pilot as PlanPilot, hourly_scores, plan_history,
)
from .weather_service import hourly_slice

_logger = logging.getLogger(__name__)

WEATHER_FILE = 'serein_ops/data/demo_world/weather_%s.json'
COMPANY_NAME = 'Stratus Ridge Weather Services'
HISTORY_DAYS = 150

CLIENTS = {
    'wadi_noor': {'name': 'Wadi Noor Water Authority', 'country': 'base.ae', 'city': 'Al Dhaid'},
    'sarawat': {'name': 'Sarawat Water Company', 'country': 'base.sa', 'city': 'Abha'},
    'tasik_hijau': {'name': 'Tasik Hijau Water Consortium', 'country': 'base.my', 'city': 'Kuantan'},
    'breelv': {'name': 'Breelv Kraft AS', 'country': 'base.no', 'city': 'Sogndal'},
    'monaro': {'name': 'Monaro Hydro Pty Ltd', 'country': 'base.au', 'city': 'Cooma'},
    'godavari': {'name': 'Godavari Basin Irrigation Board', 'country': 'base.in', 'city': 'Nashik'},
}

# key: (name, client, country, latitude, longitude, radius km)
AREAS = {
    'hajar_foothills': ('Hajar Foothills', 'wadi_noor', 'base.ae', 24.95, 56.05, 35),
    'gravel_plains': ('Eastern Gravel Plains', 'wadi_noor', 'base.ae', 24.3, 55.6, 40),
    'al_ain_plain': ('Al Ain Plain', 'wadi_noor', 'base.ae', 24.2, 55.75, 30),
    'asir_highlands': ('Asir Highlands', 'sarawat', 'base.sa', 18.2, 42.5, 40),
    'baha_escarpment': ('Baha Escarpment', 'sarawat', 'base.sa', 20.0, 41.45, 35),
    'johor_catchment': ('Johor Catchment', 'tasik_hijau', 'base.my', 1.75, 103.6, 30),
    'kuantan_coast': ('Kuantan Coast', 'tasik_hijau', 'base.my', 3.8, 103.3, 30),
    'pahang_highlands': ('Pahang Highlands', 'tasik_hijau', 'base.my', 4.4, 101.9, 35),
    'sogn_basin': ('Sogn Basin', 'breelv', 'base.no', 61.2, 7.0, 25),
    'hardanger_catchment': ('Hardanger Catchment', 'breelv', 'base.no', 60.4, 6.5, 25),
    'telemark_plateau': ('Telemark Plateau', 'breelv', 'base.no', 59.8, 8.3, 30),
    'upper_eucumbene': ('Upper Eucumbene', 'monaro', 'base.au', -36.1, 148.6, 30),
    'monaro_tablelands': ('Monaro Tablelands', 'monaro', 'base.au', -36.4, 149.0, 35),
    'tumut_valley': ('Tumut Valley', 'monaro', 'base.au', -35.6, 148.3, 25),
    'upper_godavari': ('Upper Godavari', 'godavari', 'base.in', 19.95, 73.8, 40),
    'marathwada_plateau': ('Marathwada Plateau', 'godavari', 'base.in', 19.0, 75.8, 45),
}

# client: (season start, season end in days from today, flight-hour budget,
# standby fee per day, flight-hour rate, base). Two seasons are over.
CONTRACTS = {
    'wadi_noor': (-75, 105, 120, 1200, 2800, 'ridgeview'),
    'sarawat': (-140, 40, 150, 1100, 2700, 'ridgeview'),
    'tasik_hijau': (-120, 60, 160, 900, 2400, 'harbour_point'),
    'breelv': (-100, 80, 140, 1500, 3200, 'fjordline'),
    'monaro': (-148, -62, 80, 1300, 3000, 'snowy_plains'),
    'godavari': (-145, -48, 100, 800, 2200, 'deccan'),
}

BASES = {
    'ridgeview': 'Ridgeview Airfield',
    'harbour_point': 'Harbour Point Airfield',
    'fjordline': 'Fjordline Airstrip',
    'snowy_plains': 'Snowy Plains Airfield',
    'deccan': 'Deccan Airstrip',
}

# key: (name, type, tail, base, rack, service interval h, hourly cost)
AIRCRAFT = {
    'king_air': ('King Air 350', 'crewed', 'A6-SRK', 'ridgeview', 24, 100, 950),
    'cessna': ('Cessna 340', 'crewed', 'A6-SRC', 'ridgeview', 16, 100, 520),
    'drone_2': ('Seeder Drone SD-2', 'drone', 'SD-02', 'ridgeview', 4, 100, 90),
    'cheyenne': ('Piper Cheyenne II', 'crewed', '9M-SRP', 'harbour_point', 20, 50, 680),
    'navajo': ('Piper Navajo', 'crewed', '9M-SRN', 'harbour_point', 14, 100, 560),
    'drone': ('Seeder Drone SD-1', 'drone', 'SD-01', 'harbour_point', 4, 100, 90),
    'king_air_c90': ('King Air C90', 'crewed', 'LN-SRK', 'fjordline', 18, 100, 720),
    'drone_3': ('Seeder Drone SD-3', 'drone', 'SD-03', 'fjordline', 4, 100, 90),
    'cessna_421': ('Cessna 421', 'crewed', 'VH-SRC', 'snowy_plains', 16, 100, 600),
    'seneca': ('Piper Seneca V', 'crewed', 'VT-SRS', 'deccan', 12, 100, 480),
}
# The Cheyenne, Harbour Point's largest aircraft (Malaysia: seedable nearly
# every day), ends its history just under its service interval, so one more
# flight takes it past it.
RESERVED_AIRCRAFT = 'cheyenne'
RESERVED_HOURS = 49.6

# key: (name, role, licence no., licence expiry in days, medical expiry in
# days, hourly cost, base)
PILOTS = {
    'haddad': ('Leila Haddad', 'pilot', 'CPL-41207', 410, 160, 140, 'ridgeview'),
    'menon': ('Ravi Menon', 'pilot', 'CPL-38815', 290, 120, 130, 'ridgeview'),
    'saleh': ('Omar Saleh', 'pilot', 'CPL-47702', 470, 24, 135, 'ridgeview'),
    'aziz': ('Karim Aziz', 'drone_operator', 'RPL-08841', 380, None, 60, 'ridgeview'),
    'okoro': ('Daniel Okoro', 'pilot', 'CPL-45530', 520, 210, 135, 'harbour_point'),
    'rahman': ('Siti Rahman', 'pilot', 'CPL-50318', 610, 240, 130, 'harbour_point'),
    'tan': ('Aiko Tan', 'drone_operator', 'RPL-07712', 600, None, 60, 'harbour_point'),
    'solberg': ('Ingrid Solberg', 'pilot', 'CPL-29944', 350, -9, 150, 'fjordline'),
    'dahl': ('Erik Dahl', 'pilot', 'CPL-33107', 19, 300, 150, 'fjordline'),
    'berg': ('Nora Berg', 'drone_operator', 'RPL-06215', 440, None, 65, 'fjordline'),
    'hughes': ('Liam Hughes', 'pilot', 'CPL-27760', 330, 190, 145, 'snowy_plains'),
    'parker': ('Grace Parker', 'pilot', 'CPL-31954', 400, 230, 145, 'snowy_plains'),
    'nair': ('Priya Nair', 'pilot', 'CPL-44183', 500, 170, 125, 'deccan'),
    'desai': ('Arjun Desai', 'pilot', 'CPL-39026', 280, 140, 125, 'deccan'),
}
WEEKLY_DUTY_LIMIT = 30

# The first flare is the default the planners load. Glaciogenic flares seed
# the cold clouds of Norway and the Snowy Mountains.
FLARES = {
    'hf': {'name': 'HF-1000 Hygroscopic Flare', 'code': 'HF-1000', 'grams': 1000, 'cost': 85.0,
           'vendor': 'Altostratus Pyrotechnics', 'lead_days': 4},
    'si': {'name': 'SI-150 Glaciogenic Flare', 'code': 'SI-150', 'grams': 150, 'cost': 62.0,
           'vendor': 'Nimbus Glaciogenics', 'lead_days': 6},
}
BASE_FLARE = {'ridgeview': 'hf', 'harbour_point': 'hf', 'deccan': 'hf', 'fjordline': 'si', 'snowy_plains': 'si'}
# (base, flare): (minimum, maximum) of the automatic reorder rule. Each base is
# first stocked up to its maximum.
REORDER = {
    ('ridgeview', 'hf'): (120, 360),
    ('harbour_point', 'hf'): (120, 360),
    ('deccan', 'hf'): (60, 240),
    ('fjordline', 'si'): (120, 400),
    ('fjordline', 'hf'): (20, 60),
    ('snowy_plains', 'si'): (100, 300),
}

# (aircraft, opened days ago, days out of service or None while still open, fault)
BREAKDOWNS = [
    ('cheyenne', 118, 3, "Left engine magneto drop on run-up"),
    ('cessna', 96, 2, "Flare rack firing circuit intermittent"),
    ('seneca', 88, 3, "Nose gear shimmy on landing roll"),
    ('king_air_c90', 71, 4, "De-ice boot leak, left wing"),
    ('drone', 64, 1, "GPS module replaced after compass error"),
    ('navajo', 40, 2, "Cracked exhaust stack, right engine"),
    ('drone_3', 22, 2, "Battery pack failed capacity test"),
    ('cessna_421', 6, None, "Propeller governor fault, right engine"),
]

# Days before the history starts: the last service of every aircraft, and the
# first stocking of every base.
FIRST_SERVICE_DAYS_BEFORE = 3
FIRST_STOCK_ORDERED_DAYS_BEFORE = 14

# Purchases proposed by a reorder rule within this time of loading are left
# as requests for quotation, awaiting confirmation.
RFQ_AWAITING = timedelta(days=1)

# Today's missions: (state, minutes from now to the planned takeoff; the
# launched one took off that long ago).
PRESENT_MISSIONS = [('launched', -40), ('ready', 90), ('proposed', 180)]
# A proposed mission blocked by its pilot's expired medical: (client, aircraft, pilot).
BLOCKED_MISSION = ('breelv', 'king_air_c90', 'solberg')

# Observed rainfall is recorded at loading for missions that landed at least
# this many days ago. Younger missions keep it for "Fetch Observed Rainfall".
RAINFALL_RECORDED_AFTER_DAYS = 14
# Vendor bills are paid this many days after they are received; clients pay
# their invoices within this range of days.
VENDOR_PAYMENT_DAYS = 30
CLIENT_PAYMENT_DAYS = (18, 35)

# Without the forecast archive: past missions (days ago, takeoff hour UTC,
# area, aircraft, pilot, minutes flown, flares planned, flares fired, planned
# by), with the recorded snapshots. Every aircraft's last service was
# FALLBACK_LAST_SERVICE_DAYS_AGO days ago. Crew fly their own base's aircraft,
# to the areas their base serves.
FALLBACK_LAST_SERVICE_DAYS_AGO = 50
FALLBACK_AREAS = ('hajar_foothills', 'gravel_plains', 'johor_catchment', 'kuantan_coast',
                  'sogn_basin', 'hardanger_catchment')
FALLBACK_MISSIONS = [
    (44, 10, 'hajar_foothills', 'king_air', 'haddad', 225, 24, 22, 'human'),
    (42, 13, 'kuantan_coast', 'cheyenne', 'okoro', 150, 20, 18, 'human'),
    (41, 9, 'sogn_basin', 'king_air_c90', 'solberg', 230, 18, 16, 'rules'),
    (39, 11, 'gravel_plains', 'cessna', 'haddad', 140, 16, 14, 'human'),
    (37, 6, 'johor_catchment', 'drone', 'tan', 55, 4, 4, 'rules'),
    (36, 12, 'hajar_foothills', 'king_air', 'menon', 235, 24, 24, 'human'),
    (34, 8, 'hardanger_catchment', 'king_air_c90', 'dahl', 220, 18, 15, 'human'),
    (32, 14, 'kuantan_coast', 'drone', 'tan', 50, 4, 3, 'human'),
    (31, 10, 'gravel_plains', 'king_air', 'haddad', 215, 24, 21, 'rules'),
    (29, 7, 'sogn_basin', 'drone_3', 'berg', 70, 4, 4, 'human'),
    (27, 12, 'johor_catchment', 'cheyenne', 'okoro', 175, 20, 20, 'rules'),
    (26, 9, 'hajar_foothills', 'king_air', 'haddad', 230, 24, 23, 'human'),
    (24, 13, 'hardanger_catchment', 'king_air_c90', 'solberg', 225, 18, 15, 'rules'),
    (22, 6, 'kuantan_coast', 'drone', 'tan', 60, 4, 4, 'human'),
    (20, 11, 'gravel_plains', 'king_air', 'saleh', 220, 24, 20, 'human'),
    (18, 8, 'sogn_basin', 'king_air_c90', 'solberg', 240, 18, 18, 'rules'),
    (16, 12, 'johor_catchment', 'cheyenne', 'okoro', 160, 20, 17, 'human'),
    (14, 10, 'hajar_foothills', 'cessna', 'menon', 150, 16, 15, 'human'),
    (12, 9, 'hardanger_catchment', 'king_air_c90', 'solberg', 225, 18, 17, 'human'),
    (11, 7, 'kuantan_coast', 'drone', 'tan', 45, 4, 4, 'rules'),
    (9, 13, 'gravel_plains', 'king_air', 'haddad', 220, 24, 21, 'human'),
    (7, 10, 'sogn_basin', 'king_air_c90', 'dahl', 155, 18, 14, 'rules'),
    (5, 11, 'hajar_foothills', 'king_air', 'menon', 215, 24, 22, 'human'),
    (4, 6, 'johor_catchment', 'cheyenne', 'okoro', 170, 20, 19, 'human'),
    (2, 12, 'hardanger_catchment', 'king_air_c90', 'dahl', 200, 18, 16, 'rules'),
]

UNAVAILABLE = {
    'service': "in its scheduled service",
    'maintenance': "under maintenance",
    'busy': "on another mission",
    'reserved': "held back: its scheduled service is due soon",
    'no crew': "no crew member free and cleared to fly it",
}


class SeedingDemoWorld(models.TransientModel):
    _name = 'seeding.demo.world'
    _description = 'Serein Demo World Loader'

    blocking_reason = fields.Text(compute='_compute_blocking_reason')

    def _compute_blocking_reason(self):
        reason = self._blocking_reason()
        for wizard in self:
            wizard.blocking_reason = reason

    @api.model
    def _blocking_reason(self):
        """The demo world goes into an empty Serein database only."""
        env = self.sudo().with_context(active_test=False).env
        found = []
        for model, domain, one, many in (
            ('seeding.target.area', [], _("target area"), _("target areas")),
            ('seeding.contract', [], _("contract"), _("contracts")),
            ('seeding.base', [], _("base"), _("bases")),
            ('seeding.mission', [], _("mission"), _("missions")),
            ('maintenance.equipment', [('is_aircraft', '=', True)], _("aircraft"), _("aircraft")),
            ('hr.employee', [('is_pilot', '=', True)], _("pilot"), _("pilots")),
            ('product.template', [('is_seeding_flare', '=', True)], _("flare product"), _("flare products")),
        ):
            count = env[model].search_count(domain)
            if count:
                found.append('%s %s' % (count, one if count == 1 else many))
        if not found:
            return False
        return _(
            "This database already holds Serein records (%s). The demo world is loaded into a "
            "new database with Serein installed and nothing entered yet.", ', '.join(found),
        )

    def action_load(self):
        counts = self.load()
        message = _(
            "%(clients)s clients, %(areas)s target areas, %(aircraft)s aircraft, %(pilots)s crew, "
            "%(missions)s past missions (%(rainfall)s with observed rainfall), %(alerts)s cloud "
            "alerts (%(live_alerts)s live today), %(current)s missions under way today, "
            "%(purchases)s purchase orders, %(invoices)s client invoices.",
            **counts,
        )
        if counts['history'] == 'fallback':
            message += ' ' + _(
                "The forecast archive could not be reached: the history was built from the "
                "recorded snapshots, without cloud alerts.",
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Demo world loaded"),
                'message': message,
                'type': 'success' if counts['history'] == 'archive' else 'warning',
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }

    # Loading -----------------------------------------------------------------

    @api.model
    def load(self):
        """Build the whole demo world. Returns counts of what was created."""
        if not self.env.user.has_group('serein_ops.group_serein_manager'):
            raise UserError(_("Only a Serein manager can load the demo world."))
        reason = self._blocking_reason()
        if reason:
            raise UserError(reason)
        # Superuser rights for stock, maintenance, employees, purchasing, and
        # accounting; records keep the loading user as their author.
        return _WorldBuilder(self.sudo().env).build()


class _WorldBuilder:
    """Builds the demo world and keeps the state of one load."""

    def __init__(self, env):
        self.env = env
        self._now = fields.Datetime.now().replace(minute=0, second=0, microsecond=0)
        self._today = fields.Date.context_today(env.user)
        self._history_start = self._now - timedelta(days=HISTORY_DAYS)
        self._receipts = []
        self._lots = {}
        self._histories = {}

    def build(self):
        self._setup_company(self.env.company)
        self._clients = self._create_clients()
        self._areas = self._create_areas()
        self._contracts = self._create_contracts()
        self._bases = self._create_bases()
        self._flares = self._create_flares()
        self._stock_bases()
        self._pilots = self._create_pilots()

        histories = self._fetch_histories()
        if histories:
            self._aircraft = self._create_aircraft(
                self._history_start - timedelta(days=FIRST_SERVICE_DAYS_BEFORE))
            events = self._plan(histories)
        else:
            self._aircraft = self._create_aircraft(
                self._now - timedelta(days=FALLBACK_LAST_SERVICE_DAYS_AGO))
            self._create_recorded_snapshots()
            events = self._fallback_events()
        self._histories = histories or {}
        self._play(events)
        self._receive_due(self._now)
        self.env['seeding.cloud.alert']._cron_expire_alerts()

        missions = self.env['seeding.mission'].search([('state', '=', 'completed')], order='takeoff_at')
        rainfall = self._record_rainfall(missions)
        writer = self.env['seeding.mission.writer']
        for mission in missions:
            writer.write_summary_from_record(mission)
        invoices = self._invoice_seasons()
        self._close_finished_contracts()
        self._register_payments(invoices)
        self.env['maintenance.equipment'].browse([a.id for a in self._aircraft.values()])._check_service_due()
        present = self._present_day()

        flown = self.env['seeding.mission'].search([('state', 'in', ('completed', 'billed'))])
        _logger.info("Serein: demo world loaded (%s missions, history from the %s).", len(flown),
                     'forecast archive' if histories else 'recorded snapshots')
        return {
            'clients': len(self._clients), 'areas': len(self._areas), 'aircraft': len(self._aircraft),
            'pilots': len(self._pilots), 'missions': len(flown),
            'cancelled': self.env['seeding.mission'].search_count([('state', '=', 'cancelled')]),
            'rainfall': rainfall,
            'alerts': self.env['seeding.cloud.alert'].search_count([]),
            'snapshots': self.env['seeding.weather.snapshot'].search_count([]),
            'purchases': self.env['purchase.order'].search_count([('state', '=', 'purchase')]),
            'invoices': len(invoices),
            'current': len(present),
            'live_alerts': self.env['seeding.cloud.alert'].search_count([('source', '=', 'live')]),
            'history': 'archive' if histories else 'fallback',
        }

    # Master data ---------------------------------------------------------------

    def _setup_company(self, company):
        layout = self.env.ref('web.external_layout_standard', raise_if_not_found=False)
        vals = {'name': COMPANY_NAME}
        if layout and not company.external_report_layout_id:
            vals['external_report_layout_id'] = layout.id
        company.write(vals)
        # On a new database Odoo loads the chart of accounts after every
        # module, so the service products were created without the company's
        # sale tax. Give it to them, as product creation would have.
        tax = company.account_sale_tax_id
        if tax:
            for xmlid in ('serein_ops.product_seeding_standby_day', 'serein_ops.product_seeding_flight_hour'):
                product = self.env.ref(xmlid).product_tmpl_id
                if not product.taxes_id.filtered(lambda t: t.company_id == company):
                    product.taxes_id = [(4, tax.id)]

    def _create_clients(self):
        return {
            key: self.env['res.partner'].create({
                'name': data['name'], 'is_company': True, 'city': data['city'],
                'country_id': self.env.ref(data['country']).id,
            })
            for key, data in CLIENTS.items()
        }

    def _create_areas(self):
        return {
            key: self.env['seeding.target.area'].create({
                'name': name, 'partner_id': self._clients[client].id,
                'country_id': self.env.ref(country).id,
                'latitude': lat, 'longitude': lon, 'radius_km': radius,
            })
            for key, (name, client, country, lat, lon, radius) in AREAS.items()
        }

    def _create_contracts(self):
        contracts = {}
        for key, (start, end, budget, standby, rate, _base) in CONTRACTS.items():
            contract = self.env['seeding.contract'].create({
                'partner_id': self._clients[key].id,
                'area_ids': [(6, 0, [a.id for k, a in self._areas.items() if AREAS[k][1] == key])],
                'season_start': self._today + timedelta(days=start),
                'season_end': self._today + timedelta(days=end),
                'flight_hour_budget': budget,
                'standby_fee': standby,
                'flight_hour_rate': rate,
            })
            contract.action_activate()
            contracts[key] = contract
        return contracts

    def _create_bases(self):
        """Each base serves the target areas of the clients it flies for."""
        bases = {}
        for key, name in BASES.items():
            served = [area.id for k, area in self._areas.items() if CONTRACTS[AREAS[k][1]][5] == key]
            bases[key] = self.env['seeding.base'].create({'name': name, 'area_ids': [(6, 0, served)]})
        return bases

    def _create_flares(self):
        flares = {}
        for key, data in FLARES.items():
            vendor = self.env['res.partner'].create({'name': data['vendor'], 'is_company': True})
            template = self.env['product.template'].create({
                'name': data['name'], 'default_code': data['code'],
                'type': 'consu', 'is_storable': True, 'tracking': 'lot',
                'is_seeding_flare': True, 'grams_per_flare': data['grams'],
                'purchase_ok': True, 'sale_ok': False, 'standard_price': data['cost'],
                'seller_ids': [(0, 0, {
                    'partner_id': vendor.id, 'price': data['cost'], 'delay': data['lead_days'],
                })],
            })
            flares[key] = template.product_variant_id
        return flares

    def _stock_bases(self):
        """An automatic reorder rule per base and flare, and a first purchase
        that fills each base up to the rule's maximum before the history."""
        orderpoints = self.env['stock.warehouse.orderpoint']
        for (base, flare), (minimum, maximum) in REORDER.items():
            location = self._bases[base].location_id
            orderpoints |= orderpoints.create({
                'product_id': self._flares[flare].id, 'location_id': location.id,
                'warehouse_id': location.warehouse_id.id,
                'product_min_qty': minimum, 'product_max_qty': maximum, 'trigger': 'auto',
            })
        orderpoints.action_replenish(force_to_max=True)
        self._confirm_purchases(self._history_start - timedelta(days=FIRST_STOCK_ORDERED_DAYS_BEFORE))
        self._receive_due(self._history_start)

    def _create_aircraft(self, serviced_at):
        """Aircraft, each with its last scheduled service closed at `serviced_at`."""
        aircraft = {}
        for key, (name, kind, tail, base, rack, interval, cost) in AIRCRAFT.items():
            aircraft[key] = self.env['maintenance.equipment'].create({
                'name': name, 'is_aircraft': True, 'aircraft_type': kind, 'tail_number': tail,
                'home_base_id': self._bases[base].id, 'rack_capacity': rack,
                'service_interval_hours': interval, 'aircraft_hourly_cost': cost,
            })
            self._open_service(aircraft[key], serviced_at - timedelta(hours=6))
            self._close_service(aircraft[key], serviced_at)
        return aircraft

    def _create_pilots(self):
        pilots = {}
        for key, (name, role, licence, licence_days, medical_days, cost, base) in PILOTS.items():
            pilots[key] = self.env['hr.employee'].create({
                'name': name, 'is_pilot': True, 'pilot_role': role,
                'home_base_id': self._bases[base].id,
                'pilot_licence_number': licence,
                'licence_expiry': self._today + timedelta(days=licence_days),
                'medical_expiry': self._today + timedelta(days=medical_days) if medical_days is not None else False,
                'weekly_duty_limit': WEEKLY_DUTY_LIMIT, 'pilot_hourly_cost': cost,
            })
        return pilots

    # History -------------------------------------------------------------------

    def _fetch_histories(self):
        """Past forecasts for every area, or None if any area cannot be fetched."""
        service = self.env['seeding.weather.service']
        start = self._history_start.date()
        end = self._today - timedelta(days=1)
        histories = {}
        for key, area in self._areas.items():
            try:
                histories[key] = service.fetch_history(area, start, end)
            except UserError as error:
                _logger.warning("Serein: demo world history not fetched for %s: %s", area.name, error)
                return None
        return histories

    def _plan(self, histories):
        areas = {}
        for key, data in histories.items():
            times, scores = hourly_scores(data)
            client = AREAS[key][1]
            areas[key] = PlanArea(key, client, CONTRACTS[client][5], AREAS[key][4], times, scores)
        contracts = {
            key: PlanContract(key, contract.season_start, contract.season_end, contract.flight_hour_budget)
            for key, contract in self._contracts.items()
        }
        aircraft = {
            key: PlanAircraft(key, data[3], data[1], data[4], data[5]) for key, data in AIRCRAFT.items()
        }
        pilots = {
            key: PlanPilot(key, data[6], data[1], self._pilots[key].licence_expiry,
                           self._pilots[key].medical_expiry or None, WEEKLY_DUTY_LIMIT)
            for key, data in PILOTS.items()
        }
        breakdowns = [
            Breakdown(plane, self._now - timedelta(days=days_ago),
                      self._now - timedelta(days=days_ago - down) if down is not None else None, fault)
            for plane, days_ago, down, fault in BREAKDOWNS
        ]
        threshold = self.env['seeding.weather.snapshot']._threshold()
        events, _planner = plan_history(
            areas, contracts, aircraft, pilots, breakdowns, threshold, self._now,
            reserved=RESERVED_AIRCRAFT, reserve_limit=RESERVED_HOURS,
        )
        return events

    def _fallback_events(self):
        events = []
        for plane, days_ago, down, fault in BREAKDOWNS:
            opened = self._now - timedelta(days=days_ago)
            events.append(Event(opened, 'breakdown_open', {'aircraft': plane, 'name': fault}))
            if down is not None:
                events.append(Event(opened + timedelta(days=down), 'breakdown_close',
                                    {'aircraft': plane, 'name': fault}))
        for days_ago, hour, area, plane, pilot, minutes, planned, fired, planned_by in FALLBACK_MISSIONS:
            takeoff = datetime.combine(self._today - timedelta(days=days_ago), time(hour, 0))
            events.append(Event(takeoff - CHECKS_BEFORE_TAKEOFF, 'alert', {
                'area': area, 'outcome': 'flown', 'mission': {
                    'aircraft': plane, 'pilot': pilot, 'takeoff': takeoff,
                    'landing': takeoff + timedelta(minutes=minutes), 'planned': planned,
                    'short': planned - fired, 'planned_by': planned_by,
                },
            }))
        return sorted(events, key=Event.sort_key)

    def _create_recorded_snapshots(self):
        """The snapshots recorded for the fallback areas (data/demo_world)."""
        service = self.env['seeding.weather.service']
        for key in FALLBACK_AREAS:
            with file_open(WEATHER_FILE % key, 'r') as handle:
                recorded = json.load(handle)
            fetched_at = datetime.strptime(recorded['fetched_at'], '%Y-%m-%d %H:%M:%S')
            data = recorded['response']
            vals = service._parse(data, fetched_at.replace(minute=0, second=0))
            vals.update({
                'area_id': self._areas[key].id, 'fetched_at': fetched_at, 'raw_response': json.dumps(data),
            })
            self.env['seeding.weather.snapshot'].create(vals)

    # Playing the history -------------------------------------------------------

    def _play(self, events):
        handlers = {
            'alert': self._play_alert,
            'service_close': lambda event: self._close_service(self._aircraft[event.data['aircraft']], event.time),
            'breakdown_open': self._open_breakdown,
            'breakdown_close': self._close_breakdown,
        }
        for event in events:
            self._receive_due(event.time)
            handlers[event.kind](event)

    def _play_alert(self, event):
        data = event.data
        area = self._areas[data['area']]
        alert = self.env['seeding.cloud.alert']
        if 'start' in data:
            alert = self._raise_alert(area, data['start'])
        outcome = data['outcome']
        if outcome == 'dismissed':
            self._dismiss(alert, self._unavailable_note(area, data['reason']))
        elif outcome in ('flown', 'cancelled'):
            mission = self._create_mission(area, alert, data['mission'])
            if not mission:
                self._dismiss(alert, _(
                    "Not flown: %s holds no flares for this mission.",
                    self._bases[CONTRACTS[AREAS[data['area']][1]][5]].name,
                ))
            elif outcome == 'cancelled':
                mission.action_cancel()
                mission.message_post(body=data['reason'])
            else:
                self._fly(mission, data['mission'])

    def _raise_alert(self, area, start):
        """The archived forecast for `start` as a snapshot, and its alert."""
        sliced = hourly_slice(self._history_of(area), start, 13)
        vals = self.env['seeding.weather.service']._parse(sliced, start)
        vals.update({
            'area_id': area.id, 'fetched_at': self._now, 'raw_response': json.dumps(sliced),
            'data_source': 'archive',
        })
        snapshot = self.env['seeding.weather.snapshot'].create(vals)
        alert = snapshot._raise_archive_alert()
        if not alert:
            raise UserError(_("The archived forecast for %(area)s at %(time)s did not raise an alert.",
                              area=area.name, time=start))
        return alert

    def _history_of(self, area):
        key = next(k for k, a in self._areas.items() if a == area)
        return self._histories[key]

    def _dismiss(self, alert, note):
        if not alert:
            return
        alert.note = note
        alert.action_dismiss()
        alert.message_post(body=note)

    def _unavailable_note(self, area, reasons):
        client = AREAS[next(k for k, a in self._areas.items() if a == area)][1]
        base = self._bases[CONTRACTS[client][5]]
        if reasons[0][1] == 'on pace':
            return _("Not flown: contract %s is on its planned pace of flight hours for the season.",
                     self._contracts[client].name)
        if len(reasons) == 1 and reasons[0][1] == 'no crew':
            return _("Not flown: no crew member at %(base)s was free and cleared to fly %(aircraft)s.",
                     base=base.name, aircraft=self._aircraft[reasons[0][0]].name)
        return _(
            "Not flown: no aircraft at %(base)s was available (%(details)s).",
            base=base.name,
            details='; '.join('%s %s' % (self._aircraft[key].name, UNAVAILABLE[why]) for key, why in reasons),
        )

    def _create_mission(self, area, alert, spec):
        plane = self._aircraft[spec['aircraft']]
        base = plane.home_base_id
        flare = self._flares[BASE_FLARE[next(k for k, b in self._bases.items() if b == base)]]
        on_hand = flare.with_context(location=base.location_id.id).qty_available
        planned = min(spec['planned'], int(on_hand))
        if planned < 1:
            return None
        takeoff = spec['takeoff']
        vals = {
            'area_id': area.id,
            'aircraft_id': plane.id,
            'pilot_id': self._pilots[spec['pilot']].id,
            'flare_product_id': flare.id,
            'flares_planned': planned,
            'planned_takeoff': alert.window_start if alert else takeoff,
            'launch_by': alert.window_end if alert else takeoff + timedelta(hours=1),
            'planned_by': spec['planned_by'],
        }
        if alert:
            vals['alert_id'] = alert.id
        if spec['planned_by'] == 'rules':
            vals['plan_reasoning'] = _(
                "Rule-based plan: %s All applicable readiness checks passed.",
                self.env['seeding.mission.planner']._rule_summary(plane, self._pilots[spec['pilot']], planned),
            )
        Mission = self.env['seeding.mission'].with_context(readiness_at=takeoff - CHECKS_BEFORE_TAKEOFF)
        mission = Mission.create(vals)
        if mission.readiness_state != 'pass':
            failed = mission.readiness_ids.filtered(lambda c: c.result == 'fail')
            raise UserError(_(
                "Demo world mission on %(day)s did not pass its readiness checks: %(checks)s",
                day=takeoff, checks='; '.join('%s: %s' % (c.name, c.explanation) for c in failed),
            ))
        return mission.with_context(readiness_at=None)

    def _fly(self, mission, spec):
        """Ready, launched, completed: the flares leave the base by batch on
        landing, and the aircraft's service interval is checked."""
        takeoff, landing = spec['takeoff'], spec['landing']
        mission.write({'state': 'ready'})
        mission.write({'state': 'launched', 'takeoff_at': takeoff})
        mission.write({'flares_fired': max(1, mission.flares_planned - spec['short']), 'landing_at': landing})
        mission.write({'state': 'completed'})
        picking = mission._release_fired_flares()
        picking.write({'date_done': landing})
        picking.move_ids.write({'date': landing})
        picking.move_ids.move_line_ids.write({'date': landing})
        plane = mission.aircraft_id
        before = plane.maintenance_ids
        plane._check_service_due()
        opened = plane.maintenance_ids - before
        opened.write({'request_date': landing.date(), 'schedule_date': landing})
        # The release may have proposed a purchase: it is ordered that afternoon.
        self._confirm_purchases(landing + timedelta(hours=2))

    # Maintenance ---------------------------------------------------------------

    def _open_service(self, plane, at):
        Request = self.env['maintenance.request']
        if plane._open_service_requests():
            return
        Request.create({
            'name': _("Scheduled service: %s", plane.name),
            'equipment_id': plane.id,
            'maintenance_type': 'preventive',
            'is_scheduled_service': True,
            'maintenance_team_id': plane.maintenance_team_id.id or Request._get_default_team_id(),
            'request_date': at.date(),
            'schedule_date': at,
        })

    def _close_service(self, plane, at):
        done = self.env['maintenance.stage'].search([('done', '=', True)], order='sequence', limit=1)
        for request in plane._open_service_requests():
            request.write({'stage_id': done.id})
            request.write({'service_completed_at': at, 'close_date': at.date()})

    def _open_breakdown(self, event):
        plane = self._aircraft[event.data['aircraft']]
        Request = self.env['maintenance.request']
        Request.create({
            'name': event.data['name'],
            'equipment_id': plane.id,
            'maintenance_type': 'corrective',
            'maintenance_team_id': plane.maintenance_team_id.id or Request._get_default_team_id(),
            'request_date': event.time.date(),
            'schedule_date': event.time,
        })

    def _close_breakdown(self, event):
        plane = self._aircraft[event.data['aircraft']]
        done = self.env['maintenance.stage'].search([('done', '=', True)], order='sequence', limit=1)
        request = plane._open_corrective_requests().filtered(lambda r: r.name == event.data['name'])
        request.write({'stage_id': done.id})
        request.write({'close_date': event.time.date()})

    # Purchasing ----------------------------------------------------------------

    def _confirm_purchases(self, at):
        """Confirm every proposed purchase, as ordered at `at`. Each is received
        after its vendor's lead time. Purchases proposed in the last
        RFQ_AWAITING stay requests for quotation."""
        if at > self._now - RFQ_AWAITING:
            return
        for order in self.env['purchase.order'].search([('state', '=', 'draft')]):
            order.button_confirm()
            order.write({'date_order': at, 'date_approve': at})
            lead = max(order.order_line.mapped(
                lambda line: line.product_id.seller_ids[:1].delay or 0)) or 1
            self._receipts.append((at + timedelta(days=lead), order.id))

    def _receive_due(self, until):
        due = sorted(item for item in self._receipts if item[0] <= until)
        self._receipts = [item for item in self._receipts if item[0] > until]
        for at, order_id in due:
            self._receive(self.env['purchase.order'].browse(order_id), at)

    def _receive(self, order, at):
        """Receive the order by batch at `at`, and post the vendor's bill."""
        for picking in order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
            for move in picking.move_ids:
                lot_name = self._next_lot(move.product_id, at)
                lines = move.move_line_ids
                lines[1:].unlink()
                lines[:1].write({'lot_name': lot_name, 'quantity': move.product_uom_qty})
            picking.move_ids.picked = True
            picking.button_validate()
            picking.write({'date_done': at})
            picking.move_ids.write({'date': at})
            picking.move_ids.move_line_ids.write({'date': at})
            lots = picking.move_ids.move_line_ids.lot_id
            self.env['stock.quant'].search([('lot_id', 'in', lots.ids)]).write({'in_date': at})
        order.action_create_invoice()
        bills = order.invoice_ids.filtered(lambda m: m.state == 'draft')
        bills.write({'invoice_date': at.date(), 'ref': order.name})
        bills.action_post()

    def _next_lot(self, product, at):
        prefix = '%s-%s' % (product.default_code.split('-')[0], at.strftime('%y%m'))
        count = self._lots.get(prefix, 0)
        self._lots[prefix] = count + 1
        return '%s-%s' % (prefix, chr(ord('A') + count))

    # After the history -----------------------------------------------------------

    def _record_rainfall(self, missions):
        """Observed rainfall for missions that landed at least
        RAINFALL_RECORDED_AFTER_DAYS ago: one archive call per area. If the
        archive cannot be reached, the missions keep it for later."""
        cutoff = self._now - timedelta(days=RAINFALL_RECORDED_AFTER_DAYS)
        old = missions.filtered(lambda m: m.landing_at <= cutoff)
        recorded = 0
        service = self.env['seeding.weather.service']
        for area in old.area_id:
            area_missions = old.filtered(lambda m: m.area_id == area)
            periods = [m._rainfall_period() for m in area_missions]
            try:
                with self.env.cr.savepoint():
                    data = service.fetch_archive(area, min(p[0] for p in periods), max(p[1] for p in periods))
                    for mission in area_missions:
                        mission._record_rainfall(data)
                recorded += len(area_missions)
            except UserError as error:
                _logger.warning("Serein: demo world rainfall not recorded for %s: %s", area.name, error)
        return recorded

    def _invoice_seasons(self):
        """Invoice every past calendar month of each season. The most recent
        month of a running season stays a draft; the rest are posted. The
        current month is left to invoice."""
        invoices = self.env['account.move']
        month_start = self._today.replace(day=1)
        for contract in self._contracts.values():
            months = []
            first = contract.season_start.replace(day=1)
            while first < month_start and first <= contract.season_end:
                last = min((first + timedelta(days=32)).replace(day=1) - timedelta(days=1), contract.season_end)
                months.append((max(first, contract.season_start), last))
                first = (first + timedelta(days=32)).replace(day=1)
            running = contract.season_end >= self._today
            for index, (date_from, date_to) in enumerate(months):
                post = not running or index < len(months) - 1
                invoice = contract._create_invoice(
                    date_from, date_to, invoice_date=date_to + timedelta(days=1) if post else None,
                )
                if not invoice:
                    continue
                invoices |= invoice
                if post:
                    invoice.action_post()
        return invoices

    def _close_finished_contracts(self):
        for contract in self._contracts.values():
            if contract.season_end < self._today:
                contract.action_close()

    def _register_payments(self, invoices):
        """Clients pay their posted invoices within CLIENT_PAYMENT_DAYS; bills
        for flares are paid VENDOR_PAYMENT_DAYS after receipt."""
        to_pay = []
        for invoice in invoices.filtered(lambda m: m.state == 'posted'):
            rng = random.Random(invoice.name)
            to_pay.append((invoice, invoice.invoice_date + timedelta(days=rng.randint(*CLIENT_PAYMENT_DAYS))))
        bills = self.env['account.move'].search([('move_type', '=', 'in_invoice'), ('state', '=', 'posted')])
        for bill in bills:
            to_pay.append((bill, bill.invoice_date + timedelta(days=VENDOR_PAYMENT_DAYS)))
        for move, paid_on in to_pay:
            if paid_on > self._today:
                continue
            self.env['account.payment.register'].with_context(
                active_model='account.move', active_ids=move.ids,
            ).create({'payment_date': paid_on})._create_payments()

    # The present day -------------------------------------------------------------

    def _present_day(self):
        """Today's operations: a live weather fetch for every area (real alerts
        where the weather is seedable now), and missions in each open state:
        one airborne, one ready with its briefing, one proposed and passing its
        checks, and one proposed and blocked. Returns the missions."""
        self.env['seeding.target.area'].search([])._fetch_weather_safe()
        now = fields.Datetime.now().replace(second=0, microsecond=0)
        running = self.env['seeding.contract'].search([('state', '=', 'active')])
        alerts = self.env['seeding.cloud.alert'].search(
            [('state', '=', 'new'), ('area_id', 'in', running.area_ids.ids)], order='score desc')
        # The reserved aircraft stays free for planning; the blocked mission's
        # aircraft is kept for it.
        used = {
            'areas': set(), 'pilots': set(),
            'aircraft': {self._aircraft[RESERVED_AIRCRAFT].id, self._aircraft[BLOCKED_MISSION[1]].id},
        }
        missions = self.env['seeding.mission']
        for state, minutes in PRESENT_MISSIONS:
            missions |= self._present_mission(state, now + timedelta(minutes=minutes), running, alerts, used)
        missions |= self._blocked_mission(now, alerts, used)
        return missions

    def _present_mission(self, state, takeoff, running, alerts, used):
        """The first area, aircraft, and crew member that pass every check:
        areas with a live alert first, then the other areas of running seasons."""
        Mission = self.env['seeding.mission']
        no_alert = self.env['seeding.cloud.alert']
        candidates = [(alert.area_id, alert) for alert in alerts]
        candidates += [(area, no_alert) for area in running.area_ids.sorted('name')]
        for area, alert in candidates:
            if area.id in used['areas']:
                continue
            for base in area.base_ids:
                planes = base.aircraft_ids.filtered(lambda p: p.id not in used['aircraft'])
                for plane in planes.sorted(lambda p: (-p.rack_capacity, p.name)):
                    role = 'drone_operator' if plane.aircraft_type == 'drone' else 'pilot'
                    crew = self.env['hr.employee'].search([
                        ('is_pilot', '=', True), ('home_base_id', '=', base.id), ('pilot_role', '=', role),
                        ('id', 'not in', list(used['pilots'])),
                    ])
                    for pilot in crew.sorted(lambda p: (p.duty_hours_7d, p.name)):
                        vals = self._present_values(area, alert, plane, pilot, takeoff)
                        if vals and not Mission._evaluate_plan(vals):
                            used['areas'].add(area.id)
                            used['aircraft'].add(plane.id)
                            used['pilots'].add(pilot.id)
                            return self._start_mission(vals, state, takeoff)
        return Mission

    def _present_values(self, area, alert, plane, pilot, takeoff):
        base = plane.home_base_id
        flare = self._flares[BASE_FLARE[next(k for k, b in self._bases.items() if b == base)]]
        planned = min(plane.rack_capacity, int(flare.with_context(location=base.location_id.id).qty_available))
        if planned < 1:
            return None
        vals = {
            'area_id': area.id, 'aircraft_id': plane.id, 'base_id': base.id, 'pilot_id': pilot.id,
            'flare_product_id': flare.id, 'flares_planned': planned, 'planned_takeoff': takeoff,
            'launch_by': alert.window_end if alert else takeoff + timedelta(hours=2),
        }
        if alert:
            vals['alert_id'] = alert.id
        return vals

    def _start_mission(self, vals, state, takeoff):
        writer = self.env['seeding.mission.writer']
        if state == 'proposed':
            plane = self.env['maintenance.equipment'].browse(vals['aircraft_id'])
            pilot = self.env['hr.employee'].browse(vals['pilot_id'])
            vals = dict(vals, planned_by='rules', plan_reasoning=_(
                "Rule-based plan: %s All applicable readiness checks passed.",
                self.env['seeding.mission.planner']._rule_summary(plane, pilot, vals['flares_planned']),
            ))
        mission = self.env['seeding.mission'].create(vals)
        if state in ('ready', 'launched'):
            mission.write({'state': 'ready'})
            writer.write_briefing_from_record(mission)
        if state == 'launched':
            mission.action_launch()
            mission.write({'takeoff_at': takeoff})
        return mission

    def _blocked_mission(self, now, alerts, used):
        """A mission proposed for a Norwegian area with Ingrid Solberg, whose
        medical has expired: check 9 blocks it until the pilot is changed."""
        client, plane_key, pilot_key = BLOCKED_MISSION
        areas = self._contracts[client].area_ids.filtered(lambda a: a.id not in used['areas'])
        alert = alerts.filtered(lambda a: a.area_id in areas)[:1]
        area = alert.area_id or areas.sorted('name')[:1]
        if not area:
            return self.env['seeding.mission']
        vals = self._present_values(area, alert, self._aircraft[plane_key], self._pilots[pilot_key],
                                    now + timedelta(hours=2))
        if not vals:
            return self.env['seeding.mission']
        used['areas'].add(area.id)
        return self.env['seeding.mission'].create(vals)

