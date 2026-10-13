"""Open-Meteo WeatherSearch tool: bounded forecasts, never invented future weather."""

from datetime import date, datetime, timedelta, timezone
import json
from math import isfinite
import os
import re
import threading
from time import monotonic
import unicodedata

import httpx

from src.schema.weather import ForecastDay, WeatherEvidence, iso_date
from src.security.guardrails import validate_text


_CACHE = {}
_LOCK = threading.Lock()
_DAILY = ('weather_code', 'temperature_2m_min', 'temperature_2m_max',
          'precipitation_probability_max', 'precipitation_sum', 'wind_speed_10m_max')
_CONDITIONS = {0: 'Clear sky', 1: 'Mainly clear', 2: 'Partly cloudy', 3: 'Overcast',
    45: 'Fog', 48: 'Depositing rime fog', 51: 'Light drizzle', 53: 'Moderate drizzle', 55: 'Dense drizzle',
    56: 'Light freezing drizzle', 57: 'Dense freezing drizzle', 61: 'Slight rain', 63: 'Moderate rain', 65: 'Heavy rain',
    66: 'Light freezing rain', 67: 'Heavy freezing rain', 71: 'Slight snowfall', 73: 'Moderate snowfall', 75: 'Heavy snowfall',
    77: 'Snow grains', 80: 'Slight rain showers', 81: 'Moderate rain showers', 82: 'Violent rain showers',
    85: 'Slight snow showers', 86: 'Heavy snow showers', 95: 'Thunderstorm',
    96: 'Thunderstorm with slight hail', 99: 'Thunderstorm with heavy hail'}


def reference_today():
    # VISTRA's local chat uses Asia/Bangkok; no Windows tzdata installation is required.
    return datetime.now(timezone(timedelta(hours=7))).date()


def normalized(value):
    value = unicodedata.normalize('NFKD', (value or '').casefold()).replace('đ', 'd')
    return ''.join(char for char in value if not unicodedata.combining(char))


def wants_weather(query):
    return bool(re.search(r'\b(weather|forecast|itinerary|itineraries|thoi tiet|du bao|lich trinh)\b|\bplan\b.{0,70}\b(trip|days?|visit)\b', normalized(query)))


