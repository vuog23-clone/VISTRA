"""Provider boundaries and route-aware itinerary checks, without network calls."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from src.planning.notebook import Notebook
from src.planning.orchestrator import TravelOrchestrator, render_itinerary
from src.planning.tools import TravelTools
from src.planning.validation import validate_itinerary
from src.schema.models import SearchResponse, SearchResult
from src.schema.planning import EvidenceRecord, Itinerary, PlanningReport, ToolResult, TripRequest
from src.schema.routing import RouteLocation, RouteMatrixEvidence, ROUTING_SOURCE_URL
from src.security.context import answer_safety_issues, prepare_context
from src.service.routing_service import MATRIX_URL, OpenRouteService, RoutingUnavailable, route_for_pair
from src.utils.reply_sources import reply_sources
from src.utils.utils import load_config


def location(index):
    return RouteLocation(entity_id=f'google:{index}', name=f'Place {index}', city='Hanoi',
                         latitude=21.02 + index/10000, longitude=105.85)


def matrix(points, **changes):
    size = len(points)
    data = dict(mode='walking', profile='foot-walking', fetched_at=datetime.now(timezone.utc).isoformat(),
                locations=points, durations_seconds=[[0 if a == b else 600 for b in range(size)] for a in range(size)],
                distances_meters=[[0 if a == b else 800 for b in range(size)] for a in range(size)])
    return RouteMatrixEvidence(**{**data, **changes})


def fixture():
    book = Notebook()
    meals = [EvidenceRecord(**location(i).model_dump(), kind='restaurant', provider='Google metadata') for i in range(3)]
    attraction = EvidenceRecord(**location(3).model_dump(), kind='attraction', provider='Google metadata')
    book.write(ToolResult(tool='RestaurantSearch', city='Hanoi', status='partial', records=meals, note='Indexed'))
    book.write(ToolResult(tool='AttractionSearch', city='Hanoi', status='partial', records=[attraction], note='Indexed'))
    entry = book.write(ToolResult(tool='DistanceMatrix', city='Hanoi', status='available',
                                  route_matrix=matrix([location(i) for i in range(4)]), note='Estimates'))
    plan = Itinerary.model_validate({'days': [{'day': 1, 'city': 'Hanoi',
        'breakfast': {'evidence_id': 'E1R1', 'start_time': '08:00', 'end_time': '09:00'},
        'attractions': [{'evidence_id': 'E2R1', 'start_time': '09:15', 'end_time': '11:00'}],
        'lunch': {'evidence_id': 'E1R2', 'start_time': '12:00', 'end_time': '13:00'},
        'dinner': {'evidence_id': 'E1R3', 'start_time': '18:00', 'end_time': '19:00'}}]})
    return book, entry, plan, TripRequest(destinations=['Hanoi'], duration_days=1, transport_mode='walking')


class ProviderTests(unittest.TestCase):
    @unittest.skipUnless(os.getenv('VISTRA_TEST_ORS') == '1', 'Enable a real ORS call explicitly')
    def test_live_hanoi_matrix(self):
        points = [RouteLocation(entity_id='google:live-test-lake', name='Hoan Kiem Lake', city='Hanoi',
                                latitude=21.0285, longitude=105.852),
                  RouteLocation(entity_id='google:live-test-cathedral', name='St Joseph Cathedral', city='Hanoi',
                                latitude=21.0289, longitude=105.849)]
        provider = OpenRouteService(use_cache=False)
        try:
            result = provider.matrix(points, mode='walking')
            self.assertIsNotNone(result.durations_seconds[0][1])
            self.assertGreater(result.distances_meters[0][1], 0)
        finally:
            provider.close()

    def test_request_profiles_units_coordinates_and_cache_isolation(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={'durations': [[0, 61], [121, 0]], 'distances': [[0, 100], [200, 0]]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            provider = OpenRouteService(api_key='test-key-coordinate-cache', client=client)
            points = [location(0), location(1)]
            result = provider.matrix(points, mode='taxi')
            self.assertEqual(str(calls[0].url), MATRIX_URL + 'driving-car')
            self.assertEqual(calls[0].headers['Authorization'], 'test-key-coordinate-cache')
            self.assertEqual(json.loads(calls[0].content), {'locations': [[105.85, 21.02], [105.85, 21.0201]],
                             'metrics': ['duration', 'distance'], 'units': 'm'})
            result.durations_seconds[0][1] = 999
            self.assertEqual(provider.matrix(points, mode='taxi').durations_seconds[0][1], 61)
            self.assertEqual(len(calls), 1)
            provider.matrix(points, mode='walking')
            self.assertEqual(str(calls[1].url), MATRIX_URL + 'foot-walking')
            changed = [point.model_copy(deep=True) for point in points]
            changed[0].latitude += .000001
            provider.matrix(changed, mode='walking')
            self.assertEqual(len(calls), 3)

    def test_missing_key_and_transit_never_call_provider(self):
        def handler(_):
            self.fail('Unsupported requests must not contact ORS')
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            for key, mode in (('', 'walking'), ('key', 'public_transit')):
                with self.subTest(mode=mode), self.assertRaises(RoutingUnavailable):
                    OpenRouteService(api_key=key, client=client).matrix([location(0), location(1)], mode=mode)

    def test_provider_failures_are_redacted_without_retry_or_redirect(self):
        for status in (401, 403, 429, 500, 302):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, headers={'location': 'https://example.com/steal'},
                                      json={'error': 'credential-super-secret'})
            with self.subTest(status=status), httpx.Client(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(RoutingUnavailable) as error:
                    OpenRouteService(api_key='credential-super-secret', client=client, use_cache=False).matrix(
                        [location(0), location(1)], mode='walking')
                self.assertNotIn('credential-super-secret', str(error.exception))
                self.assertEqual(len(calls), 1)

    def test_invalid_and_oversized_payloads_fail_closed(self):
        payloads = [b'not json', b' ' * 131073,
            json.dumps({'durations': [[0, True], [1, 0]], 'distances': [[0, 1], [1, 0]]}).encode(),
            json.dumps({'durations': [[0, -1], [1, 0]], 'distances': [[0, 1], [1, 0]]}).encode(),
            b'{"durations":[[0,NaN],[1,0]],"distances":[[0,1],[1,0]]}',
            b'{"durations":[[0,1]],"distances":[[0,1]]}']
        for payload in payloads:
            with self.subTest(size=len(payload)), httpx.Client(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=payload, headers={'content-type': 'application/json'}))) as client:
                with self.assertRaises(RoutingUnavailable):
                    OpenRouteService(api_key='test', client=client, use_cache=False).matrix(
                        [location(0), location(1)], mode='walking')

    def test_null_cells_preserve_unreachable_routes(self):
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200,
            json={'durations': [[0, None], [120, 0]], 'distances': [[0, None], [200, 0]]}))) as client:
            result = OpenRouteService(api_key='test', client=client, use_cache=False).matrix(
                [location(0), location(1)], mode='driving')
            self.assertIsNone(result.durations_seconds[0][1])
            self.assertEqual(result.durations_seconds[1][0], 120)

    def test_invalid_coordinates_and_provenance_are_rejected(self):
        for latitude in (True, float('inf'), 91):
            with self.subTest(latitude=latitude), self.assertRaises(ValueError):
                RouteLocation(**{**location(0).model_dump(), 'latitude': latitude})
        for changes in ({'profile': 'driving-car'}, {'fetched_at': '2026-10-13T00:00:00'},
                        {'locations': [location(0), location(0)]}, {'url': 'https://example.com'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                matrix([location(0), location(1)], **changes)


class PipelineTests(unittest.TestCase):
    @unittest.skipUnless(all(os.getenv(flag) == '1' for flag in
        ('VISTRA_TEST_LIVE', 'VISTRA_TEST_ORS', 'VISTRA_TEST_POSTGRES')), 'Enable live route-aware planning explicitly')
    def test_live_planner_with_indexed_routes(self):
        from src.planning.planner import Planner
        request = TripRequest(destinations=['Hanoi'], duration_days=1, transport_mode='walking')
        config = load_config()
        with ExitStack() as resources:
            tools = TravelTools(request, 'Plan one walking day in Hanoi', resources=resources, config=config)
            book = Notebook()
            for name in ('AttractionSearch', 'RestaurantSearch', 'AccommodationSearch'):
                book.write(tools.run(name, 'Hanoi'))
            book.write(tools.run('DistanceMatrix', 'Hanoi', notebook=book.data))
            planner = resources.enter_context(Planner(config=config))
            query = 'Plan one walking day in Hanoi with breakfast, attractions, lunch and dinner. Prefer stops covered by the route matrix and allow enough walking time.'
            plan = planner.run(query, request, book.data)
            issues = validate_itinerary(plan, request, book.data)[0]
            errors = [issue for issue in issues if issue.severity == 'error']
            if errors:
                plan = planner.run(query, request, book.data, draft=plan, errors=errors)
                issues = validate_itinerary(plan, request, book.data)[0]
            self.assertFalse(any(issue.severity == 'error' for issue in issues),
                             '; '.join(issue.message for issue in issues if issue.severity == 'error'))
            answer = render_itinerary(plan, PlanningReport(status='partial', action='create_itinerary', issues=issues[:80]),
                                      book.data, query, request)
            self.assertIn('Estimated travel between stops', answer)
            context = prepare_context({'route': 'text', 'sources': [], 'notebook': book.list_all()})
            self.assertEqual(answer_safety_issues(answer, context), [])

    @unittest.skipUnless(os.getenv('VISTRA_TEST_ORS') == '1' and os.getenv('VISTRA_TEST_POSTGRES') == '1',
                         'Enable real indexed-place routing explicitly')
    def test_live_indexed_hanoi_venues(self):
        request = TripRequest(destinations=['Hanoi'], duration_days=1, transport_mode='walking')
        with ExitStack() as resources:
            tools = TravelTools(request, 'Plan a walking day in Hanoi', resources=resources, config=load_config())
            book = Notebook()
            for name in ('AttractionSearch', 'RestaurantSearch', 'AccommodationSearch'):
                book.write(tools.run(name, 'Hanoi'))
            result = tools.run('DistanceMatrix', 'Hanoi', notebook=book.data)
            self.assertIsNotNone(result.route_matrix, result.note)
            self.assertGreater(len(result.route_matrix.locations), 1)
            identities = {record.entity_id for record in book.data.records().values()}
            self.assertTrue(all(point.entity_id in identities for point in result.route_matrix.locations))
            self.assertTrue(any(value is not None and value > 0 for row in result.route_matrix.durations_seconds for value in row))

    def test_directed_estimate_checks_gap_and_preserves_other_unknowns(self):
        book, entry, plan, request = fixture()
        entry.route_matrix.durations_seconds[3][0] = 50000  # reverse is different
        issues = validate_itinerary(plan, request, book.data)[0]
        self.assertFalse(any(issue.constraint == 'transport_time' for issue in issues))
        self.assertTrue(any(issue.constraint == 'budget' for issue in issues))
        plan.days[0].attractions[0].start_time = '09:05'
        errors = validate_itinerary(plan, request, book.data)[0]
        self.assertTrue(any(issue.constraint == 'transport_time' and issue.severity == 'error' for issue in errors))

    def test_schedule_repair_accounts_for_known_routes_and_keeps_venues(self):
        from src.planning.scheduling import repair_schedule
        book, entry, plan, request = fixture()
        entry.route_matrix.durations_seconds[0][3] = 2700
        plan.days[0].attractions[0].start_time = '09:05'
        repaired = repair_schedule(plan, request, book.data, preserve_venues=True)
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.days[0].attractions[0].start_time, '09:45')
        self.assertEqual(repaired.days[0].attractions[0].evidence_id, 'E2R1')
        self.assertFalse(any(issue.severity == 'error' for issue in validate_itinerary(repaired, request, book.data)[0]))

    def test_planner_schema_uses_covered_candidates_only_when_sufficient(self):
        from src.planning.planner import draft_schema
        book, entry, _, request = fixture()
        book.write(ToolResult(tool='RestaurantSearch', city='Hanoi', status='partial', note='Extra candidate',
            records=[EvidenceRecord(**location(9).model_dump(), kind='restaurant', provider='Google metadata')]))
        schema = draft_schema(book.data, request).model_json_schema()
        encoded = json.dumps(schema)
        self.assertNotIn('E4R1', encoded)
        entry.route_matrix = None
        self.assertIn('E4R1', json.dumps(draft_schema(book.data, request).model_json_schema()))

    def test_intercity_records_use_exact_city_points_without_inferred_fares(self):
        request = TripRequest(destinations=['Hanoi', 'Hue'], duration_days=2, transport_mode='driving')
        with ExitStack() as resources:
            tools = TravelTools(request, 'Plan a trip', resources=resources, config={})
            for index, city in enumerate(request.destinations):
                hit = SearchResult(place_id=city, entity_id='wikivoyage:' + city, name=city, source='text',
                                   score=1, payload={}, latitude=21-index*4, longitude=105)
                tools.guide_responses[city] = SearchResponse(route='text', results=[hit])
            provider = SimpleNamespace(matrix=lambda points, mode: matrix(points, mode=mode, profile='driving-car'), close=lambda: None)
            with patch('src.service.routing_service.OpenRouteService', return_value=provider):
                result = tools.run('DistanceMatrix', 'Hanoi', notebook=Notebook().data)
            self.assertEqual(len(result.records), 1)
            record = result.records[0]
            self.assertEqual((record.origin, record.destination, record.mode), ('Hanoi', 'Hue', 'driving'))
            self.assertEqual(record.provider, 'OpenRouteService')
            self.assertEqual(record.duration_minutes, 10)
            self.assertIsNone(record.cost)
            self.assertIn('Estimated city-center', record.excerpt)
            book = Notebook(city_count=2)
            book.write(result)
            prepare_context({'route': 'text', 'sources': [], 'notebook': book.list_all()})

    def test_failed_intercity_matrix_does_not_erase_local_routes(self):
        book, _, _, request = fixture()
        request.destinations = ['Hanoi', 'Hue']
        with ExitStack() as resources:
            tools = TravelTools(request, 'Plan a trip', resources=resources, config={})
            tools._city_references = {'Hanoi': location(10), 'Hue': location(11).model_copy(update={'city': 'Hue'})}
            calls = []
            def fetch(points, *, mode):
                calls.append(points)
                if len(calls) == 2:
                    raise RoutingUnavailable('Intercity route unavailable.')
                return matrix(points)
            with patch('src.service.routing_service.OpenRouteService',
                       return_value=SimpleNamespace(matrix=fetch, close=lambda: None)):
                result = tools.run('DistanceMatrix', 'Hanoi', notebook=book.data)
            self.assertIsNotNone(result.route_matrix)
            self.assertEqual(result.status, 'partial')
            self.assertEqual(result.records, [])
            self.assertIn('Intercity route unavailable', result.note)

    def test_departure_city_coordinates_come_from_exact_indexed_titles(self):
        request = TripRequest(destinations=['Hue'], origin='Hanoi', duration_days=2)
        with ExitStack() as resources:
            tools = TravelTools(request, 'Plan a trip', resources=resources, config={})
            def rows(path):
                if path.name == 'pages.jsonl':
                    return iter([{'page_id': 1, 'title': 'Hanoi'}, {'page_id': 2, 'title': 'Hue'},
                                 {'page_id': 3, 'title': 'Hanoi/Old Quarter'}])
                return iter([{'page_id': 1, 'latitude': 21, 'longitude': 105},
                             {'page_id': 2, 'latitude': 16, 'longitude': 107},
                             {'page_id': 3, 'latitude': 22, 'longitude': 106}])
            with patch('src.planning.tools.read_jsonl', side_effect=rows) as loader:
                points = tools._indexed_city_points()
                self.assertEqual(points['Hanoi'].entity_id, 'wikivoyage:1')
                self.assertEqual(points['Hanoi'].latitude, 21)
                self.assertEqual(points['Hue'].entity_id, 'wikivoyage:2')
                self.assertIs(tools._indexed_city_points(), points)
                self.assertEqual(loader.call_count, 2)

    def test_walking_candidates_favor_nearby_places_and_preserve_saved_venues(self):
        request = TripRequest(destinations=['Hanoi'], duration_days=1, transport_mode='walking')
        previous = Itinerary.model_validate({'days': [{'day': 1, 'city': 'Hanoi',
            'breakfast': {'evidence_id': 'E1R1', 'entity_id': 'google:far'}}]})
        places = [{'place_id': 'far', 'place_name': 'Far cafe', 'category': 'restaurant', 'province': 'Hanoi',
                   'rating': 5, 'latitude': 22, 'longitude': 105.85},
                  {'place_id': 'near', 'place_name': 'Nearby cafe', 'category': 'restaurant', 'province': 'Hanoi',
                   'rating': 4, 'latitude': 21.0201, 'longitude': 105.85}]
        with ExitStack() as resources:
            for saved, expected in ((None, 'google:near'), (previous, 'google:far')):
                tools = TravelTools(request, 'Plan a walking trip', resources=resources, config={}, limit=1, previous=saved)
                tools._city_references = {'Hanoi': location(0)}
                tools._catalog = SimpleNamespace(store=SimpleNamespace(list_places=lambda: places))
                self.assertEqual(tools.run('RestaurantSearch', 'Hanoi').records[0].entity_id, expected)

    def test_missing_null_stale_mismatched_modes_or_coordinates_stay_unknown(self):
        for change in ('null', 'stale', 'future', 'mode', 'coordinates', 'missing'):
            book, entry, plan, request = fixture()
            if change == 'null':
                entry.route_matrix.durations_seconds[0][3] = None
            elif change in ('stale', 'future'):
                delta = timedelta(hours=-1 if change == 'stale' else 1)
                entry.route_matrix.fetched_at = (datetime.now(timezone.utc) + delta).isoformat()
            elif change == 'mode':
                request.transport_mode = 'public_transit'
            elif change == 'coordinates':
                entry.route_matrix.locations[0].longitude += .01
            else:
                entry.route_matrix = None
            with self.subTest(change=change):
                self.assertTrue(any(issue.constraint == 'transport_time' and issue.severity == 'unknown'
                                    for issue in validate_itinerary(plan, request, book.data)[0]))

    def test_driving_cannot_satisfy_no_driving_but_taxi_can(self):
        book, entry, plan, request = fixture()
        request.transport_mode = None
        request.transport_constraints = ['no_driving']
        entry.route_matrix.mode, entry.route_matrix.profile = 'driving', 'driving-car'
        a, b = book.data.records()['E1R1'], book.data.records()['E2R1']
        self.assertIsNone(route_for_pair(book.data, a, b, request))
        entry.route_matrix.mode = 'taxi'
        self.assertIsNotNone(route_for_pair(book.data, a, b, request))

    def test_render_sources_and_output_boundary(self):
        book, entry, plan, request = fixture()
        answer = render_itinerary(plan, PlanningReport(status='partial', action='create_itinerary'), book.data, 'Plan a trip', request)
        self.assertIn('~10 min', answer)
        self.assertIn('0.8 km', answer)
        self.assertIn('[E3]', answer)
        context = prepare_context({'route': 'text', 'sources': [], 'notebook': book.list_all()})
        self.assertEqual(answer_safety_issues(answer, context), [])
        source = next(source for source in reply_sources({'context': context, 'notebook': book.list_all(),
                                                        'itinerary': plan.model_dump()}) if source['source_id'] == 'E3')
        self.assertEqual(source['url'], ROUTING_SOURCE_URL)
        self.assertEqual(source['fetched_at'], entry.route_matrix.fetched_at)

    def test_matrix_budget_retains_valid_dimensions_and_context(self):
        book = Notebook(city_count=3, duration_days=3)
        for city in ('Hanoi', 'Hue', 'Da Nang', 'Hanoi'):
            points = [location(i).model_copy(update={'city': city}) for i in range(16)]
            book.write(ToolResult(tool='DistanceMatrix', city=city, status='available', route_matrix=matrix(points), note='Estimates'))
        for entry in book.data.entries:
            if entry.route_matrix:
                RouteMatrixEvidence.model_validate(entry.route_matrix.model_dump())
        prepare_context({'route': 'text', 'sources': [], 'notebook': book.list_all()})
        self.assertLess(len(json.dumps(book.list_all()).encode()), 45000)
        self.assertTrue(any(entry.status == 'partial' for entry in book.data.entries))

    def test_real_tools_and_orchestrator_wait_for_venue_evidence(self):
        config = load_config()
        config['weather_enabled'] = False
        hit = SearchResult(place_id='hanoi', entity_id='wikivoyage:hanoi', name='Hanoi', score=1, source='text',
            payload={'text': 'Hanoi has museums and restaurants.'}, latitude=21.02, longitude=105.85,
            url='https://en.wikivoyage.org/wiki/Hanoi')
        search = SimpleNamespace(search=lambda *args, **kwargs: SearchResponse(route='text', location='Hanoi', results=[hit]))
        kinds = ('restaurant', 'restaurant', 'restaurant', 'attraction', 'hotel')
        places = [{'place_id': str(i), 'place_name': f'Place {i}', 'category': kind,
            'province': 'Hanoi', 'rating': 5, 'latitude': 21.02 + i/10000, 'longitude': 105.85}
                  for i, kind in enumerate(kinds)]
        calls = []
        class Planner:
            def next_action(self, request, notebook, remaining):
                return {'tool': 'DistanceMatrix', 'city': 'Hanoi'}
            def run(self, query, request, notebook, **kwargs):
                self.notebook = notebook
                restaurants = [record.evidence_id for record in notebook.records().values() if record.kind == 'restaurant']
                attraction = next(record.evidence_id for record in notebook.records().values() if record.kind == 'attraction')
                return {'days': [{'day': 1, 'city': 'Hanoi',
                    'breakfast': {'evidence_id': restaurants[0], 'start_time': '08:00', 'end_time': '09:00'},
                    'attractions': [{'evidence_id': attraction, 'start_time': '09:15', 'end_time': '11:00'}],
                    'lunch': {'evidence_id': restaurants[1], 'start_time': '12:00', 'end_time': '13:00'},
                    'dinner': {'evidence_id': restaurants[2], 'start_time': '18:00', 'end_time': '19:00'}}]}
        def provider(request):
            points = json.loads(request.content)['locations']
            calls.append(points)
            return httpx.Response(200, json={'durations': [[60 for _ in points] for _ in points],
                                             'distances': [[100 for _ in points] for _ in points]})
        with ExitStack() as resources, httpx.Client(transport=httpx.MockTransport(provider)) as client:
            request = TripRequest(destinations=['Hanoi'], duration_days=1)
            tools = TravelTools(request, 'Plan a day in Hanoi', resources=resources, search_service=search, config=config)
            tools._catalog = SimpleNamespace(store=SimpleNamespace(list_places=lambda: places))
            planner = Planner()
            with patch('src.service.routing_service.OpenRouteService', side_effect=lambda **kwargs:
                OpenRouteService(api_key='test', client=client, use_cache=False, **kwargs)):
                result = TravelOrchestrator(config=config, planner=planner, tools=tools).run('Plan a day in Hanoi', request)
            self.assertEqual(result['planning']['status'], 'partial')
            self.assertIsNotNone(result['itinerary'])
            entries = planner.notebook.entries
            route_index = next(i for i, entry in enumerate(entries) if entry.tool == 'DistanceMatrix')
            self.assertEqual({entry.tool for entry in entries[:route_index]},
                             {'GuideSearch', 'AttractionSearch', 'RestaurantSearch', 'AccommodationSearch'})
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(calls[0]), 5)
            self.assertFalse(any(issue['constraint'] == 'transport_time' for issue in result['planning']['issues']))
            self.assertIn('~1 min', result['answer'])

    def test_optional_disabled_and_transit_tools_do_not_load_client(self):
        with ExitStack() as resources, patch('src.service.routing_service.OpenRouteService') as provider:
            for enabled, mode in ((False, None), (True, 'public_transit'), (True, 'driving')):
                request = TripRequest(destinations=['Hanoi'], transport_mode=mode, transport_constraints=['no_driving'])
                tools = TravelTools(request, 'Plan a trip', resources=resources, config={'ors_enabled': enabled})
                self.assertEqual(tools.run('DistanceMatrix', 'Hanoi').status, 'unavailable')
            provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()
