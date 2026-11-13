import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .seeding_weather_snapshot import SCORE_BANDS

_logger = logging.getLogger(__name__)


class SeedingTargetArea(models.Model):
    _name = 'seeding.target.area'
    _description = 'Seeding Target Area'
    _inherit = ['mail.thread']
    _order = 'partner_id, name'

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    partner_id = fields.Many2one(
        'res.partner', string='Client', required=True, tracking=True,
        index=True,
        domain=[('is_company', '=', True)],
        help="The organisation that pays for seeding. Only companies can be clients.",
    )
    country_id = fields.Many2one('res.country', string='Country', tracking=True)
    latitude = fields.Float(
        digits=(10, 6), required=True, tracking=True,
        help="Centre of the area in decimal degrees, north positive. Weather is fetched here.",
    )
    longitude = fields.Float(
        digits=(10, 6), required=True, tracking=True,
        help="Centre of the area in decimal degrees, east positive.",
    )
    radius_km = fields.Float(
        string='Radius (km)', default=25.0, required=True, tracking=True,
        help="Radius of the area around its centre.",
    )
    contract_ids = fields.Many2many(
        'seeding.contract', 'seeding_contract_area_rel', 'area_id', 'contract_id',
        string='Contracts', readonly=True,
    )
    base_ids = fields.Many2many(
        'seeding.base', 'seeding_base_area_rel', 'area_id', 'base_id', string='Served From',
        help="Bases whose aircraft fly to this area.",
    )
    note = fields.Text()

    _latitude_range = models.Constraint(
        'check(latitude >= -90 and latitude <= 90)',
        'Latitude must be between -90 and 90 degrees.',
    )
    _longitude_range = models.Constraint(
        'check(longitude >= -180 and longitude <= 180)',
        'Longitude must be between -180 and 180 degrees.',
    )
    _radius_positive = models.Constraint(
        'check(radius_km > 0)',
        'The radius must be greater than zero.',
    )

    @api.constrains('partner_id')
    def _check_partner_matches_contracts(self):
        for area in self:
            wrong = area.contract_ids.filtered(lambda c: c.partner_id != area.partner_id)
            if wrong:
                raise ValidationError(_(
                    "Area %(area)s is on contract %(contract)s, which belongs to "
                    "another client. Remove it from that contract first.",
                    area=area.name, contract=wrong[0].name,
                ))

    # Weather ----------------------------------------------------------------

    snapshot_ids = fields.One2many('seeding.weather.snapshot', 'area_id', string='Weather Snapshots')
    alert_ids = fields.One2many('seeding.cloud.alert', 'area_id', string='Cloud Alerts')
    snapshot_count = fields.Integer(compute='_compute_weather_stats')
    alert_count = fields.Integer(compute='_compute_weather_stats')
    last_fetched_at = fields.Datetime(string='Last Fetched', compute='_compute_weather_stats')
    last_score = fields.Integer(string='Last Score', compute='_compute_weather_stats')
    last_score_band = fields.Selection(
        SCORE_BANDS, string='Last Seedability', compute='_compute_weather_stats',
    )

    @api.depends('snapshot_ids', 'alert_ids')
    def _compute_weather_stats(self):
        for area in self:
            snapshots = area.snapshot_ids.sorted('fetched_at', reverse=True)
            area.snapshot_count = len(snapshots)
            area.alert_count = len(area.alert_ids)
            area.last_fetched_at = snapshots[:1].fetched_at
            area.last_score = snapshots[:1].score
            area.last_score_band = snapshots[:1].score_band

    def _fetch_weather(self):
        """Fetch a snapshot for each area and raise live alerts.
        Returns (snapshots, alerts)."""
        service = self.env['seeding.weather.service']
        snapshots = self.env['seeding.weather.snapshot']
        alerts = self.env['seeding.cloud.alert']
        for area in self:
            snapshot = service.fetch_area(area)
            snapshots |= snapshot
            alerts |= snapshot._raise_live_alert()
        return snapshots, alerts

    def action_fetch_weather(self):
        """'Fetch Now' button."""
        snapshots, alerts = self._fetch_weather()
        lines = [
            _("%(area)s: score %(score)s.", area=snap.area_id.name, score=snap.score)
            for snap in snapshots
        ]
        if alerts:
            lines.append(_("Alert raised: %s.", ', '.join(alerts.mapped('name'))))
        else:
            lines.append(_("No new alert (below the threshold, or an alert is already open)."))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Weather fetched"),
                'message': ' '.join(lines),
                'type': 'success' if alerts else 'info',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _fetch_weather_safe(self):
        """Fetch each area in its own savepoint: one failing area (no
        network, bad answer) does not stop the others. Used by the cron."""
        fetched = failed = raised = 0
        for area in self:
            try:
                with self.env.cr.savepoint():
                    snapshots, alerts = area._fetch_weather()
                fetched += len(snapshots)
                raised += len(alerts)
            except Exception as error:  # noqa: BLE001 - logged, next area continues
                failed += 1
                _logger.warning("Serein: weather fetch failed for area %s: %s", area.display_name, error)
        _logger.info("Serein: weather fetched for %s area(s), %s failed, %s alert(s) raised.",
                     fetched, failed, raised)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Weather fetched"),
                'message': _(
                    "%(ok)s area(s) fetched, %(failed)s failed, %(raised)s alert(s) raised.",
                    ok=fetched, failed=failed, raised=raised,
                ),
                'type': 'warning' if failed else 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    @api.model
    def _cron_fetch_weather(self):
        self.search([])._fetch_weather_safe()

    def action_view_snapshots(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('serein_ops.seeding_weather_snapshot_action')
        action['domain'] = [('area_id', '=', self.id)]
        return action

    def action_view_alerts(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('serein_ops.seeding_cloud_alert_action')
        action['domain'] = [('area_id', '=', self.id)]
        action['context'] = {'create': False}
        return action
