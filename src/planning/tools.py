"""TravelPlanner-compatible tools backed by VISTRA or explicit benchmark mode.

These native adapters avoid upstream dependency pins and never run eval() on data.
Benchmark mode requires separately supplied historical CSV data.
"""

import csv
from hashlib import sha256
from math import ceil
from dataclasses import asdict
from pathlib import Path
import re

from src.schema.models import SearchResponse, SearchResult
from src.schema.planning import EvidenceRecord, ToolResult
from src.retrieval.category_filters import metadata_photo_category
from src.resolver.location_resolver import LocationResolver
from src.service.weather_service import weather_for_trip
from src.utils.utils import read_jsonl, resolve_path


class TravelTools:
    def __init__(self, request, query, *, resources, search_service=None, weather_tool=None,
                 config, limit=20, route=None, include_weather=None, on_progress=None, previous=None):
        self.request, self.query, self.resources = request, query, resources
        self.search_service, self.weather_tool, self.config = search_service, weather_tool, config
        self.limit, self.route, self.on_progress = min(limit, 20), route or 'text', on_progress
        self.include_weather = config.get('weather_enabled', True) and include_weather is not False
        self.response = SearchResponse(route='hybrid', location=request.destinations[0] if request.destinations else None)
        self.guide_responses = {}
        self._catalog = None
        self._places = None
        self._city_references = None
        self._preferred_entities = {choice.entity_id for day in previous.days for _, choice in day.choices()} if previous else set()

    def _search(self):
        if self.search_service is None:
            from src.service.search_service import SearchService
            self.search_service = self.resources.enter_context(SearchService())
        return self.search_service

    def _google(self):
        if self._catalog is None:
            from src.retrieval.google_metadata_catalog import GoogleMetadataCatalog
            self._catalog = GoogleMetadataCatalog(config=self.config)
            self.resources.callback(self._catalog.close)
        return self._catalog

    def run(self, name, city, *, action=None, notebook=None):
        if name == 'GuideSearch':
            response = self._search().search(self.query, location=city, route=self.route, limit=self.limit,
                                           on_progress=self.on_progress)
            self.guide_responses[city] = response
            self.response.results.extend(response.results)
            self.response.related_results.extend(response.related_results)
            self.response.location_status = response.location_status
            records = []
            for hit in response.results:
                if hit.source != 'text':
                    continue
                excerpts = list(dict.fromkeys(str(unit.payload.get('text') or '') for unit in [hit, *hit.evidence]))
                try:
                    records.append(EvidenceRecord(entity_id=hit.entity_id, kind='guide', name=hit.name or city,
                        city=city, provider='Wikivoyage', url=hit.url, excerpt='\n'.join(excerpts)[:1800],
                        latitude=hit.latitude, longitude=hit.longitude))
                except ValueError:
                    continue
            return ToolResult(tool=name, city=city, status='available' if records else 'no_results', records=records,
                              note='Indexed guide evidence; freshness is unknown.')
        if name == 'WeatherSearch':
            response = self.guide_responses.get(city) or SearchResponse(route='text', location=city)
            weather = weather_for_trip(self.query, response, state=self.request.model_dump(),
                start=self.request.start_date, end=self.request.end_date, tool=self.weather_tool,
                enabled=self.include_weather, timeout=self.config.get('weather_timeout_seconds', 8))
            return ToolResult(tool=name, city=city, status='available' if weather['status'] == 'available' else
                'partial' if weather['days'] else 'unavailable', weather=weather, note=weather['note'])
        if self.config.get('planning_data_mode', 'vistra') == 'benchmark':
            return self._benchmark(name, city, action=action)
        if name in ('AttractionSearch', 'RestaurantSearch', 'AccommodationSearch'):
            return self._places_for(name, city)
        if name == 'DistanceMatrix':
            return self._distance_matrix(city, action=action, notebook=notebook)
        if name == 'FlightSearch' and (not self.request.origin or 'no_flight' in self.request.transport_constraints):
            return ToolResult(tool=name, city=city, status='not_applicable', note='No departure-city flight is requested or flights are excluded.')
        return ToolResult(tool=name, city=city, status='unavailable', note={
            'FlightSearch': 'No current flight schedules or fares provider is connected.',
            'CitySearch': 'Region-to-city candidate selection is not connected. Specify the destination cities.'
        }.get(name, 'This tool is unavailable.'))

    def _distance_matrix(self, city, *, action, notebook):
        from src.schema.routing import RouteLocation, ROUTING_SOURCE_URL
        from src.service.routing_service import OpenRouteService, RoutingUnavailable
        if not self.config.get('ors_enabled', True):
            return ToolResult(tool='DistanceMatrix', city=city, status='unavailable', note='OpenRouteService routing is disabled.')
        mode = self.request.transport_mode or (action.mode if action else None) or 'walking'
        if mode == 'public_transit':
            return ToolResult(tool='DistanceMatrix', city=city, status='unavailable',
                note='OpenRouteService does not supply public-transit schedules or fares.')
        if mode == 'driving' and 'no_driving' in self.request.transport_constraints:
            return ToolResult(tool='DistanceMatrix', city=city, status='unavailable',
                note='Driving routes conflict with the no-driving constraint. Choose walking or taxi explicitly.')
        maximum = self.config.get('ors_max_locations', 12)
        if type(maximum) is not int or not 2 <= maximum <= 16:
            raise ValueError('Routing location limit must be from 2 to 16.')
        points, city_points = [], {}
        def add(record):
            if len(points) >= maximum or any(point.entity_id == record.entity_id for point in points):
                return None
            try:
                point = RouteLocation(entity_id=record.entity_id, name=record.name, city=record.city,
                    latitude=record.latitude, longitude=record.longitude)
            except ValueError:
                return None
            points.append(point)
            return point
        # City-to-city estimates use exact city guide coordinates, never a venue
        # chosen as a substitute for an unknown departure city.
        for destination in self.request.destinations:
            response = self.guide_responses.get(destination)
            for hit in response.results if response else []:
                if hit.source == 'text' and LocationResolver._normalize(hit.name or '') == LocationResolver._normalize(destination):
                    try:
                        point = RouteLocation(entity_id=hit.entity_id, name=hit.name, city=destination,
                            latitude=hit.latitude, longitude=hit.longitude)
                        city_points[destination] = point
                        break
                    except ValueError:
                        continue
        if self.request.origin or len(self.request.destinations) > 1:
            for destination, point in self._indexed_city_points().items():
                city_points.setdefault(destination, point)
        records = list(notebook.records().values()) if notebook else []
        groups = [[record for record in records if record.city == city and record.kind == kind]
                  for kind in ('restaurant', 'attraction', 'accommodation')]
        # Reserve enough meal/attraction coverage for short trips. Accommodation
        # fills spare space; it must not displace the day's scheduled stops.
        days = ((self.request.duration_days or 1) + len(self.request.destinations)-1)//len(self.request.destinations)
        attraction_count = min(len(groups[1]), max(days, min(4, maximum-3*days)))
        for record in groups[0][:max(0, maximum-attraction_count)]:
            add(record)
        for record in groups[1][:attraction_count]:
            add(record)
        # Fill unused space only from retained notebook candidates.
        for index in range(max((len(group) for group in groups), default=0)):
            for group in groups:
                if index < len(group):
                    add(group[index])
        if len(points) < 2 and len(city_points) < 2:
            return ToolResult(tool='DistanceMatrix', city=city, status='unavailable',
                note='Fewer than two indexed locations have valid coordinates. Route times remain unknown.')
        provider = OpenRouteService(timeout=self.config.get('ors_timeout_seconds', 8))
        self.resources.callback(provider.close)
        matrix, intercity, failures = None, None, []
        try:
            if len(points) >= 2:
                matrix = provider.matrix(points, mode=mode)
        except RoutingUnavailable as error:
            failures.append(str(error))
        # A separate small city matrix is shared through the provider cache.
        # Unreachable or over-limit intercity routes cannot erase local evidence.
        city_locations = list({point.entity_id: point for point in city_points.values()}.values())
        city_indices = {point.entity_id: index for index, point in enumerate(city_locations)}
        if len(city_locations) >= 2:
            try:
                intercity = provider.matrix(city_locations, mode=mode)
            except RoutingUnavailable as error:
                failures.append(str(error))
        if matrix is None and intercity is None:
            return ToolResult(tool='DistanceMatrix', city=city, status='unavailable', note=' '.join(dict.fromkeys(failures))[:600])
        pairs = []
        if action and action.origin and action.destination:
            pairs.append((action.origin, action.destination))
        elif city in self.request.destinations:
            index = self.request.destinations.index(city)
            previous = self.request.destinations[index-1] if index else self.request.origin
            following = self.request.destinations[index+1] if index+1 < len(self.request.destinations) else self.request.origin
            pairs = [(previous, city), (city, following)]
        connections = []
        for origin, destination in pairs:
            if intercity is None or not origin or not destination or origin == destination or origin not in city_points or destination not in city_points:
                continue
            a, b = city_indices[city_points[origin].entity_id], city_indices[city_points[destination].entity_id]
            seconds, meters = intercity.durations_seconds[a][b], intercity.distances_meters[a][b]
            if seconds is None or meters is None or seconds > 600000:
                continue
            identity = sha256(f'{city_points[origin].entity_id}|{city_points[destination].entity_id}|{mode}'.encode()).hexdigest()[:32]
            connections.append(EvidenceRecord(entity_id='ors:' + identity, kind='transport', name=f'{origin} to {destination}',
                city=city, provider='OpenRouteService', url=ROUTING_SOURCE_URL, origin=origin, destination=destination,
                mode=mode, duration_minutes=ceil(seconds/60),
                excerpt=f'Estimated city-center route: {ceil(seconds/60)} minutes, {meters/1000:.1f} km; fetched {intercity.fetched_at}. No live traffic, fare or availability evidence.'))
        complete = not failures and (matrix is not None or bool(connections)) and all(
            value is not None for result in (matrix, intercity) if result for rows in (result.durations_seconds, result.distances_meters)
            for row in rows for value in row)
        return ToolResult(tool='DistanceMatrix', city=city, status='available' if complete else 'partial',
            records=connections, route_matrix=matrix,
            note=(f'OpenRouteService {mode} estimates for {len(matrix.locations) if matrix else 0} indexed venues and {len(connections)} city connections. '
                  'Other venues and unreachable pairs remain unknown. No live traffic, fares or booking availability. ' +
                  ' '.join(dict.fromkeys(failures)))[:600])

    def _indexed_city_points(self):
        """Resolve exact city titles/approved aliases from the existing geo index.

        Stream the local files once per trip and retain only its few city points.
        Departure coordinates never come from an LLM or a guessed venue.
        """
        if self._city_references is not None:
            return self._city_references
        from src.schema.routing import RouteLocation
        self._city_references = {}
        names = self.request.destinations + ([self.request.origin] if self.request.origin else [])
        aliases = {}
        for city in names:
            values = [city]
            for canonical, variants in self.config.get('location_aliases', {}).items():
                if LocationResolver._normalize(city) in {LocationResolver._normalize(value) for value in (canonical, *variants)}:
                    values.extend((canonical, *variants))
            for value in values:
                aliases[LocationResolver._normalize(value)] = city
        try:
            pages_path = resolve_path(self.config.get('wikivoyage_pages_path', 'datasets/processed/wikivoyage/pages.jsonl'))
            geo_path = resolve_path(self.config.get('wikivoyage_geo_tags_path', 'datasets/processed/wikivoyage/geo_tags.jsonl'))
            pages = {}
            for page in read_jsonl(pages_path):
                city = aliases.get(LocationResolver._normalize(str(page.get('title') or '')))
                if city and page.get('page_id') is not None:
                    pages[str(page['page_id'])] = (city, page['title'])
            for geo in read_jsonl(geo_path):
                page_id = str(geo.get('page_id'))
                if page_id not in pages:
                    continue
                city, title = pages[page_id]
                try:
                    self._city_references[city] = RouteLocation(entity_id='wikivoyage:' + page_id, name=title, city=city,
                        latitude=geo.get('latitude'), longitude=geo.get('longitude'))
                except ValueError:
                    continue
        except (OSError, ValueError):
            # Missing optional city data leaves intercity journeys unknown.
            self._city_references = {}
        return self._city_references

    def _places_for(self, name, city):
        catalog = self._google()
        if self._places is None:
            self._places = catalog.store.list_places()
        aliases = [city]
        for canonical, values in self.config.get('google_location_aliases', {}).items():
            if LocationResolver._normalize(city) in {LocationResolver._normalize(v) for v in [canonical, *values]}:
                aliases = [canonical, *values]
        kind = {'AttractionSearch': 'attraction', 'RestaurantSearch': 'restaurant', 'AccommodationSearch': 'accommodation'}[name]
        categories = {'attraction': {'attraction', 'museum', 'park', 'garden', 'beach', 'viewpoint', 'market'},
                      'restaurant': {'restaurant', 'cafe'},
                      'accommodation': {'hotel', 'hostel', 'resort', 'guest house', 'guesthouse', 'lodging', 'apartment'}}[kind]
        candidates = [place for place in self._places if metadata_photo_category(place) in categories and
            any(LocationResolver.mentioned(alias, str(place.get(field) or '')) for alias in aliases for field in ('address', 'province'))]
        reference = self._indexed_city_points().get(city) if self.request.transport_mode in (None, 'walking') else None
        from src.resolver.image_distance_resolver import ImageDistanceResolver
        def order(place):
            distance = float('inf')
            if reference:
                from types import SimpleNamespace
                coordinates = ImageDistanceResolver.coordinates(SimpleNamespace(
                    latitude=place.get('latitude'), longitude=place.get('longitude')))
                if coordinates:
                    distance = ImageDistanceResolver.distance_km((reference.latitude, reference.longitude), coordinates)
            return ('google:' + place['place_id'] not in self._preferred_entities,
                    distance if reference else 0, -(place.get('rating') or 0), place['place_id'])
        # Geographical proximity selects a compact walking candidate set. Actual
        # travel times still come exclusively from ORS, never straight-line math.
        candidates.sort(key=order)
        records = []
        for place in candidates[:self.limit]:
            try:
                record = EvidenceRecord(entity_id='google:' + place['place_id'], kind=kind,
                    name=place['place_name'], city=city, provider='Google metadata', url=place.get('google_maps_url'),
                    category=place.get('category'),
                    latitude=place.get('latitude'), longitude=place.get('longitude'), opening_hours=place.get('opening_hours'))
            except ValueError:
                continue
            records.append(record)
            # Gallery identity is the same verified place identity used in the notebook.
            images = catalog.store.get_images_for_place(place['place_id']) if self.route == 'hybrid' else []
            image = images[0] if images else None
            if self.route == 'hybrid':
                self.response.related_results.append(SearchResult(place_id=place['place_id'], score=0, source='image',
                    payload={'place_id': place['place_id']}, entity_id=record.entity_id, name=record.name, url=record.url,
                    latitude=record.latitude, longitude=record.longitude, category=place.get('category'),
                    address=place.get('address'), province=place.get('province'), image=image,
                    metadata={'place': place, 'image': image, 'retrieval_method': 'metadata_lookup'}))
        return ToolResult(tool=name, city=city, status='partial' if records else 'no_results', records=records,
            note='Indexed places, not live availability. Exact cost units, cuisine and room restrictions are not supplied by this dataset.')

    def _benchmark(self, name, city, *, action=None):
        root = resolve_path(self.config.get('planning_benchmark_path', 'datasets/benchmark/travelplanner'))
        paths = {'FlightSearch': 'flights/clean_Flights_2022.csv', 'DistanceMatrix': 'googleDistanceMatrix/distance.csv',
            'RestaurantSearch': 'restaurants/clean_restaurant_2022.csv', 'AttractionSearch': 'attractions/attractions.csv',
            'AccommodationSearch': 'accommodations/clean_accommodations_2022.csv', 'CitySearch': 'background/citySet_with_states.txt'}
        path = root / paths[name]
        if not path.is_file():
            return ToolResult(tool=name, city=city, status='unavailable', note='The TravelPlanner benchmark database for this tool has not been installed.')
        records = []
        if name == 'CitySearch':
            for row in path.read_text(encoding='utf-8').splitlines():
                parts = row.split('\t')
                if len(parts) == 2 and parts[1].casefold() == city.casefold():
                    records.append(EvidenceRecord(entity_id='benchmark:city:' + parts[0], kind='city', name=parts[0],
                        city=parts[0], provider='TravelPlanner benchmark'))
                    if len(records) == self.limit:
                        break
        else:
            def number(value):
                return float(value) if value is not None and re.fullmatch(r'\d+(?:\.\d+)?', str(value).strip()) else None
            with path.open(encoding='utf-8-sig', newline='') as stream:
                for index, row in enumerate(csv.DictReader(stream)):
                    values = {}
                    if name == 'FlightSearch':
                        if 'no_flight' in self.request.transport_constraints or not self.request.origin:
                            break
                        origin = (action.origin if action else None) or (self.request.destinations[-1] if city == self.request.origin else self.request.origin)
                        destination = (action.destination if action else None) or city
                        departure = (action.departure_date if action else None) or (self.request.end_date if city == self.request.origin else self.request.start_date)
                        if (row.get('OriginCityName'), row.get('DestCityName'), row.get('FlightDate')) != (origin, destination, departure):
                            continue
                        kind, label = 'flight', 'Flight ' + row['Flight Number']
                        values = dict(origin=origin, destination=destination, departure_date=row['FlightDate'], mode='flight',
                                      duration_minutes=int(number(row.get('ActualElapsedTime'))) if number(row.get('ActualElapsedTime')) else None,
                                      cost=number(row.get('Price')), currency='USD', cost_unit='per_person')
                    elif name == 'DistanceMatrix':
                        origin = (action.origin if action else None) or self.request.origin or city
                        destination = (action.destination if action else None) or city
                        if (row.get('origin'), row.get('destination')) != (origin, destination):
                            continue
                        kind, label = 'transport', f'{origin} to {destination}'
                        duration = re.fullmatch(r'(?:(\d+) hours?\s*)?(?:(\d+) mins?)?', row.get('duration', '').strip())
                        minutes = (int(duration[1] or 0)*60 + int(duration[2] or 0)) if duration and duration[0] else None
                        values = dict(origin=origin, destination=destination,
                                      mode=(action.mode if action else None) or self.request.transport_mode or 'driving', duration_minutes=minutes)
                    else:
                        key = 'city' if name == 'AccommodationSearch' else 'City'
                        if row.get(key, '').casefold() != city.casefold():
                            continue
                        kind = {'RestaurantSearch': 'restaurant', 'AttractionSearch': 'attraction', 'AccommodationSearch': 'accommodation'}[name]
                        label = row.get('Name') or row.get('NAME')
                        if kind == 'restaurant':
                            values = dict(cost=number(row.get('Average Cost')), currency='USD', cost_unit='per_person',
                                          cuisines=[v.strip() for v in row.get('Cuisines', '').split(',') if v.strip()] or None)
                        elif kind == 'accommodation':
                            values = dict(cost=number(row.get('price')), currency='USD', cost_unit='per_night', room_type=row.get('room type'),
                                room_rules=[v.strip() for v in row.get('house_rules', '').split(',') if v.strip()] or None,
                                minimum_nights=int(number(row.get('minimum nights'))) if number(row.get('minimum nights')) else None,
                                maximum_occupancy=int(number(row.get('maximum occupancy'))) if number(row.get('maximum occupancy')) else None)
                    try:
                        records.append(EvidenceRecord(entity_id=f'benchmark:{name}:{index}', kind=kind, name=label,
                            city=city, provider='TravelPlanner benchmark', **values))
                    except ValueError:
                        continue
                    if len(records) == self.limit:
                        break
        return ToolResult(tool=name, city=city, status='available' if records else 'no_results', records=records,
            note='Static TravelPlanner benchmark records, not current travel availability. Road costs are not inferred from distance.')
