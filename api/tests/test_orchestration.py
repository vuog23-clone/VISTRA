"""TravelPlanner-style adaptive selection, evidence boundaries and repair checks."""

from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from src.planning.orchestrator import TravelOrchestrator
from src.planning.planner import Planner
from src.schema.planning import ToolResult, TripRequest
from src.schema.weather import iso_date
from src.utils.utils import load_config


class EmptyTools:
    def __init__(self, *, fail=None):
        self.calls = []
        self.fail = fail

    def run(self, name, city, **options):
        self.calls.append((name, city, options.get('action')))
        if name == self.fail:
            raise TimeoutError('Unavailable test provider')
        return ToolResult(tool=name, city=city, status='no_results', note='No indexed evidence.')


def empty_days(request):
    return {'days': [{'day': i+1, 'city': request.destinations[min(i, len(request.destinations)-1)],
        'date': (iso_date(request.start_date)+timedelta(days=i)).isoformat() if request.start_date else None}
        for i in range(request.duration_days)]}


def next_required(request, notebook, remaining):
    return {'tool': remaining[0][0], 'city': remaining[0][1]} if remaining else {'tool': 'Planner'}


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.config['weather_enabled'] = False

    def test_llm_selects_optional_tools_from_notebook_observations(self):
        request = TripRequest(destinations=['Hanoi'], duration_days=1)
        observed = []

        def select(request, notebook, remaining):
            observed.append([entry.tool for entry in notebook.entries])
            if len(notebook.entries) == 1:
                return {'tool': 'CitySearch', 'city': 'Hanoi'}
            if ('RestaurantSearch', 'Hanoi') in remaining:
                return {'tool': 'RestaurantSearch', 'city': 'Hanoi'}
            return next_required(request, notebook, remaining)

        planner = SimpleNamespace(next_action=Mock(side_effect=select),
                                  run=Mock(return_value=empty_days(request)))
        tools, events = EmptyTools(), []
        result = TravelOrchestrator(config=self.config, planner=planner, tools=tools).run(
            'Create a one-day itinerary', request, on_progress=events.append)
        self.assertEqual([name for name, _, _ in tools.calls[:3]],
                         ['GuideSearch', 'CitySearch', 'RestaurantSearch'])
        self.assertEqual(planner.next_action.call_count, 6)
        self.assertEqual(observed[0], ['GuideSearch'])
        self.assertEqual(observed[1], ['GuideSearch', 'CitySearch'])
        planner.run.assert_called_once()
        self.assertEqual(result['planning']['status'], 'partial')
        self.assertTrue(any(e['step'] == 'planning_select' for e in events))
        self.assertFalse(any(e.get('selection_source') == 'code' for e in events))

    def test_provider_selection_receives_request_notebook_and_remaining_tools(self):
        request = TripRequest(destinations=['Hanoi'], duration_days=1)
        from src.planning.notebook import Notebook
        from src.schema.planning import ToolAction
        book = Notebook()
        book.write(ToolResult(tool='GuideSearch', city='Hanoi', status='no_results', note='No guide found.'))
        parse = Mock(return_value=SimpleNamespace(status='completed',
            output_parsed=ToolAction(tool='RestaurantSearch', city='Hanoi')))
        planner = Planner(config=self.config, client=SimpleNamespace(responses=SimpleNamespace(parse=parse)))
        selected = planner.next_action(request, book.data, [('RestaurantSearch', 'Hanoi')])
        self.assertEqual(selected.tool, 'RestaurantSearch')
        payload = json.loads(parse.call_args.kwargs['input'])
        self.assertEqual(payload['notebook']['entries'][0]['status'], 'no_results')
        self.assertEqual(payload['remaining_required'], [['RestaurantSearch', 'Hanoi']])
        self.assertEqual(payload['request']['destinations'], ['Hanoi'])

    def test_routing_prerequisites_override_premature_llm_selection(self):
        request = TripRequest(destinations=['Hanoi', 'Da Nang'], duration_days=2)

        def select(request, notebook, remaining):
            if ('DistanceMatrix', 'Hanoi') in remaining:
                return {'tool': 'DistanceMatrix', 'city': 'Hanoi'}
            return next_required(request, notebook, remaining)

        planner = SimpleNamespace(next_action=Mock(side_effect=select),
                                  run=Mock(return_value=empty_days(request)))
        tools = EmptyTools()
        result = TravelOrchestrator(config=self.config, planner=planner, tools=tools).run('Plan two days', request)
        completed = set()
        for name, city, _ in tools.calls:
            if name == 'DistanceMatrix':
                self.assertTrue({('GuideSearch', c) for c in request.destinations} <= completed)
                self.assertTrue({(tool, city) for tool in
                    ('AttractionSearch', 'RestaurantSearch', 'AccommodationSearch')} <= completed)
            completed.add((name, city))
        self.assertEqual(len(completed), 10)
        self.assertEqual(result['planning']['status'], 'partial')

    def test_rejects_out_of_scope_llm_actions_before_tool_execution(self):
        request = TripRequest(destinations=['Hanoi'], duration_days=1)
        planner = SimpleNamespace(next_action=Mock(side_effect=[
            {'tool': 'CitySearch', 'city': 'Tokyo'},
            {'tool': 'DistanceMatrix', 'city': 'Hanoi', 'origin': 'Paris'}]),
            run=Mock(return_value=empty_days(request)))
        tools = EmptyTools()
        result = TravelOrchestrator(config=self.config, planner=planner, tools=tools).run('Plan a day', request)
        self.assertEqual([name for name, _, _ in tools.calls], ['GuideSearch'])
        self.assertEqual(sum('Rejected tool call' in line for line in result['planning']['tool_trace']), 2)

    def test_repeated_llm_actions_are_bounded(self):
        request = TripRequest(destinations=['Hanoi'], duration_days=1)
        planner = SimpleNamespace(next_action=Mock(return_value={'tool': 'GuideSearch', 'city': 'Hanoi'}),
                                  run=Mock(return_value=empty_days(request)))
        tools = EmptyTools()
        result = TravelOrchestrator(config=self.config, planner=planner, tools=tools).run('Plan a day', request)
        self.assertEqual(planner.next_action.call_count, 2)
        self.assertEqual([name for name, _, _ in tools.calls], ['GuideSearch', 'AttractionSearch'])
        self.assertTrue(any('Stopped repeated' in line for line in result['planning']['tool_trace']))

    def test_tool_timeout_stays_in_notebook_and_does_not_repeat(self):
        request = TripRequest(destinations=['Hanoi'], duration_days=1)
        tools = EmptyTools(fail='RestaurantSearch')
        planner = SimpleNamespace(next_action=Mock(side_effect=next_required), run=Mock(return_value=empty_days(request)))
        result = TravelOrchestrator(config=self.config, planner=planner, tools=tools).run('Plan a day', request)
        entry = next(e for e in result['notebook']['entries'] if e['tool'] == 'RestaurantSearch')
        self.assertEqual(entry['status'], 'unavailable')
        self.assertEqual(entry['records'], [])
        self.assertEqual(sum(name == 'RestaurantSearch' for name, _, _ in tools.calls), 1)
        self.assertTrue(any(i['constraint'] == 'tool_availability' and 'RestaurantSearch' in i['message']
                            for i in result['planning']['issues']))

    def test_step_budget_reports_unattempted_tools(self):
        self.config.update(planning_max_steps=1, weather_enabled=True)
        request = TripRequest(destinations=['Hanoi'], duration_days=1)
        planner = SimpleNamespace(next_action=Mock(side_effect=next_required), run=Mock(return_value=empty_days(request)))
        tools = EmptyTools()
        result = TravelOrchestrator(config=self.config, planner=planner, tools=tools).run('Plan a day', request)
        self.assertEqual([name for name, _, _ in tools.calls], ['GuideSearch'])
        missed = [i for i in result['planning']['issues'] if 'step budget' in i['message']]
        self.assertEqual(len(missed), 5)
        self.assertEqual(result['planning']['status'], 'partial')

    def test_weather_can_be_disabled_per_turn(self):
        self.config['weather_enabled'] = True
        request = TripRequest(destinations=['Hanoi'], duration_days=1)
        for enabled in (True, False):
            tools = EmptyTools()
            planner = SimpleNamespace(next_action=Mock(side_effect=next_required), run=Mock(return_value=empty_days(request)))
            TravelOrchestrator(config=self.config, planner=planner, tools=tools).run(
                'Plan a day', request, include_weather=enabled)
            self.assertEqual(any(name == 'WeatherSearch' for name, _, _ in tools.calls), enabled)

    def test_configured_repairs_are_bounded(self):
        self.config['planning_max_repairs'] = 2
        request = TripRequest(destinations=['Hanoi'], duration_days=1)
        calls = []

        def parse(**options):
            calls.append(json.loads(options['input']))
            return SimpleNamespace(status='completed', output_parsed=options['text_format'].model_validate(
                {'days': [{'day': 2, 'city': 'Hanoi'}]}))

        planner = Planner(config=self.config, client=SimpleNamespace(responses=SimpleNamespace(parse=parse)))
        planner.next_action = Mock(side_effect=next_required)
        result = TravelOrchestrator(config=self.config, planner=planner, tools=EmptyTools()).run('Plan a day', request)
        self.assertEqual(len(calls), 3)
        self.assertIsNotNone(calls[1]['rejected_draft'])
        self.assertEqual(result['planning']['repair_attempts'], 2)
        self.assertEqual(result['planning']['status'], 'failed')
        self.assertIsNone(result['itinerary'])

    def test_code_repair_avoids_a_second_planner_llm_call(self):
        from api.tests.test_planning_repair import three_day_fixture
        book, request, _ = three_day_fixture()

        class Tools:
            def run(self, name, city, **options):
                entry = next((e for e in book.data.entries if e.tool == name), None)
                return ToolResult(**entry.model_dump(exclude={'entry_id'})) if entry else ToolResult(
                    tool=name, city=city, status='no_results', note='No evidence.')

        def parse(**options):
            records = [r for entry in json.loads(options['input'])['notebook']['entries'] for r in entry.get('records', [])]
            meals = [r['evidence_id'] for r in records if r['kind'] == 'restaurant']
            sight = next(r['evidence_id'] for r in records if r['kind'] == 'attraction')
            data = empty_days(request)
            for day in data['days']:
                day.update(breakfast={'evidence_id': meals[0], 'start_time': '08:00', 'end_time': '09:00'},
                           attractions=[{'evidence_id': sight, 'start_time': '09:05', 'end_time': '11:00'}],
                           lunch={'evidence_id': meals[1], 'start_time': '12:00', 'end_time': '13:00'},
                           dinner={'evidence_id': meals[2], 'start_time': '18:00', 'end_time': '19:00'})
            return SimpleNamespace(status='completed', output_parsed=options['text_format'].model_validate(data))

        client = SimpleNamespace(responses=SimpleNamespace(parse=Mock(side_effect=parse)))
        planner = Planner(config=self.config, client=client)
        planner.next_action = Mock(side_effect=next_required)
        result = TravelOrchestrator(config=self.config, planner=planner, tools=Tools()).run('Plan three days', request)
        client.responses.parse.assert_called_once()
        self.assertIsNotNone(result['itinerary'])
        self.assertEqual(result['planning']['repair_attempts'], 1)
        self.assertFalse(any(i['severity'] == 'error' for i in result['planning']['issues']))

    def test_scoped_follow_up_keeps_unrequested_days_and_venue_identities(self):
        from api.tests.test_planning_repair import three_day_fixture
        from src.planning.scheduling import repair_schedule
        from src.planning.validation import bind_sources
        book, request, draft = three_day_fixture()
        previous = bind_sources(repair_schedule(draft, request, book.data), book.data)

        class Tools:
            def run(self, name, city, **options):
                entry = next((e for e in book.data.entries if e.tool == name), None)
                return ToolResult(**entry.model_dump(exclude={'entry_id'})) if entry else ToolResult(
                    tool=name, city=city, status='no_results', note='No evidence.')

        def parse(**options):
            entries = json.loads(options['input'])['notebook']['entries']
            ids = {r['entity_id']: r['evidence_id'] for e in entries for r in e.get('records', [])}
            proposed = previous.model_copy(deep=True)
            for day in proposed.days:
                for _, choice in day.choices():
                    choice.evidence_id = ids[choice.entity_id]
                    choice.entity_id = choice.name = None
                day.dinner.start_time, day.dinner.end_time = '19:00', '20:00'
            return SimpleNamespace(status='completed', output_parsed=options['text_format'].model_validate(
                proposed.model_dump(exclude_none=True)))

        client = SimpleNamespace(responses=SimpleNamespace(parse=Mock(side_effect=parse)))
        planner = Planner(config=self.config, client=client)
        planner.next_action = Mock(side_effect=next_required)
        result = TravelOrchestrator(config=self.config, planner=planner, tools=Tools()).run(
                'Change only day 2 dinner to 19:00; keep the same venues.', request,
                previous=previous, action='revise_itinerary', revision_day=2)
        client.responses.parse.assert_called_once()
        self.assertIsNotNone(result['itinerary'])
        for old, new in zip(previous.days, result['itinerary']['days']):
            self.assertEqual(old.dinner.entity_id, new['dinner']['entity_id'])
            self.assertEqual(new['dinner']['start_time'], '19:00' if old.day == 2 else old.dinner.start_time)
        self.assertFalse(any(i['severity'] == 'error' for i in result['planning']['issues']))

    def test_multiple_day_edit_preserves_the_other_days(self):
        from api.tests.test_planning_repair import three_day_fixture
        from src.planning.scheduling import repair_schedule
        from src.planning.validation import bind_sources
        book, request, draft = three_day_fixture()
        previous = bind_sources(repair_schedule(draft, request, book.data), book.data)

        class Tools:
            def run(self, name, city, **options):
                entry = next((e for e in book.data.entries if e.tool == name), None)
                return ToolResult(**entry.model_dump(exclude={'entry_id'})) if entry else ToolResult(
                    tool=name, city=city, status='no_results', note='No evidence.')

        def parse(**options):
            entries = json.loads(options['input'])['notebook']['entries']
            ids = {r['entity_id']: r['evidence_id'] for e in entries for r in e.get('records', [])}
            proposed = previous.model_copy(deep=True)
            for day in proposed.days:
                for _, choice in day.choices():
                    choice.evidence_id = ids[choice.entity_id]
                    choice.entity_id = choice.name = None
                day.dinner.start_time, day.dinner.end_time = '19:00', '20:00'
            return SimpleNamespace(status='completed', output_parsed=options['text_format'].model_validate(
                proposed.model_dump(exclude_none=True)))

        for query in ('Change only day 2 and day 3 dinner to 19:00; keep day 1 unchanged.',
                      'Change only days 2 and 3 dinner to 19:00; keep day 1 unchanged.'):
            with self.subTest(query=query):
                client = SimpleNamespace(responses=SimpleNamespace(parse=Mock(side_effect=parse)))
                planner = Planner(config=self.config, client=client)
                planner.next_action = Mock(side_effect=next_required)
                result = TravelOrchestrator(config=self.config, planner=planner, tools=Tools()).run(
                    query, request, previous=previous, action='revise_itinerary')
                self.assertIsNotNone(result['itinerary'])
                self.assertEqual(result['itinerary']['days'][0]['dinner']['start_time'],
                                 previous.days[0].dinner.start_time)
                self.assertEqual([day['dinner']['start_time'] for day in result['itinerary']['days'][1:]],
                                 ['19:00', '19:00'])

    def test_literal_edit_days_do_not_include_comparison_or_protected_days(self):
        from src.schema.planning import revision_days_from_text
        cases = {
            'Change day 2 to be like day 1; keep day 3 unchanged.': {2},
            'Change days 2 and 3 dinner; keep day 1 unchanged.': {2, 3},
            'Change day 2 dinner and day 3 lunch.': {2, 3},
            'Change days 2 to 4; keep day 1 unchanged.': {2, 3, 4},
            'Sửa ngày 2 và ngày 3; giữ ngày 1.': {2, 3},
            'Day 2, please.': {2},
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(revision_days_from_text(text), expected)

    def test_invalid_edit_day_asks_before_running_any_tools(self):
        from api.tests.test_planning_repair import three_day_fixture
        _, request, previous = three_day_fixture()
        for value in (0, 4, 99):
            tools = EmptyTools()
            planner = SimpleNamespace(run=Mock(), next_action=Mock())
            result = TravelOrchestrator(config=self.config, planner=planner, tools=tools).run(
                f'Change day {value} dinner.', request, previous=previous, action='revise_itinerary')
            self.assertEqual(result['pipeline_status'], 'needs_clarification')
            self.assertEqual(tools.calls, [])
            planner.run.assert_not_called()

    def test_notebook_overflow_keeps_a_partial_result_instead_of_crashing(self):
        from unittest.mock import patch
        from src.planning.notebook import Notebook
        original = Notebook.write
        def write(book, result):
            if result.tool == 'RestaurantSearch':
                raise ValueError('No room for observation or placeholder')
            return original(book, result)
        request = TripRequest(destinations=['Hanoi'], duration_days=1)
        planner = SimpleNamespace(next_action=Mock(side_effect=next_required), run=Mock(return_value=empty_days(request)))
        with patch.object(Notebook, 'write', write):
            result = TravelOrchestrator(config=self.config, planner=planner, tools=EmptyTools()).run('Plan a day', request)
        self.assertIsNotNone(result['itinerary'])
        self.assertEqual(result['planning']['status'], 'partial')
        self.assertTrue(any(i['constraint'] == 'evidence_budget' for i in result['planning']['issues']))


if __name__ == '__main__':
    unittest.main()
