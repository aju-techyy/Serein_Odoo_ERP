from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .seeding_weather_snapshot import FORMULA_TEXT


class SeedingCloudAlert(models.Model):
    _name = 'seeding.cloud.alert'
    _description = 'Cloud Seeding Alert'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'window_start desc, id desc'

    name = fields.Char(
        string='Reference', required=True, copy=False, readonly=True,
        default=lambda self: _('New'),
    )
    snapshot_id = fields.Many2one(
        'seeding.weather.snapshot', string='Weather Snapshot', required=True,
        ondelete='restrict', index=True, tracking=True,
        help="The saved real weather readings this alert is scored on.",
    )
    area_id = fields.Many2one(
        related='snapshot_id.area_id', string='Target Area', store=True, index=True,
    )
    partner_id = fields.Many2one(related='area_id.partner_id', string='Client', store=True)
    source = fields.Selection(
        [('live', 'Live'), ('archive', 'Forecast Archive'), ('replayed', 'Replayed'), ('manual', 'Manual')],
        required=True, default='manual', readonly=True, tracking=True,
        help="Live: raised by a weather fetch. Forecast Archive: raised for a past "
             "hour from the forecast issued then. Replayed: raised from a saved "
             "snapshot, recorded earlier. Manual: created by an operator.",
    )
    state = fields.Selection(
        [
            ('new', 'New'),
            ('planned', 'Planned'),
            ('dismissed', 'Dismissed'),
            ('expired', 'Expired'),
        ],
        string='Status', default='new', required=True, tracking=True, copy=False,
    )
    window_start = fields.Datetime(
        string='Window Start', required=True, tracking=True,
        default=lambda self: fields.Datetime.now().replace(second=0, microsecond=0),
        help="Start of the seedable window: the forecast hour that raised the alert.",
    )
    window_end = fields.Datetime(
        string='Window End', required=True, tracking=True,
        default=lambda self: fields.Datetime.now().replace(second=0, microsecond=0) + timedelta(hours=2),
        help="End of the seedable window: consecutive forecast hours at or above the threshold, at most 6.",
    )
    expires_at = fields.Datetime(
        string='Expires At', compute='_compute_expires_at', store=True, readonly=False,
        copy=False, tracking=True, help="After this time the alert can no longer be planned.",
    )
    recorded_at = fields.Datetime(
        related='snapshot_id.valid_for', string='Conditions Recorded For',
    )
    replay_label = fields.Char(compute='_compute_replay_label')

    score = fields.Integer(related='snapshot_id.score', string='Seedability Score', store=True)
    cloud_points = fields.Float(related='snapshot_id.cloud_points')
    instability_points = fields.Float(related='snapshot_id.instability_points')
    wind_points = fields.Float(related='snapshot_id.wind_points')
    score_breakdown = fields.Char(compute='_compute_score_breakdown')
    score_formula = fields.Text(compute='_compute_score_breakdown')
    threshold = fields.Integer(
        compute='_compute_score_breakdown', string='Alert Threshold',
        help="Lowest seedability score that raises an alert.",
    )

    cloud_cover_low = fields.Float(related='snapshot_id.cloud_cover_low')
    cloud_cover_mid = fields.Float(related='snapshot_id.cloud_cover_mid')
    cloud_cover_high = fields.Float(related='snapshot_id.cloud_cover_high')
    cape = fields.Float(related='snapshot_id.cape')
    lifted_index = fields.Float(related='snapshot_id.lifted_index')
    precipitation_probability = fields.Float(related='snapshot_id.precipitation_probability')
    relative_humidity = fields.Float(related='snapshot_id.relative_humidity')
    wind_speed = fields.Float(related='snapshot_id.wind_speed')
    wind_gusts = fields.Float(related='snapshot_id.wind_gusts')
    note = fields.Text()
    mission_ids = fields.One2many('seeding.mission', 'alert_id', string='Missions')
    mission_count = fields.Integer(compute='_compute_mission_count')

    _window_order = models.Constraint(
        'check(window_end > window_start)',
        'The window must end after it starts.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('seeding.cloud.alert') or _('New')
        alerts = super().create(vals_list)
        for alert in alerts.filtered(lambda a: a.source == 'replayed'):
            alert.message_post(body=alert.replay_label)
        return alerts

    @api.depends('window_end')
    def _compute_expires_at(self):
        for alert in self:
            alert.expires_at = alert.window_end

    @api.depends('source', 'recorded_at')
    def _compute_replay_label(self):
        for alert in self:
            if alert.source == 'replayed' and alert.recorded_at:
                alert.replay_label = _(
                    "Replayed from conditions recorded for %(stamp)s UTC.",
                    stamp=alert.recorded_at.strftime('%d %b %Y %H:%M'),
                )
            else:
                alert.replay_label = False

    @api.depends('cloud_points', 'instability_points', 'wind_points', 'score')
    def _compute_score_breakdown(self):
        threshold = self.env['seeding.weather.snapshot']._threshold()
        for alert in self:
            alert.score_breakdown = _(
                "Cloud %(cloud)s + Instability %(inst)s + Wind %(wind)s = %(score)s",
                cloud=alert.cloud_points, inst=alert.instability_points,
                wind=alert.wind_points, score=alert.score,
            )
            alert.score_formula = FORMULA_TEXT
            alert.threshold = threshold

    @api.depends('mission_ids')
    def _compute_mission_count(self):
        for alert in self:
            alert.mission_count = len(alert.mission_ids)

    def action_view_missions(self):
        self.ensure_one()
        return self.env['seeding.mission']._open_missions(
            [('alert_id', '=', self.id)], {'default_alert_id': self.id})

    def _release_after_missions(self):
        """A planned alert with no mission left (all cancelled or deleted)
        is open again, or expired if its time has passed."""
        now = fields.Datetime.now()
        for alert in self.filtered(lambda a: a.state == 'planned'):
            if alert.mission_ids.filtered(lambda m: m.state != 'cancelled'):
                continue
            alert.state = 'expired' if alert.expires_at and alert.expires_at <= now else 'new'

    def _open_form(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'seeding.cloud.alert',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_plan_with_ai(self):
        self.ensure_one()
        mission = self.env['seeding.mission.planner'].plan_alert(self)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'seeding.mission',
            'res_id': mission.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_dismiss(self):
        if any(a.state != 'new' for a in self):
            raise UserError(_("Only a new alert can be dismissed."))
        self.state = 'dismissed'

    def action_reset(self):
        now = fields.Datetime.now()
        for alert in self:
            if alert.state != 'dismissed':
                raise UserError(_("Only a dismissed alert can be reopened."))
            if alert.expires_at and alert.expires_at <= now:
                raise UserError(_("Alert %s has expired and cannot be reopened.", alert.name))
        self.state = 'new'

    @api.model
    def _cron_expire_alerts(self):
        expired = self.search([
            ('state', '=', 'new'),
            ('expires_at', '<=', fields.Datetime.now()),
        ])
        expired.write({'state': 'expired'})
        return expired
