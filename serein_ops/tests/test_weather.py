import json
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.serein_ops.models.seeding_weather_snapshot import seedability
from odoo.addons.serein_ops.models.weather_service import HOURLY_FIELDS, hourly_slice

SEEDABLE = {'cloud_cover_low': 80, 'cloud_cover_mid': 60, 'cape': 800, 'wind_speed': 15}
CLEAR = {'cloud_cover_low': 0, 'cloud_cover_mid': 0, 'cape': 0, 'wind_speed': 10}


@tagged('post_install', '-at_install')
class TestWeather(TransactionCase):
    """Weather snapshots, the seedability score, and cloud alerts.
    The network is never used: the service's `_request` is mocked."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env['res.partner'].create({'name': 'Northwind Water Authority', 'is_company': True})
        cls.area = cls.env['seeding.target.area'].create({
            'name': 'Northern Highlands', 'partner_id': cls.client.id,
            'latitude': 24.5, 'longitude': 55.9, 'radius_km': 40,
        })
        cls.area2 = cls.env['seeding.target.area'].create({
            'name': 'Southern Valley', 'partner_id': cls.client.id,
            'latitude': 23.9, 'longitude': 55.4, 'radius_km': 30,
        })
        cls.hour = fields.Datetime.now().replace(minute=0, second=0, microsecond=0)
        cls.Service = cls.registry['seeding.weather.service']

    # Helpers ---------------------------------------------------------------

    def _response(self, hours_seedable=3, current=None):
        """A fake Open-Meteo answer: 3 hours before now to 30 hours after.
        From the current hour, `hours_seedable` hours look like `current`
        (SEEDABLE by default), every other hour is CLEAR."""
        current = current or SEEDABLE
        times, rows = [], []
        for offset in range(-3, 31):
            moment = self.hour + timedelta(hours=offset)
            times.append(moment.strftime('%Y-%m-%dT%H:%M'))
            rows.append(current if 0 <= offset < hours_seedable else CLEAR)
        hourly = {'time': times}
        for api_name, field_name in HOURLY_FIELDS.items():
            hourly[api_name] = [row.get(field_name, 1.0) for row in rows]
        hourly['relative_humidity_2m'] = [77.0] * len(times)
        return {'latitude': 24.5, 'longitude': 55.9, 'hourly': hourly}

    def _patched(self, response=None, side_effect=None):
        request = patch.object(self.Service, '_request', return_value=response, side_effect=side_effect)
        hour = patch.object(self.Service, '_current_hour', return_value=self.hour)
        return request, hour

    def _fetch(self, area=None, **kwargs):
        request, hour = self._patched(self._response(**kwargs))
        with request as mocked, hour:
            snapshots, alerts = (area or self.area)._fetch_weather()
        return snapshots, alerts, mocked

    # The formula -------------------------------------------------------------

    def test_formula_full_marks(self):
        self.assertEqual(seedability(100, 40, 1000, 10), (50.0, 30.0, 20.0, 100))

    def test_formula_clear_sky_scores_only_wind(self):
        self.assertEqual(seedability(0, 0, 0, 5), (0.0, 0.0, 20.0, 20))

    def test_formula_uses_larger_of_low_and_mid_cloud(self):
        self.assertEqual(seedability(20, 70, 0, 100)[0], 35.0)

    def test_formula_caps_and_clamps(self):
        self.assertEqual(seedability(150, 0, 5000, 0)[:2], (50.0, 30.0))
        self.assertEqual(seedability(-5, -5, -100, -3), (0.0, 0.0, 20.0, 20))

    def test_formula_wind_scale(self):
        self.assertEqual(seedability(0, 0, 0, 20)[2], 20.0)
        self.assertEqual(seedability(0, 0, 0, 35)[2], 10.0)
        self.assertEqual(seedability(0, 0, 0, 50)[2], 0.0)
        self.assertEqual(seedability(0, 0, 0, 80)[2], 0.0)

    # Fetching ----------------------------------------------------------------

    def test_fetch_stores_readings(self):
        snapshots, _alerts, mocked = self._fetch()
        mocked.assert_called_once_with(24.5, 55.9)
        snap = snapshots
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap.area_id, self.area)
        self.assertEqual(snap.partner_id, self.client)
        self.assertEqual(snap.valid_for, self.hour)
        self.assertEqual(snap.cloud_cover_low, 80)
        self.assertEqual(snap.cloud_cover_mid, 60)
        self.assertEqual(snap.cape, 800)
        self.assertEqual(snap.wind_speed, 15)
        self.assertEqual(snap.relative_humidity, 77)
        self.assertEqual(json.loads(snap.raw_response)['latitude'], 24.5)
        # 0.5 x 80 + 30 x 0.8 + 20 = 40 + 24 + 20
        self.assertEqual((snap.cloud_points, snap.instability_points, snap.wind_points), (40.0, 24.0, 20.0))
        self.assertEqual(snap.score, 84)
        self.assertEqual(self.area.snapshot_count, 1)
        self.assertEqual(self.area.last_score, 84)

    def test_fetch_missing_hour_is_an_error(self):
        response = self._response()
        response['hourly']['time'] = ['2000-01-01T00:00'] * len(response['hourly']['time'])
        request, hour = self._patched(response)
        with request, hour, self.assertRaises(UserError):
            self.area._fetch_weather()

    def test_fetch_network_failure_is_a_user_error(self):
        request, hour = self._patched(side_effect=requests.exceptions.ConnectionError('offline'))
        with request, hour, self.assertRaises(UserError):
            self.area.action_fetch_weather()
        self.assertFalse(self.area.snapshot_ids)

    @mute_logger('odoo.addons.serein_ops.models.seeding_target_area')
    def test_cron_continues_after_a_failing_area(self):
        good = self._response()

        def fake(latitude, longitude):
            if latitude == self.area.latitude:
                raise requests.exceptions.Timeout('slow')
            return good

        request, hour = self._patched(side_effect=fake)
        with request, hour:
            (self.area | self.area2)._fetch_weather_safe()
        self.assertFalse(self.area.snapshot_ids)
        self.assertEqual(len(self.area2.snapshot_ids), 1)
        self.assertEqual(len(self.area2.alert_ids), 1)

    def test_fetch_all_button_through_rpc_path(self):
        """Call the list-header button the way the web client does: through
        call_kw with an (empty) id list as the first argument."""
        from odoo.service.model import call_kw
        request, hour = self._patched(self._response())
        with request, hour:
            action = call_kw(self.env['seeding.weather.snapshot'], 'action_fetch_all_areas', [[]], {})
        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual(len(self.area.snapshot_ids), 1)
        self.assertEqual(len(self.area2.snapshot_ids), 1)

    def test_area_buttons_through_rpc_path(self):
        from odoo.service.model import call_kw
        request, hour = self._patched(self._response())
        with request, hour:
            call_kw(self.env['seeding.target.area'], 'action_fetch_weather', [[self.area.id]], {})
        snapshot = self.area.snapshot_ids
        action = call_kw(self.env['seeding.weather.snapshot'], 'action_replay', [[snapshot.id]], {})
        self.assertEqual(self.env['seeding.cloud.alert'].browse(action['res_id']).source, 'replayed')

    def test_fetch_now_button_returns_notification(self):
        request, hour = self._patched(self._response())
        with request, hour:
            action = self.area.action_fetch_weather()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertIn('ALR/', action['params']['message'])

    # Alerts ------------------------------------------------------------------

    def test_high_score_raises_live_alert(self):
        snapshots, alerts, _mocked = self._fetch(hours_seedable=3)
        self.assertEqual(len(alerts), 1)
        alert = alerts
        self.assertTrue(alert.name.startswith('ALR/'))
        self.assertEqual(alert.snapshot_id, snapshots)
        self.assertEqual(alert.area_id, self.area)
        self.assertEqual(alert.partner_id, self.client)
        self.assertEqual(alert.source, 'live')
        self.assertEqual(alert.state, 'new')
        self.assertEqual(alert.score, 84)
        self.assertEqual(alert.window_start, self.hour)
        self.assertEqual(alert.window_end, self.hour + timedelta(hours=3))
        self.assertEqual(alert.expires_at, alert.window_end)
        self.assertIn('= 84', alert.score_breakdown)
        self.assertFalse(alert.replay_label)

    def test_window_is_capped_at_six_hours(self):
        _snapshots, alerts, _mocked = self._fetch(hours_seedable=20)
        self.assertEqual(alerts.window_end, self.hour + timedelta(hours=6))

    def test_low_score_raises_no_alert(self):
        snapshots, alerts, _mocked = self._fetch(current=CLEAR)
        self.assertEqual(snapshots.score, 20)
        self.assertFalse(alerts)

    def test_threshold_is_a_system_parameter(self):
        self.env['ir.config_parameter'].sudo().set_param('serein_ops.seedability_threshold', 90)
        _snapshots, alerts, _mocked = self._fetch()
        self.assertFalse(alerts)
        self.env['ir.config_parameter'].sudo().set_param('serein_ops.seedability_threshold', 15)
        _snapshots, alerts, _mocked = self._fetch(current=CLEAR)
        self.assertTrue(alerts)

    def test_no_duplicate_while_an_alert_is_open(self):
        _s, first, _m = self._fetch()
        _s, second, _m = self._fetch()
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(self.area.snapshot_ids), 2)
        first.action_dismiss()
        _s, third, _m = self._fetch()
        self.assertTrue(third)

    # Replay ------------------------------------------------------------------

    def test_replay_raises_labelled_alert(self):
        snapshots, live, _mocked = self._fetch(hours_seedable=2)
        live.action_dismiss()
        action = snapshots.action_replay()
        replay = self.env['seeding.cloud.alert'].browse(action['res_id'])
        self.assertEqual(replay.source, 'replayed')
        self.assertEqual(replay.snapshot_id, snapshots)
        self.assertEqual(replay.recorded_at, self.hour)
        self.assertIn(self.hour.strftime('%d %b %Y %H:%M'), replay.replay_label)
        self.assertGreaterEqual(replay.window_start, self.hour)
        self.assertEqual(replay.window_end - replay.window_start, timedelta(hours=2))
        self.assertTrue(any('Replay' in (m.body or '') for m in replay.message_ids))

    def test_replay_refuses_low_score(self):
        snapshots, _alerts, _mocked = self._fetch(current=CLEAR)
        with self.assertRaises(UserError):
            snapshots.action_replay()

    # Status and expiry ---------------------------------------------------------

    def test_expire_cron(self):
        snapshots, alert, _mocked = self._fetch()
        past = fields.Datetime.now() - timedelta(hours=1)
        dismissed = alert.copy({'window_start': past - timedelta(hours=1), 'window_end': past})
        dismissed.action_dismiss()
        old = alert.copy({'window_start': past - timedelta(hours=1), 'window_end': past})
        planned = old.copy({'state': 'planned'})
        expired = self.env['seeding.cloud.alert']._cron_expire_alerts()
        self.assertEqual(expired, old)
        self.assertEqual(old.state, 'expired')
        self.assertEqual(alert.state, 'new')
        self.assertEqual(dismissed.state, 'dismissed')
        self.assertEqual(planned.state, 'planned')

    def test_dismiss_and_reopen(self):
        _s, alert, _m = self._fetch()
        alert.action_dismiss()
        self.assertEqual(alert.state, 'dismissed')
        with self.assertRaises(UserError):
            alert.action_dismiss()
        alert.action_reset()
        self.assertEqual(alert.state, 'new')
        alert.action_dismiss()
        alert.expires_at = fields.Datetime.now() - timedelta(minutes=1)
        with self.assertRaises(UserError):
            alert.action_reset()

    # Access ------------------------------------------------------------------

    def test_operator_can_fetch_and_dismiss(self):
        operator = new_test_user(self.env, login='op_weather', groups='serein_ops.group_serein_operator')
        area = self.area.with_user(operator)
        request, hour = self._patched(self._response())
        with request, hour:
            area.action_fetch_weather()
        alert = area.alert_ids
        self.assertEqual(len(alert), 1)
        alert.action_dismiss()
        self.assertEqual(alert.state, 'dismissed')
        with self.assertRaises(AccessError):
            area.snapshot_ids.unlink()
        with self.assertRaises(AccessError):
            alert.unlink()

    # Forecast archive --------------------------------------------------------

    def test_history_fetch_and_archive_alert(self):
        response = self._response(hours_seedable=4)
        start = self.hour - timedelta(days=1)
        with patch.object(self.Service, '_request_history', return_value=response) as mocked:
            data = self.env['seeding.weather.service'].fetch_history(self.area, start.date(), self.hour.date())
        mocked.assert_called_once()
        sliced = hourly_slice(data, self.hour, 13)
        self.assertEqual(sliced['hourly']['time'][0], self.hour.strftime('%Y-%m-%dT%H:%M'))
        self.assertEqual(len(sliced['hourly']['time']), 13)
        vals = self.env['seeding.weather.service']._parse(sliced, self.hour)
        vals.update({'area_id': self.area.id, 'raw_response': json.dumps(sliced), 'data_source': 'archive'})
        snapshot = self.env['seeding.weather.snapshot'].create(vals)
        alert = snapshot._raise_archive_alert()
        self.assertEqual(alert.source, 'archive')
        self.assertEqual(alert.window_start, self.hour)
        self.assertEqual(alert.window_end, self.hour + timedelta(hours=4))
        # A live snapshot never raises an archive alert.
        live = self.env['seeding.weather.snapshot'].create(dict(vals, data_source='forecast'))
        self.assertFalse(live._raise_archive_alert())

    def test_history_fetch_network_failure_is_a_user_error(self):
        with patch.object(self.Service, '_request_history',
                          side_effect=requests.exceptions.ConnectionError('offline')):
            with self.assertRaisesRegex(UserError, 'forecast archive'):
                self.env['seeding.weather.service'].fetch_history(
                    self.area, self.hour.date(), self.hour.date())
