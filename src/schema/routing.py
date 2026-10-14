"""Bounded, directed route estimates tied to indexed place identities."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.security.guardrails import validate_text


ROUTING_SOURCE_URL = 'https://giscience.github.io/openrouteservice/api-reference/endpoints/matrix/'
PROFILES = {'walking': 'foot-walking', 'driving': 'driving-car', 'taxi': 'driving-car'}


class RouteLocation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    entity_id: str = Field(min_length=1, max_length=180)
    name: str = Field(min_length=1, max_length=300)
    city: str = Field(min_length=1, max_length=160)
    latitude: float | int = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float | int = Field(ge=-180, le=180, allow_inf_nan=False)

    @model_validator(mode='after')
    def checked(self):
        if not self.entity_id.startswith(('google:', 'wikivoyage:')):
            raise ValueError('Routes require an indexed place identity.')
        for value in (self.entity_id, self.name, self.city):
            validate_text(value, 'route location', max_chars=300, reject_secrets=False)
        return self


Metric = float | int | None


class RouteMatrixEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    provider: Literal['OpenRouteService'] = 'OpenRouteService'
    url: Literal[ROUTING_SOURCE_URL] = ROUTING_SOURCE_URL
    mode: Literal['walking', 'driving', 'taxi']
    profile: Literal['foot-walking', 'driving-car']
    fetched_at: str = Field(max_length=40)
    locations: list[RouteLocation] = Field(min_length=2, max_length=16)
    durations_seconds: list[list[Metric]] = Field(min_length=2, max_length=16)
    distances_meters: list[list[Metric]] = Field(min_length=2, max_length=16)

    @model_validator(mode='after')
    def checked(self):
        stamp = datetime.fromisoformat(self.fetched_at)
        if stamp.tzinfo is None or PROFILES[self.mode] != self.profile:
            raise ValueError('Route provenance is invalid.')
        size = len(self.locations)
        if len({point.entity_id for point in self.locations}) != size:
            raise ValueError('Route locations must have unique identities.')
        for matrix, maximum in ((self.durations_seconds, 604800), (self.distances_meters, 20_000_000)):
            if len(matrix) != size or any(len(row) != size for row in matrix):
                raise ValueError('Route matrix dimensions do not match its locations.')
            if any(value is not None and (type(value) not in (int, float) or not 0 <= value <= maximum)
                   for row in matrix for value in row):
                raise ValueError('Route metrics must be finite, nonnegative numbers or null.')
        return self
