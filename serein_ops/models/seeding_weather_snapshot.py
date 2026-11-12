from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .weather_service import hourly_rows

# Seedability score, 0 to 100 points.
CLOUD_MAX_POINTS = 50        # from the larger of low and mid cloud cover
INSTABILITY_MAX_POINTS = 30  # from CAPE, capped
CAPE_CAP = 1000.0            # J/kg at which instability scores full points
WIND_MAX_POINTS = 20         # calm enough to fly
WIND_CALM = 20.0             # km/h or less: full wind points
WIND_LIMIT = 50.0            # km/h or more: no wind points

DEFAULT_THRESHOLD = 60
THRESHOLD_PARAM = 'serein_ops.seedability_threshold'
MAX_WINDOW_HOURS = 6


def seedability(cloud_cover_low, cloud_cover_mid, cape, wind_speed):
    """Return (cloud points, instability points, wind points, total score)."""
    seedable_cloud = max(cloud_cover_low or 0.0, cloud_cover_mid or 0.0)
    cloud = CLOUD_MAX_POINTS * min(max(seedable_cloud, 0.0), 100.0) / 100.0
    instability = INSTABILITY_MAX_POINTS * min(max(cape or 0.0, 0.0), CAPE_CAP) / CAPE_CAP
    wind_speed = max(wind_speed or 0.0, 0.0)
    if wind_speed <= WIND_CALM:
        wind = float(WIND_MAX_POINTS)
    elif wind_speed >= WIND_LIMIT:
        wind = 0.0
    else:
        wind = WIND_MAX_POINTS * (WIND_LIMIT - wind_speed) / (WIND_LIMIT - WIND_CALM)
    cloud, instability, wind = round(cloud, 1), round(instability, 1), round(wind, 1)
    return cloud, instability, wind, int(round(cloud + instability + wind))


FORMULA_TEXT = (
    "Score = Cloud + Instability + Wind (0 to 100).\n"
    "Cloud (max 50) = 50 x max(low cloud %, mid cloud %) / 100. High cloud does not count.\n"
    "Instability (max 30) = 30 x min(CAPE, 1000 J/kg) / 1000.\n"
    "Wind (max 20) = 20 at 20 km/h or less, 0 at 50 km/h or more, straight line in between."
)


