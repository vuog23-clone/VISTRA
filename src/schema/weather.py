"""Typed, bounded weather evidence shared by planning, grounding and the demo."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


WEATHER_SOURCE_URL = 'https://open-meteo.com/en/docs'


def iso_date(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('Trip dates must use YYYY-MM-DD.')
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('Trip dates must use YYYY-MM-DD.')
    return parsed


class ForecastDay(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    date: str = Field(min_length=10, max_length=10)
    condition: str = Field(max_length=80)
    temperature_min_c: float | int | None = Field(default=None, ge=-100, le=70, allow_inf_nan=False)
    temperature_max_c: float | int | None = Field(default=None, ge=-100, le=70, allow_inf_nan=False)
    precipitation_probability_pct: float | int | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    precipitation_mm: float | int | None = Field(default=None, ge=0, le=3000, allow_inf_nan=False)
    wind_max_kmh: float | int | None = Field(default=None, ge=0, le=500, allow_inf_nan=False)

    @model_validator(mode='after')
    def checked(self):
        iso_date(self.date)
        if self.temperature_min_c is not None and self.temperature_max_c is not None and self.temperature_min_c > self.temperature_max_c:
            raise ValueError('Forecast temperature bounds conflict.')
        return self


class WeatherEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_id: Literal['W1'] = 'W1'
    provider: Literal['Open-Meteo'] = 'Open-Meteo'
    url: Literal['https://open-meteo.com/en/docs'] = WEATHER_SOURCE_URL
    status: Literal['available', 'partial', 'missing_dates', 'missing_location', 'outside_forecast_window', 'unavailable', 'disabled']
    location: str | None = Field(default=None, max_length=160)
    latitude: float | int | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | int | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    timezone: str | None = Field(default=None, max_length=80)
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    fetched_at: str | None = Field(default=None, max_length=40)
    days: list[ForecastDay] = Field(default_factory=list, max_length=16)
    note: str = Field(max_length=600)

    @model_validator(mode='after')
    def checked(self):
        start = iso_date(self.start_date) if self.start_date else None
        end = iso_date(self.end_date) if self.end_date else None
        if start and end and start > end:
            raise ValueError('Trip end date precedes its start date.')
        dates = [iso_date(day.date) for day in self.days]
        if dates != sorted(set(dates)) or any((start and day < start) or (end and day > end) for day in dates):
            raise ValueError('Forecast days must be unique, ordered and within the requested trip.')
        if self.status in ('available', 'partial'):
            if not dates or not start or not end or not self.timezone or not self.fetched_at:
                raise ValueError('A forecast needs dates, timezone and a fetch timestamp.')
            if self.status == 'available' and len(dates) != (end - start).days + 1:
                raise ValueError('A complete forecast must cover every trip day.')
        elif dates:
            raise ValueError('Unavailable weather cannot contain forecast days.')
        if self.fetched_at and datetime.fromisoformat(self.fetched_at).tzinfo is None:
            raise ValueError('Weather fetch timestamps need a UTC offset.')
        return self
