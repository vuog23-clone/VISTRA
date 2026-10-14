"""OpenRouteService matrix adapter; no guessed coordinates, transit or fares."""

from collections import OrderedDict
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
from math import isfinite
import os
import threading
from time import monotonic

import httpx

from src.schema.routing import PROFILES, RouteLocation, RouteMatrixEvidence


MATRIX_URL = 'https://api.heigit.org/openrouteservice/v2/matrix/'
_CACHE, _LOCK = OrderedDict(), threading.Lock()


class RoutingUnavailable(ValueError):
    """Only fixed, credential-free messages cross the tool boundary."""


class OpenRouteService:
    def __init__(self, *, api_key=None, client=None, timeout=8, use_cache=True, now=None):
        if type(timeout) not in (int, float) or not isfinite(timeout) or not 0 < timeout <= 15:
            raise ValueError('Routing timeout must be between 0 and 15 seconds.')
        if api_key is None:
            from dotenv import load_dotenv
            from src.utils.utils import PROJECT_ROOT
            load_dotenv(PROJECT_ROOT / '.env', override=False)
        self.api_key = os.getenv('ORS_API_KEY', '') if api_key is None else api_key
        self.timeout, self.use_cache = timeout, use_cache
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)

    def matrix(self, locations, *, mode):
        if not self.api_key:
            raise RoutingUnavailable('ORS_API_KEY is not configured. Route times remain unknown.')
        if mode not in PROFILES:
            raise RoutingUnavailable('OpenRouteService does not supply public-transit schedules or fares.')
        points = [RouteLocation.model_validate(point) for point in locations]
        if not 2 <= len(points) <= 16 or len({point.entity_id for point in points}) != len(points):
            raise ValueError('Routing requires 2 to 16 distinct indexed locations.')
        now = self.now()
        if now.tzinfo is None:
            raise ValueError('The routing clock must include a timezone.')
        payload = {'locations': [[point.longitude, point.latitude] for point in points],
                   'metrics': ['duration', 'distance'], 'units': 'm'}
        key = (sha256(self.api_key.encode()).hexdigest(), mode,
               json.dumps([point.model_dump() for point in points], sort_keys=True))
        if self.use_cache:
            with _LOCK:
                cached = _CACHE.get(key)
                if cached and monotonic() - cached[0] < 1800:
                    _CACHE.move_to_end(key)
                    return cached[1].model_copy(deep=True)
        try:
            deadline, content = monotonic() + self.timeout, bytearray()
            with self.client.stream('POST', MATRIX_URL + PROFILES[mode], json=payload,
                headers={'Authorization': self.api_key, 'Accept-Encoding': 'identity', 'User-Agent': 'VISTRA/1.0'},
                timeout=self.timeout, follow_redirects=False) as response:
                if response.status_code == 429:
                    raise RoutingUnavailable('OpenRouteService quota is exhausted. Route times remain unknown.')
                if response.status_code in (401, 403):
                    raise RoutingUnavailable('OpenRouteService rejected the API key or its permissions.')
                response.raise_for_status()
                if response.is_redirect or response.headers.get('content-encoding', 'identity') != 'identity' or 'application/json' not in response.headers.get('content-type', ''):
                    raise ValueError('Unexpected routing response.')
                for chunk in response.iter_bytes(chunk_size=65536):
                    content.extend(chunk)
                    if len(content) > 131072 or monotonic() > deadline:
                        raise ValueError('Routing response exceeded its budget.')
            data = json.loads(content)
            if not isinstance(data, dict) or data.get('error'):
                raise ValueError('Invalid routing response.')
            result = RouteMatrixEvidence(mode=mode, profile=PROFILES[mode], fetched_at=now.isoformat(),
                locations=points, durations_seconds=data['durations'], distances_meters=data['distances'])
        except RoutingUnavailable:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            logging.getLogger(__name__).warning('OpenRouteService matrix unavailable (%s; HTTP status %s).',
                                               type(error).__name__, status or 'not applicable')
            raise RoutingUnavailable('OpenRouteService could not supply validated routes. Route times remain unknown.') from None
        if self.use_cache:
            with _LOCK:
                _CACHE[key] = (monotonic(), result.model_copy(deep=True))
                _CACHE.move_to_end(key)
                while len(_CACHE) > 32:
                    _CACHE.popitem(last=False)
        return result

    def close(self):
        if self._owns_client:
            self.client.close()


def route_for_pair(notebook, before, after, request, *, now=None):
    """Use directed cells only when identities, coordinates, mode and age match."""
    now = now or datetime.now(timezone.utc)
    for entry in reversed(notebook.entries):
        matrix = entry.route_matrix
        if matrix is None or request.transport_mode and matrix.mode != request.transport_mode:
            continue
        if matrix.mode == 'driving' and 'no_driving' in request.transport_constraints:
            continue
        age = (now - datetime.fromisoformat(matrix.fetched_at)).total_seconds()
        if not -300 <= age <= 1800:
            continue
        indices = {point.entity_id: index for index, point in enumerate(matrix.locations)}
        if before.entity_id not in indices or after.entity_id not in indices:
            continue
        a, b = indices[before.entity_id], indices[after.entity_id]
        if any((point.latitude, point.longitude, point.city) != (record.latitude, record.longitude, record.city)
               for point, record in ((matrix.locations[a], before), (matrix.locations[b], after))):
            continue
        seconds, meters = matrix.durations_seconds[a][b], matrix.distances_meters[a][b]
        if seconds is not None and meters is not None:
            return entry.entry_id, matrix, seconds, meters
    return None
