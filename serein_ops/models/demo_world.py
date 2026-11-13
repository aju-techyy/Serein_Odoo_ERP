"""The demo world: a complete, fictional cloud seeding operator.

Loaded once into an empty Serein database from Configuration > Load Demo
World. Every record goes through the module's own code: missions are
proposed, pass the readiness engine, fly, release their flares by batch, and
are invoiced by the contract. Dates are relative to the moment of loading.
"""
import json
import logging
from datetime import datetime, time, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import file_open

_logger = logging.getLogger(__name__)

WEATHER_FILE = 'serein_ops/data/demo_world/weather_%s.json'

COMPANY_NAME = 'Stratus Ridge Weather Services'

CLIENTS = {
    'wadi_noor': {'name': 'Wadi Noor Water Authority', 'country': 'base.ae', 'city': 'Al Dhaid'},
    'tasik_hijau': {'name': 'Tasik Hijau Water Consortium', 'country': 'base.my', 'city': 'Kuantan'},
    'breelv': {'name': 'Breelv Kraft AS', 'country': 'base.no', 'city': 'Sogndal'},
}

# key: (name, client, country, latitude, longitude, radius km)
AREAS = {
    'hajar_foothills': ('Hajar Foothills', 'wadi_noor', 'base.ae', 24.95, 56.05, 35),
    'gravel_plains': ('Eastern Gravel Plains', 'wadi_noor', 'base.ae', 24.3, 55.6, 40),
    'johor_catchment': ('Johor Catchment', 'tasik_hijau', 'base.my', 1.75, 103.6, 30),
    'kuantan_coast': ('Kuantan Coast', 'tasik_hijau', 'base.my', 3.8, 103.3, 30),
    'sogn_basin': ('Sogn Basin', 'breelv', 'base.no', 61.2, 7.0, 25),
    'hardanger_catchment': ('Hardanger Catchment', 'breelv', 'base.no', 60.4, 6.5, 25),
}

# client: (flight-hour budget, standby fee per day, flight-hour rate)
CONTRACTS = {
    'wadi_noor': (120, 1200, 2800),
    'tasik_hijau': (60, 900, 2400),
    'breelv': (80, 1500, 3200),
}
SEASON_STARTED_DAYS_AGO = 60
SEASON_DAYS_LEFT = 120

BASES = {'ridgeview': 'Ridgeview Airfield', 'harbour_point': 'Harbour Point Airfield'}

# key: (name, type, tail, base, rack, service interval h, hourly cost)
AIRCRAFT = {
    'king_air': ('King Air 350', 'crewed', 'A6-SRK', 'ridgeview', 24, 50, 950),
    'cessna': ('Cessna 340', 'crewed', 'A6-SRC', 'ridgeview', 16, 100, 520),
    'cheyenne': ('Piper Cheyenne II', 'crewed', '9M-SRP', 'harbour_point', 20, 100, 680),
    'drone': ('Seeder Drone SD-1', 'drone', 'SD-01', 'harbour_point', 4, 100, 90),
}
LAST_SERVICE_DAYS_AGO = 50

# key: (name, role, licence no., licence expiry in days, medical expiry in days, hourly cost)
PILOTS = {
    'haddad': ('Leila Haddad', 'pilot', 'CPL-41207', 410, 160, 140),
    'menon': ('Ravi Menon', 'pilot', 'CPL-38815', 290, 120, 130),
    'okoro': ('Daniel Okoro', 'pilot', 'CPL-45530', 520, 210, 135),
    'solberg': ('Ingrid Solberg', 'pilot', 'CPL-29944', 350, -9, 150),
    'tan': ('Aiko Tan', 'drone_operator', 'RPL-07712', 600, None, 60),
}

FLARE = {
    'name': 'HF-1000 Hygroscopic Flare', 'default_code': 'HF-1000',
    'grams_per_flare': 1000, 'cost': 85.0, 'vendor': 'Altostratus Pyrotechnics', 'lead_days': 3,
}
# batch: (base, flares, received days ago)
BATCHES = {
    'HF-2608-A': ('ridgeview', 120, 58),
    'HF-2609-B': ('ridgeview', 120, 40),
    'HF-2610-C': ('ridgeview', 160, 20),
    'HF-2609-D': ('harbour_point', 60, 55),
    'HF-2610-E': ('harbour_point', 80, 25),
}
# base: (minimum, maximum) of the automatic reorder rule
REORDER = {'ridgeview': (80, 240), 'harbour_point': (40, 140)}

