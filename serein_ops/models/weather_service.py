import json
import logging
from datetime import datetime

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

OPEN_METEO_URL = 'https://api.open-meteo.com/v1/forecast'
OPEN_METEO_TIMEOUT = 15  # seconds

# Hourly variables requested from Open-Meteo, mapped to snapshot fields.
HOURLY_FIELDS = {
    'cloud_cover': 'cloud_cover',
    'cloud_cover_low': 'cloud_cover_low',
    'cloud_cover_mid': 'cloud_cover_mid',
    'cloud_cover_high': 'cloud_cover_high',
    'cape': 'cape',
    'lifted_index': 'lifted_index',
    'convective_inhibition': 'convective_inhibition',
    'precipitation': 'precipitation',
    'rain': 'rain',
    'showers': 'showers',
    'precipitation_probability': 'precipitation_probability',
    'relative_humidity_2m': 'relative_humidity',
    'freezing_level_height': 'freezing_level',
    'wind_speed_10m': 'wind_speed',
    'wind_gusts_10m': 'wind_gusts',
    'wind_direction_10m': 'wind_direction',
}


class SeedingWeatherService(models.AbstractModel):
    """Fetches live conditions from Open-Meteo and saves them as snapshots.

    Only `_request` talks to the network, so tests mock that one method."""
    _name = 'seeding.weather.service'
    _description = 'Serein Weather Service (Open-Meteo)'

    @api.model
    def _request(self, latitude, longitude):
        """Return Open-Meteo's decoded JSON for one point. Times are in UTC."""
        params = {
            'latitude': latitude,
            'longitude': longitude,
            'hourly': ','.join(HOURLY_FIELDS),
            'forecast_days': 2,
            'timezone': 'GMT',
        }
        response = requests.get(OPEN_METEO_URL, params=params, timeout=OPEN_METEO_TIMEOUT)
        response.raise_for_status()
        return response.json()

    @api.model
    def _current_hour(self):
        return fields.Datetime.now().replace(minute=0, second=0, microsecond=0)

    @api.model
    def _parse(self, data, hour):
        """Pick the readings for `hour` (naive UTC) out of an hourly response.

        Returns a dict of snapshot field values."""
        hourly = data.get('hourly') or {}
        times = hourly.get('time') or []
        key = hour.strftime('%Y-%m-%dT%H:%M')
        if key not in times:
            raise UserError(_(
                "The weather feed did not include the hour %(hour)s UTC.", hour=key,
            ))
        index = times.index(key)
        vals = {'valid_for': hour}
        for api_name, field_name in HOURLY_FIELDS.items():
            series = hourly.get(api_name)
            value = series[index] if series and index < len(series) else None
            vals[field_name] = value if value is not None else 0.0
        return vals

    @api.model
    def fetch_area(self, area):
        """Fetch current conditions for one area and save a snapshot.

        Raises UserError if the feed cannot be reached or read."""
        area.ensure_one()
        try:
            data = self._request(area.latitude, area.longitude)
        except requests.exceptions.RequestException as error:
            raise UserError(_(
                "Could not reach the weather feed for %(area)s: %(error)s",
                area=area.name, error=error,
            )) from error
        except ValueError as error:
            raise UserError(_(
                "The weather feed sent an unreadable answer for %(area)s.", area=area.name,
            )) from error
        vals = self._parse(data, self._current_hour())
        vals.update({
            'area_id': area.id,
            'fetched_at': fields.Datetime.now(),
            'raw_response': json.dumps(data),
        })
        return self.env['seeding.weather.snapshot'].create(vals)


def hourly_rows(raw_response, start):
    """Yield one dict of readings per hour from `start` onwards, from a saved
    raw Open-Meteo response. Used to find how long a seedable window lasts."""
    try:
        hourly = json.loads(raw_response or '{}').get('hourly') or {}
    except ValueError:
        return
    times = hourly.get('time') or []
    for index, stamp in enumerate(times):
        try:
            moment = datetime.strptime(stamp, '%Y-%m-%dT%H:%M')
        except (TypeError, ValueError):
            continue
        if moment < start:
            continue
        row = {'valid_for': moment}
        for api_name, field_name in HOURLY_FIELDS.items():
            series = hourly.get(api_name) or []
            value = series[index] if index < len(series) else None
            row[field_name] = value if value is not None else 0.0
        yield row