def trip_dates(query, state=None, *, start=None, end=None, today=None):
    """Prefer explicit options/current ISO dates, then interpreted active-trip dates.

    Natural-language dates are resolved by the existing conversation interpreter.
    Stateless callers can pass start/end; ISO dates and today/tomorrow also work.
    An undated duration does not silently become a trip starting today.
    """
    state, today = state or {}, today or reference_today()
    if start is not None or end is not None:
        if start is None or end is None:
            raise ValueError('Supply both trip start and end dates.')
        first, last = iso_date(start), iso_date(end)
    else:
        explicit = list(dict.fromkeys(re.findall(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)', query)))
        if len(explicit) > 2:
            return None
        if explicit:
            first = iso_date(explicit[0])
            last = iso_date(explicit[1]) if len(explicit) == 2 else None
        elif re.search(r'\b(tomorrow|ngay mai)\b', normalized(query)):
            first, last = today + timedelta(days=1), None
        elif re.search(r'\b(today|hom nay)\b', normalized(query)):
            first, last = today, None
        elif state.get('start_date') and state.get('end_date'):
            first, last = iso_date(state['start_date']), iso_date(state['end_date'])
        else:
            return None
        if last is None:
            weather_question = bool(re.search(r'\b(weather|forecast|thoi tiet|du bao)\b', normalized(query))) and not bool(
                re.search(r'\b(itinerary|itineraries|lich trinh)\b|\bplan\b.{0,70}\b(trip|days?|visit)\b', normalized(query)))
            duration_text = query if weather_question else query + ' ' + str(state.get('dates') or '')
            duration = re.search(r'\b(\d{1,2})[ -]*(?:days?|ngay)\b', normalized(duration_text))
            if duration and 1 <= int(duration[1]) <= 60:
                last = first + timedelta(days=int(duration[1])-1)
            elif not weather_question and state.get('start_date') and state.get('end_date'):
                last = first + (iso_date(state['end_date']) - iso_date(state['start_date']))
            else:
                last = first
    if first > last:
        raise ValueError('Trip end date precedes its start date.')
    return first.isoformat(), last.isoformat()


class WeatherSearch:
    def __init__(self, *, client=None, timeout=8, now=None, api_key=None, use_cache=True):
        if type(timeout) not in (int, float) or not isfinite(timeout) or not 0 < timeout <= 15:
            raise ValueError('Weather timeout must be between 0 and 15 seconds.')
        self.timeout, self.now, self.use_cache = timeout, now or (lambda: datetime.now(timezone.utc)), use_cache
        if api_key is None:
            from dotenv import load_dotenv
            from src.utils.utils import PROJECT_ROOT
            load_dotenv(PROJECT_ROOT / '.env', override=False)
        self.api_key = os.getenv('OPEN_METEO_API_KEY', '') if api_key is None else api_key
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)

    def _json(self, params):
        host = 'customer-api.open-meteo.com' if self.api_key else 'api.open-meteo.com'
        if self.api_key:
            params = {**params, 'apikey': self.api_key}
        deadline, content = monotonic() + self.timeout, bytearray()
        with self.client.stream('GET', f'https://{host}/v1/forecast', params=params, timeout=self.timeout,
                                follow_redirects=False, headers={'Accept-Encoding': 'identity', 'User-Agent': 'VISTRA/1.0'}) as response:
            response.raise_for_status()
            if response.is_redirect or response.headers.get('content-encoding', 'identity') != 'identity' or 'application/json' not in response.headers.get('content-type', ''):
                raise ValueError('Unexpected weather response.')
            for chunk in response.iter_bytes(chunk_size=65536):
                content.extend(chunk)
                if len(content) > 65536 or monotonic() > deadline:
                    raise ValueError('Weather response exceeded its budget.')
        data = json.loads(content)
        if not isinstance(data, dict) or data.get('error'):
            raise ValueError('Weather provider returned an error.')
        return data

    def run(self, *, latitude, longitude, location, start_date, end_date):
        location = validate_text(location, 'weather location', max_chars=160, reject_secrets=False)
        first, last = iso_date(start_date), iso_date(end_date)
        if first > last:
            raise ValueError('Trip end date precedes its start date.')
        for value, minimum, maximum in ((latitude, -90, 90), (longitude, -180, 180)):
            if type(value) not in (int, float) or not isfinite(value) or not minimum <= value <= maximum:
                raise ValueError('Weather coordinates are invalid.')
        base = dict(location=location, latitude=latitude, longitude=longitude, start_date=start_date, end_date=end_date)
        now = self.now()
        if now.tzinfo is None:
            raise ValueError('The weather clock must include a timezone.')
        today = now.astimezone(timezone(timedelta(hours=7))).date()
        # Allow a one-day timezone boundary; the returned local forecast dates are authoritative.
        if first > today + timedelta(days=16) or last < today - timedelta(days=1):
            return WeatherEvidence(**base, status='outside_forecast_window', note='Trip dates are outside the short-range forecast window. No daily forecast is available.').model_dump()
        key = (round(latitude, 5), round(longitude, 5), start_date, end_date, location, bool(self.api_key), today)
        if self.use_cache:
            with _LOCK:
                cached = _CACHE.get(key)
            if cached and monotonic() - cached[0] < 1800:
                return WeatherEvidence.model_validate(cached[1]).model_dump()
        try:
            data = self._json({'latitude': latitude, 'longitude': longitude, 'daily': ','.join(_DAILY),
                'forecast_days': 16, 'timezone': 'auto', 'temperature_unit': 'celsius',
                'wind_speed_unit': 'kmh', 'precipitation_unit': 'mm'})
            daily, units = data['daily'], data['daily_units']
            dates = daily['time']
            if not isinstance(dates, list) or not 1 <= len(dates) <= 16 or dates != sorted(set(dates)):
                raise ValueError('Invalid weather dates.')
            expected_units = {'weather_code': 'wmo code', 'temperature_2m_min': '°C', 'temperature_2m_max': '°C',
                              'precipitation_probability_max': '%', 'precipitation_sum': 'mm', 'wind_speed_10m_max': 'km/h'}
            if any(units.get(field) != unit for field, unit in expected_units.items()):
                raise ValueError('Unexpected weather units.')
            for field in _DAILY:
                if not isinstance(daily[field], list) or len(daily[field]) != len(dates):
                    raise ValueError('Mismatched weather columns.')
            days = []
            for index, day in enumerate(dates):
                value = iso_date(day)
                if first <= value <= last:
                    if all(daily[field][index] is None for field in _DAILY):
                        continue
                    code = daily['weather_code'][index]
                    days.append(ForecastDay(date=day, condition=_CONDITIONS.get(code, 'Condition unavailable') if type(code) is int else 'Condition unavailable',
                        temperature_min_c=daily['temperature_2m_min'][index], temperature_max_c=daily['temperature_2m_max'][index],
                        precipitation_probability_pct=daily['precipitation_probability_max'][index],
                        precipitation_mm=daily['precipitation_sum'][index], wind_max_kmh=daily['wind_speed_10m_max'][index]))
            zone = data['timezone']
            if not isinstance(zone, str) or not re.fullmatch(r'[A-Za-z0-9_+./-]{1,80}', zone):
                raise ValueError('Invalid forecast timezone.')
            complete = len(days) == (last-first).days + 1
            has_dates = any(first <= iso_date(day) <= last for day in dates)
            status = 'available' if complete else 'partial' if days else 'unavailable' if has_dates else 'outside_forecast_window'
            note = ('Daily forecast at the indexed destination coordinates; forecasts may change.' if complete else
                    'Only the listed trip days have forecast data; other days remain unknown.' if days else
                    'Forecast values for these trip dates are unavailable. No daily weather has been inferred.' if has_dates else
                    'No forecast covers these trip dates. No daily weather has been inferred.')
            result = WeatherEvidence(**base, status=status, timezone=zone, fetched_at=now.isoformat(), days=days, note=note).model_dump()
            if self.use_cache:
                with _LOCK:
                    if len(_CACHE) >= 64:
                        _CACHE.pop(next(iter(_CACHE)))
                    _CACHE[key] = monotonic(), result
            return result
        except Exception:
            # Do not expose HTTP errors, request URLs or a commercial API key.
            return WeatherEvidence(**base, status='unavailable', note='The weather service could not provide a forecast. Continue planning without weather assumptions.').model_dump()

    def close(self):
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def weather_for_trip(query, response, *, state=None, start=None, end=None, tool=None, enabled=True, timeout=8):
    """Use a verified retrieved destination coordinate; never geocode an ambiguous guess."""
    location = response.location or (state or {}).get('destination')
    if not enabled:
        return WeatherEvidence(status='disabled', location=location, note='Weather lookup is disabled.').model_dump()
    try:
        dates = trip_dates(query, state, start=start, end=end)
    except ValueError:
        return WeatherEvidence(status='missing_dates', location=location, note='Please provide a valid trip date range, with the end on or after the start.').model_dump()
    if dates is None:
        return WeatherEvidence(status='missing_dates', location=location, note='Trip dates are needed for a daily weather forecast. An undated itinerary can still be drafted.').model_dump()
    from src.resolver.location_resolver import LocationResolver
    from src.resolver.image_distance_resolver import ImageDistanceResolver
    reference = next((hit for hit in response.results if hit.source == 'text' and location and hit.name
                      and LocationResolver._normalize(hit.name) == LocationResolver._normalize(location)
                      and ImageDistanceResolver.coordinates(hit) is not None), None)
    if reference is None or response.location_status == 'unresolved':
        return WeatherEvidence(status='missing_location', location=location, start_date=dates[0], end_date=dates[1],
                               note='A verified destination coordinate is needed for weather. No nearby place has been substituted.').model_dump()
    own = tool is None
    try:
        tool = tool or WeatherSearch(timeout=timeout)
        return WeatherEvidence.model_validate(tool.run(latitude=reference.latitude, longitude=reference.longitude,
            location=reference.name, start_date=dates[0], end_date=dates[1])).model_dump()
    except Exception:
        return WeatherEvidence(status='unavailable', location=location, start_date=dates[0], end_date=dates[1],
                               note='Weather could not be checked. Continue planning without weather assumptions.').model_dump()
    finally:
        if own and tool is not None:
            tool.close()