# Past missions: (days ago, takeoff hour UTC, area, aircraft, pilot, minutes
# flown, flares planned, flares fired, planned by). The King Air flies 2900
# minutes (48.3 h) after its last service: just under its 50 h interval.
MISSIONS = [
    (44, 10, 'hajar_foothills', 'king_air', 'haddad', 225, 24, 22, 'human'),
    (42, 13, 'kuantan_coast', 'cheyenne', 'okoro', 150, 20, 18, 'human'),
    (41, 9, 'sogn_basin', 'king_air', 'menon', 230, 24, 20, 'rules'),
    (39, 11, 'gravel_plains', 'cessna', 'haddad', 140, 16, 14, 'human'),
    (37, 6, 'johor_catchment', 'drone', 'tan', 55, 4, 4, 'rules'),
    (36, 12, 'hajar_foothills', 'king_air', 'menon', 235, 24, 24, 'human'),
    (34, 8, 'hardanger_catchment', 'king_air', 'okoro', 220, 24, 19, 'human'),
    (32, 14, 'kuantan_coast', 'drone', 'tan', 50, 4, 3, 'human'),
    (31, 10, 'gravel_plains', 'king_air', 'haddad', 215, 24, 21, 'rules'),
    (29, 7, 'sogn_basin', 'cessna', 'menon', 165, 16, 16, 'human'),
    (27, 12, 'johor_catchment', 'cheyenne', 'okoro', 175, 20, 20, 'rules'),
    (26, 9, 'hajar_foothills', 'king_air', 'haddad', 230, 24, 23, 'human'),
    (24, 13, 'hardanger_catchment', 'king_air', 'menon', 225, 24, 18, 'rules'),
    (22, 6, 'kuantan_coast', 'drone', 'tan', 60, 4, 4, 'human'),
    (20, 11, 'gravel_plains', 'king_air', 'okoro', 220, 24, 20, 'human'),
    (18, 8, 'sogn_basin', 'king_air', 'haddad', 240, 24, 24, 'rules'),
    (16, 12, 'johor_catchment', 'cheyenne', 'okoro', 160, 20, 17, 'human'),
    (14, 10, 'hajar_foothills', 'cessna', 'menon', 150, 16, 15, 'human'),
    (12, 9, 'hardanger_catchment', 'king_air', 'menon', 225, 24, 22, 'human'),
    (11, 7, 'kuantan_coast', 'drone', 'tan', 45, 4, 4, 'rules'),
    (9, 13, 'gravel_plains', 'king_air', 'haddad', 220, 24, 21, 'human'),
    (7, 10, 'sogn_basin', 'cessna', 'okoro', 155, 16, 14, 'rules'),
    (5, 11, 'hajar_foothills', 'king_air', 'menon', 215, 24, 22, 'human'),
    (4, 6, 'johor_catchment', 'cheyenne', 'okoro', 170, 20, 19, 'human'),
    (2, 12, 'hardanger_catchment', 'king_air', 'haddad', 200, 24, 20, 'rules'),
]
# Observed rainfall is recorded at loading for missions that landed at least
# this many days ago. Younger missions keep it for "Fetch Observed Rainfall".
RAINFALL_RECORDED_AFTER_DAYS = 14


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
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Demo world loaded"),
                'message': _(
                    "%(clients)s clients, %(areas)s target areas, %(aircraft)s aircraft, "
                    "%(pilots)s pilots, %(missions)s past missions (%(rainfall)s with observed "
                    "rainfall), %(snapshots)s weather snapshots, %(invoices)s invoices.",
                    **counts,
                ),
                'type': 'success',
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
        # Superuser rights for stock, maintenance, employees, and accounting;
        # records keep the loading user as their author.
        self = self.sudo()
        now = fields.Datetime.now().replace(minute=0, second=0, microsecond=0)
        today = fields.Date.context_today(self)
        company = self.env.company

        self._setup_company(company)
        clients = self._create_clients()
        areas = self._create_areas(clients)
        contracts = self._create_contracts(clients, areas, today)
        bases = self._create_bases()
        flare = self._create_flare(bases, now)
        aircraft = self._create_aircraft(bases, now)
        pilots = self._create_pilots(today)
        snapshots = self._create_snapshots(areas)
        missions = self._fly_missions(areas, contracts, aircraft, pilots, flare, now, today)
        rainfall = self._record_rainfall(missions, now)
        writer = self.env['seeding.mission.writer']
        for mission in missions:
            writer.write_summary_from_record(mission)
        invoices = self._invoice_past_months(contracts, today)
        aircraft_records = self.env['maintenance.equipment'].browse([a.id for a in aircraft.values()])
        aircraft_records._check_service_due()
        _logger.info("Serein: demo world loaded (%s missions).", len(missions))
        return {
            'clients': len(clients), 'areas': len(areas), 'aircraft': len(aircraft),
            'pilots': len(pilots), 'missions': len(missions), 'rainfall': rainfall,
            'snapshots': len(snapshots), 'invoices': len(invoices),
        }

    @api.model
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

    @api.model
    def _create_clients(self):
        clients = {}
        for key, data in CLIENTS.items():
            clients[key] = self.env['res.partner'].create({
                'name': data['name'], 'is_company': True, 'city': data['city'],
                'country_id': self.env.ref(data['country']).id,
            })
        return clients

    @api.model
    def _create_areas(self, clients):
        areas = {}
        for key, (name, client, country, lat, lon, radius) in AREAS.items():
            areas[key] = self.env['seeding.target.area'].create({
                'name': name, 'partner_id': clients[client].id,
                'country_id': self.env.ref(country).id,
                'latitude': lat, 'longitude': lon, 'radius_km': radius,
            })
        return areas

    @api.model
    def _create_contracts(self, clients, areas, today):
        contracts = {}
        for key, (budget, standby, rate) in CONTRACTS.items():
            client_areas = [a.id for k, a in areas.items() if AREAS[k][1] == key]
            contract = self.env['seeding.contract'].create({
                'partner_id': clients[key].id,
                'area_ids': [(6, 0, client_areas)],
                'season_start': today - timedelta(days=SEASON_STARTED_DAYS_AGO),
                'season_end': today + timedelta(days=SEASON_DAYS_LEFT),
                'flight_hour_budget': budget,
                'standby_fee': standby,
                'flight_hour_rate': rate,
            })
            contract.action_activate()
            contracts[key] = contract
        return contracts

    @api.model
    def _create_bases(self):
        return {key: self.env['seeding.base'].create({'name': name}) for key, name in BASES.items()}

    @api.model
    def _create_flare(self, bases, now):
        """The flare, its vendor, its batches at each base (received by
        inventory adjustment on their receipt day), and an automatic reorder
        rule per base."""
        vendor = self.env['res.partner'].create({'name': FLARE['vendor'], 'is_company': True})
        template = self.env['product.template'].create({
            'name': FLARE['name'], 'default_code': FLARE['default_code'],
            'type': 'consu', 'is_storable': True, 'tracking': 'lot',
            'is_seeding_flare': True, 'grams_per_flare': FLARE['grams_per_flare'],
            'purchase_ok': True, 'sale_ok': False, 'standard_price': FLARE['cost'],
            'seller_ids': [(0, 0, {
                'partner_id': vendor.id, 'price': FLARE['cost'], 'delay': FLARE['lead_days'],
            })],
        })
        flare = template.product_variant_id
        company = self.env.company
        source = flare.with_company(company).property_stock_inventory or self.env['stock.location'].browse(
            self.env['ir.default'].with_company(company)._get_model_defaults('product.template').get(
                'property_stock_inventory'))
        # The same stock move an inventory adjustment makes, dated on receipt.
        for batch, (base, qty, days_ago) in sorted(BATCHES.items(), key=lambda item: -item[1][2]):
            received = now - timedelta(days=days_ago)
            location = bases[base].location_id
            lot = self.env['stock.lot'].create({'name': batch, 'product_id': flare.id})
            move = self.env['stock.move'].create({
                'product_id': flare.id, 'product_uom': flare.uom_id.id, 'product_uom_qty': qty,
                'company_id': company.id, 'state': 'confirmed', 'is_inventory': True, 'picked': True,
                'location_id': source.id, 'location_dest_id': location.id,
                'inventory_name': _("Batch %(batch)s received from %(vendor)s",
                                    batch=batch, vendor=vendor.name),
                'move_line_ids': [(0, 0, {
                    'product_id': flare.id, 'product_uom_id': flare.uom_id.id, 'quantity': qty,
                    'location_id': source.id, 'location_dest_id': location.id,
                    'company_id': company.id, 'lot_id': lot.id,
                })],
            })
            move._action_done()
            move.write({'date': received})
            move.move_line_ids.write({'date': received})
            self.env['stock.quant'].search([
                ('product_id', '=', flare.id), ('lot_id', '=', lot.id), ('location_id', '=', location.id),
            ]).write({'in_date': received})
        for base, (minimum, maximum) in REORDER.items():
            location = bases[base].location_id
            self.env['stock.warehouse.orderpoint'].create({
                'product_id': flare.id, 'location_id': location.id,
                'warehouse_id': location.warehouse_id.id,
                'product_min_qty': minimum, 'product_max_qty': maximum, 'trigger': 'auto',
            })
        return flare

    @api.model
    def _create_aircraft(self, bases, now):
        """Aircraft, each with its last scheduled service closed
        LAST_SERVICE_DAYS_AGO days ago (before every past mission)."""
        Request = self.env['maintenance.request']
        done_stage = self.env['maintenance.stage'].search([('done', '=', True)], order='sequence', limit=1)
        serviced_at = now - timedelta(days=LAST_SERVICE_DAYS_AGO)
        aircraft = {}
        for key, (name, kind, tail, base, rack, interval, cost) in AIRCRAFT.items():
            plane = self.env['maintenance.equipment'].create({
                'name': name, 'is_aircraft': True, 'aircraft_type': kind, 'tail_number': tail,
                'home_base_id': bases[base].id, 'rack_capacity': rack,
                'service_interval_hours': interval, 'aircraft_hourly_cost': cost,
            })
            request = Request.create({
                'name': _("Scheduled service: %s", name),
                'equipment_id': plane.id,
                'maintenance_type': 'preventive',
                'is_scheduled_service': True,
                'maintenance_team_id': plane.maintenance_team_id.id or Request._get_default_team_id(),
                'request_date': serviced_at.date(),
                'schedule_date': serviced_at - timedelta(hours=6),
                'stage_id': done_stage.id,
            })
            request.write({'service_completed_at': serviced_at, 'close_date': serviced_at.date()})
            aircraft[key] = plane
        return aircraft

    @api.model
    def _create_pilots(self, today):
        pilots = {}
        for key, (name, role, licence, licence_days, medical_days, cost) in PILOTS.items():
            pilots[key] = self.env['hr.employee'].create({
                'name': name, 'is_pilot': True, 'pilot_role': role,
                'pilot_licence_number': licence,
                'licence_expiry': today + timedelta(days=licence_days),
                'medical_expiry': today + timedelta(days=medical_days) if medical_days is not None else False,
                'weekly_duty_limit': 30, 'pilot_hourly_cost': cost,
            })
        return pilots

    @api.model
    def _create_snapshots(self, areas):
        """Saved real weather snapshots: Open-Meteo forecasts recorded for
        each area (data/demo_world), stored as they were fetched."""
        service = self.env['seeding.weather.service']
        snapshots = self.env['seeding.weather.snapshot']
        for key, area in areas.items():
            with file_open(WEATHER_FILE % key, 'r') as handle:
                recorded = json.load(handle)
            fetched_at = datetime.strptime(recorded['fetched_at'], '%Y-%m-%d %H:%M:%S')
            data = recorded['response']
            vals = service._parse(data, fetched_at.replace(minute=0, second=0))
            vals.update({
                'area_id': area.id, 'fetched_at': fetched_at, 'raw_response': json.dumps(data),
            })
            snapshots |= snapshots.create(vals)
        return snapshots

    @api.model
    def _fly_missions(self, areas, contracts, aircraft, pilots, flare, now, today):
        """Each past mission goes through the real flow: proposed (the
        readiness engine runs), ready, launched, completed (flares leave the
        base by batch, the service interval is checked)."""
        Mission = self.env['seeding.mission']
        missions = Mission
        for days_ago, hour, area, plane, pilot, minutes, planned, fired, planned_by in MISSIONS:
            takeoff = datetime.combine(today - timedelta(days=days_ago), time(hour, 0))
            landing = takeoff + timedelta(minutes=minutes)
            contract = contracts[AREAS[area][1]]
            vals = {
                'area_id': areas[area].id, 'contract_id': contract.id,
                'aircraft_id': aircraft[plane].id, 'pilot_id': pilots[pilot].id,
                'flare_product_id': flare.id, 'flares_planned': planned,
                'planned_takeoff': takeoff, 'launch_by': takeoff + timedelta(hours=1),
                'planned_by': planned_by,
            }
            if planned_by == 'rules':
                vals['plan_reasoning'] = _(
                    "Rule-based plan: %s All applicable readiness checks passed.",
                    self.env['seeding.mission.planner']._rule_summary(aircraft[plane], pilots[pilot], planned),
                )
            mission = Mission.create(vals)
            if mission.readiness_state != 'pass':
                failed = mission.readiness_ids.filtered(lambda c: c.result == 'fail')
                raise UserError(_(
                    "Demo world mission on %(day)s did not pass its readiness checks: %(checks)s",
                    day=takeoff.date(), checks='; '.join(
                        '%s: %s' % (c.name, c.explanation) for c in failed),
                ))
            mission.write({'state': 'ready'})
            mission.write({'state': 'launched', 'takeoff_at': takeoff})
            mission.write({'flares_fired': fired, 'landing_at': landing})
            mission.write({'state': 'completed'})
            picking = mission._release_fired_flares()
            # The release happened on landing.
            picking.write({'date_done': landing})
            picking.move_ids.write({'date': landing})
            picking.move_ids.move_line_ids.write({'date': landing})
            mission.aircraft_id._check_service_due()
            mission.readiness_checked_at = takeoff - timedelta(minutes=30)
            missions |= mission
        return missions

    @api.model
    def _record_rainfall(self, missions, now):
        """Observed rainfall for missions that landed at least
        RAINFALL_RECORDED_AFTER_DAYS ago: one archive call per area. If the
        archive cannot be reached, the missions keep it for later."""
        cutoff = now - timedelta(days=RAINFALL_RECORDED_AFTER_DAYS)
        old = missions.filtered(lambda m: m.landing_at <= cutoff)
        recorded = 0
        service = self.env['seeding.weather.service']
        for area in old.area_id:
            area_missions = old.filtered(lambda m: m.area_id == area)
            periods = [m._rainfall_period() for m in area_missions]
            start = min(p[0] for p in periods)
            end = max(p[1] for p in periods)
            try:
                with self.env.cr.savepoint():
                    data = service.fetch_archive(area, start, end)
                    for mission in area_missions:
                        mission._record_rainfall(data)
                recorded += len(area_missions)
            except UserError as error:
                _logger.warning("Serein: demo world rainfall not recorded for %s: %s", area.name, error)
        return recorded

    @api.model
    def _invoice_past_months(self, contracts, today):
        """Invoice every past calendar month of each season. All but the most
        recent month are posted; the most recent stays a draft. The current
        month is left to invoice."""
        invoices = self.env['account.move']
        month_start = today.replace(day=1)
        for contract in contracts.values():
            months = []
            first = contract.season_start.replace(day=1)
            while first < month_start:
                last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
                months.append((max(first, contract.season_start), last))
                first = last + timedelta(days=1)
            for index, (date_from, date_to) in enumerate(months):
                post = index < len(months) - 1
                invoice = contract._create_invoice(
                    date_from, date_to, invoice_date=date_to + timedelta(days=1) if post else None,
                )
                if not invoice:
                    continue
                invoices |= invoice
                if post:
                    invoice.action_post()
        return invoices
