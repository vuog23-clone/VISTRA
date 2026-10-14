"""Multi-day repairs must resolve duplicates and route gaps together."""

from datetime import date, timedelta
import os
import unittest

from api.tests.test_routing import location, matrix
from src.planning.notebook import Notebook
from src.planning.planner import draft_schema
from src.planning.scheduling import repair_schedule
from src.planning.validation import validate_itinerary
from src.schema.planning import EvidenceRecord, Itinerary, ToolResult, TripRequest


def three_day_fixture():
    book = Notebook(duration_days=3)
    meals = [EvidenceRecord(**location(i).model_dump(), kind='restaurant', provider='Google metadata') for i in range(9)]
    attractions = [EvidenceRecord(**location(i).model_dump(), kind='attraction', provider='Google metadata') for i in range(9, 12)]
    book.write(ToolResult(tool='RestaurantSearch', city='Hanoi', status='partial', records=meals, note='Indexed'))
    book.write(ToolResult(tool='AttractionSearch', city='Hanoi', status='partial', records=attractions, note='Indexed'))
    book.write(ToolResult(tool='DistanceMatrix', city='Hanoi', status='available',
                         route_matrix=matrix([location(i) for i in range(12)]), note='Estimates'))
    request = TripRequest(destinations=['Hanoi'], duration_days=3, start_date='2026-10-14', end_date='2026-10-16',
                          transport_mode='walking', travelers=2, budget_amount=6000000, currency='VND')
    days = [{'day': i+1, 'date': (date(2026, 10, 14)+timedelta(days=i)).isoformat(), 'city': 'Hanoi',
             'breakfast': {'evidence_id': 'E1R1', 'start_time': '08:00', 'end_time': '09:00'},
             'attractions': [{'evidence_id': 'E2R1', 'start_time': '09:05', 'end_time': '11:00'}],
             'lunch': {'evidence_id': 'E1R2', 'start_time': '12:00', 'end_time': '13:00'},
             'dinner': {'evidence_id': 'E1R3', 'start_time': '18:00', 'end_time': '19:00'}} for i in range(3)]
    return book, request, Itinerary.model_validate({'days': days})


class PlanningRepairTests(unittest.TestCase):
    @unittest.skipUnless(all(os.getenv(flag) == '1' for flag in
        ('VISTRA_TEST_LIVE', 'VISTRA_TEST_ORS', 'VISTRA_TEST_POSTGRES')), 'Enable live three-day planning explicitly')
    def test_live_three_day_pipeline_with_dates_weather_and_routes(self):
        from src.planning.orchestrator import TravelOrchestrator
        from src.utils.utils import load_config
        request = TripRequest(destinations=['Hanoi'], origin='Hanoi', duration_days=3,
            start_date='2026-10-14', end_date='2026-10-16', travelers=2, budget_amount=6000000,
            currency='VND', cuisines=['vegetarian'], room_type='private double room',
            transport_mode='walking', transport_constraints=['no_flight', 'no_driving'],
            preferences=['relaxed pace', 'museums', 'cultural attractions'],
            requirements=['Start around 08:00 and finish activities by 20:00'])
        query = ('Create a 3-day itinerary in Hanoi for two adults, from October 14 to October 16, 2026. '
                 'We are already in Hanoi, so no flights or intercity transportation are needed. '
                 'Our total budget is 6,000,000 VND for both people, excluding flights. '
                 'We prefer vegetarian food, museums, cultural attractions, and walking. '
                 'Include breakfast, lunch, dinner, and attractions each day, plus accommodation '
                 'for two nights in a private double room near the Old Quarter. Start activities '
                 'around 08:00 and finish by 20:00. Include weather and estimated walking times '
                 'between stops. Use sourced places and clearly identify unknown prices, availability, or dietary information.')
        result = TravelOrchestrator(config=load_config()).run(query, request, route='hybrid', include_weather=True)
        self.assertIsNotNone(result['itinerary'], '; '.join(issue['message'] for issue in result['planning']['issues']
                                                         if issue['severity'] == 'error'))
        self.assertEqual(len(result['itinerary']['days']), 3)
        self.assertFalse(any(issue['severity'] == 'error' for issue in result['planning']['issues']))
        if any(entry.get('route_matrix') for entry in result['notebook']['entries']):
            self.assertIn('Estimated travel between stops', result['answer'])
        else:
            self.assertEqual(result['planning']['status'], 'partial')
            self.assertTrue(any(issue['severity'] == 'unknown' and issue['constraint'] == 'tool_availability'
                                and issue['message'].startswith('DistanceMatrix:') for issue in result['planning']['issues']))
        self.assertIsNotNone(result['weather'])
        self.assertTrue(result['retrieval']['related_results'])
        choices = [choice['entity_id'] for day in result['itinerary']['days'] for choice in
                   [day['breakfast'], day['lunch'], day['dinner'], *day['attractions']] if choice]
        self.assertEqual(len(choices), len(set(choices)))

    def test_three_day_repair_resolves_duplicates_and_walking_times_together(self):
        book, request, plan = three_day_fixture()
        errors = [issue for issue in validate_itinerary(plan, request, book.data)[0] if issue.severity == 'error']
        self.assertEqual({issue.constraint for issue in errors}, {'diversity', 'transport_time'})
        repaired = repair_schedule(plan, request, book.data)
        self.assertIsNotNone(repaired)
        issues = validate_itinerary(repaired, request, book.data)[0]
        self.assertFalse(any(issue.severity == 'error' for issue in issues))
        self.assertFalse(any(issue.constraint == 'transport_time' for issue in issues))
        ids = [choice.evidence_id for day in repaired.days for kind, choice in day.choices()
               if kind in ('restaurant', 'attraction')]
        self.assertEqual(len(set(ids)), 12)
        self.assertEqual(repaired.days[1].date, '2026-10-15')
        self.assertTrue(any(issue.constraint == 'budget' for issue in issues))

    def test_explicit_venue_lock_is_not_ignored_to_fix_duplicates(self):
        book, request, plan = three_day_fixture()
        self.assertIsNone(repair_schedule(plan, request, book.data, preserve_venues=True))

    def test_repair_does_not_replace_known_route_failure_with_unknown_route(self):
        book, request, plan = three_day_fixture()
        book.data.entries[0].records = book.data.entries[0].records[:3]
        book.write(ToolResult(tool='RestaurantSearch', city='Hanoi', status='partial',
            records=[EvidenceRecord(**location(i).model_dump(), kind='restaurant', provider='Google metadata')
                     for i in range(20, 26)], note='Uncovered alternatives'))
        self.assertIsNone(repair_schedule(plan, request, book.data))

    def test_daily_attraction_count_cannot_exceed_unique_route_coverage(self):
        book, request, plan = three_day_fixture()
        schema = draft_schema(book.data, request)
        data = plan.model_dump(exclude_none=True)
        schema.model_validate(data)
        data['days'][0]['attractions'].append({'evidence_id': 'E2R2', 'start_time': '14:00', 'end_time': '15:00'})
        with self.assertRaises(ValueError):
            schema.model_validate(data)


if __name__ == '__main__':
    unittest.main()