class SeedingWeatherSnapshot(models.Model):
    _name = 'seeding.weather.snapshot'
    _description = 'Weather Snapshot'
    _order = 'valid_for desc, id desc'

    name = fields.Char(compute='_compute_name', store=True)
    area_id = fields.Many2one(
        'seeding.target.area', string='Target Area', required=True,
        ondelete='cascade', index=True,
    )
    partner_id = fields.Many2one(related='area_id.partner_id', string='Client', store=True)
    fetched_at = fields.Datetime(string='Fetched At', required=True, default=fields.Datetime.now)
    valid_for = fields.Datetime(
        string='Valid For', required=True,
        help="The forecast hour (start) these readings describe.",
    )

    cloud_cover = fields.Float(string='Total Cloud (%)')
    cloud_cover_low = fields.Float(string='Low Cloud (%)', help="Below 3 km.")
    cloud_cover_mid = fields.Float(string='Mid Cloud (%)', help="3 to 8 km.")
    cloud_cover_high = fields.Float(string='High Cloud (%)', help="Above 8 km.")
    cape = fields.Float(string='CAPE (J/kg)', help="Convective available potential energy.")
    lifted_index = fields.Float(string='Lifted Index', help="Negative means unstable air.")
    convective_inhibition = fields.Float(string='Convective Inhibition (J/kg)')
    precipitation = fields.Float(string='Precipitation (mm)', help="Over the preceding hour.")
    rain = fields.Float(string='Rain (mm)')
    showers = fields.Float(string='Showers (mm)')
    precipitation_probability = fields.Float(string='Precipitation Probability (%)')
    relative_humidity = fields.Float(string='Relative Humidity (%)')
    freezing_level = fields.Float(string='Freezing Level (m)')
    wind_speed = fields.Float(string='Wind Speed (km/h)', help="At 10 m.")
    wind_gusts = fields.Float(string='Wind Gusts (km/h)')
    wind_direction = fields.Float(string='Wind Direction (°)')
    raw_response = fields.Text(string='Raw Response', help="The full answer from Open-Meteo.")

    cloud_points = fields.Float(
        compute='_compute_score', store=True, digits=(4, 1),
        help="50 x the larger of low and mid cloud cover / 100. High cloud (above 8 km, "
             "ice crystals such as cirrus) is not seedable and scores nothing, even at 100 %.",
    )
    instability_points = fields.Float(
        compute='_compute_score', store=True, digits=(4, 1),
        help="30 x CAPE / 1000, capped at 1000 J/kg.",
    )
    wind_points = fields.Float(compute='_compute_score', store=True, digits=(4, 1))
    score = fields.Integer(
        string='Seedability Score', compute='_compute_score', store=True, aggregator='max',
        help=FORMULA_TEXT,
    )
    alert_ids = fields.One2many('seeding.cloud.alert', 'snapshot_id', string='Alerts')
    alert_count = fields.Integer(compute='_compute_alert_count')

    @api.depends('area_id.name', 'valid_for')
    def _compute_name(self):
        for snap in self:
            stamp = snap.valid_for.strftime('%d %b %Y %H:%M') if snap.valid_for else ''
            snap.name = f"{snap.area_id.name or ''} {stamp} UTC".strip()

    @api.depends('cloud_cover_low', 'cloud_cover_mid', 'cape', 'wind_speed')
    def _compute_score(self):
        for snap in self:
            (snap.cloud_points, snap.instability_points,
             snap.wind_points, snap.score) = seedability(
                snap.cloud_cover_low, snap.cloud_cover_mid, snap.cape, snap.wind_speed,
            )

    @api.depends('alert_ids')
    def _compute_alert_count(self):
        for snap in self:
            snap.alert_count = len(snap.alert_ids)

    @api.model
    def _threshold(self):
        value = self.env['ir.config_parameter'].sudo().get_param(THRESHOLD_PARAM, DEFAULT_THRESHOLD)
        try:
            return int(value)
        except (TypeError, ValueError):
            return DEFAULT_THRESHOLD

    def _seedable_hours(self, threshold):
        """Consecutive hours, from `valid_for` on, whose score reaches the
        threshold (read from the saved raw response). At least 1 when the
        snapshot itself reaches it, at most MAX_WINDOW_HOURS."""
        self.ensure_one()
        hours = 0
        for row in hourly_rows(self.raw_response, self.valid_for):
            if seedability(row['cloud_cover_low'], row['cloud_cover_mid'],
                           row['cape'], row['wind_speed'])[3] < threshold:
                break
            hours += 1
            if hours >= MAX_WINDOW_HOURS:
                break
        if not hours and self.score >= threshold:
            hours = 1
        return hours

    def _raise_live_alert(self):
        """Raise a live alert if this snapshot passes the threshold and its
        area has no open alert yet. Returns the alert or an empty recordset."""
        self.ensure_one()
        Alert = self.env['seeding.cloud.alert']
        threshold = self._threshold()
        if self.score < threshold:
            return Alert
        open_alert = Alert.search([
            ('area_id', '=', self.area_id.id),
            ('state', '=', 'new'),
            ('expires_at', '>', fields.Datetime.now()),
        ], limit=1)
        if open_alert:
            return Alert
        hours = self._seedable_hours(threshold)
        start = self.valid_for
        return Alert.create({
            'snapshot_id': self.id,
            'source': 'live',
            'window_start': start,
            'window_end': start + timedelta(hours=hours),
        })

    def action_replay(self):
        """Raise an alert now from these saved real readings, labelled as a replay."""
        self.ensure_one()
        threshold = self._threshold()
        if self.score < threshold:
            raise UserError(_(
                "This snapshot scored %(score)s, below the alert threshold of "
                "%(threshold)s. Pick a recording with seedable conditions.",
                score=self.score, threshold=threshold,
            ))
        hours = self._seedable_hours(threshold) or 1
        start = fields.Datetime.now().replace(second=0, microsecond=0)
        alert = self.env['seeding.cloud.alert'].create({
            'snapshot_id': self.id,
            'source': 'replayed',
            'window_start': start,
            'window_end': start + timedelta(hours=hours),
        })
        return alert._open_form()

    def action_fetch_all_areas(self):
        """'Fetch all areas now' button on the snapshot list. Not @api.model:
        list header buttons always send the selected ids (possibly none), so
        the method must accept a recordset. `self` is not used."""
        return self.env['seeding.target.area'].search([])._fetch_weather_safe()

    def action_view_alerts(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('serein_ops.seeding_cloud_alert_action')
        action['domain'] = [('snapshot_id', '=', self.id)]
        action['context'] = {'create': False}
        return action

