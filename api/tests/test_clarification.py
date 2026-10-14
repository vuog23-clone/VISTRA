"""Brief answers resolve a pending trip clarification without repeated questions."""

import unittest
from unittest.mock import Mock

from src.memory.clarification import resolve_stay_reply
from src.planning.orchestrator import TravelOrchestrator, clarification, trip_request
from src.schema.memory import ConversationMemory, TripDetails, TripState, TurnUnderstanding
from src.schema.planning import TripRequest
from src.security.guardrails import UnsafeInput
from src.utils.utils import load_config


def pending_memory():
    state = TripState(destinations=['Phu Quoc'], destination='Phu Quoc', origin='Ho Chi Minh City',
        start_date='2026-10-20', end_date='2026-10-24', duration_days=3,
        travelers=2, budget_amount=6000000, currency='VND', cuisines=['Vietnamese'],
        room_type='private double room', include_weather=True,
        pending_clarification='Which dates or duration should I use?', pending_planning_action='create_itinerary')
    return ConversationMemory(trip_state=state)


def stale_understanding(memory):
    return TurnUnderstanding(intent='travel', standalone_query='Continue the trip.', summary='Trip details retained.',
        action='create_itinerary', trip_state=TripDetails.model_validate(memory.trip_state.model_dump(exclude={'itinerary'})),
        preference_changes=[])


class ClarificationTests(unittest.TestCase):
    def test_four_nights_at_lodging_area_corrects_duration_and_keeps_other_details(self):
        memory = pending_memory()
        value, acknowledgement = resolve_stay_reply(stale_understanding(memory), '4 nights at Duong Dong town', memory)
        self.assertEqual(value.trip_state.duration_days, 5)
        self.assertEqual(value.trip_state.end_date, '2026-10-24')
        self.assertEqual(value.trip_state.destinations, ['Phu Quoc'])
        self.assertEqual(value.trip_state.budget_amount, 6000000)
        self.assertEqual(value.trip_state.travelers, 2)
        self.assertEqual(value.action, 'create_itinerary')
        self.assertIn('5 days and 4 nights', acknowledgement)
        request = trip_request('4 nights at Duong Dong town', value.trip_state.model_dump())
        self.assertIsNone(clarification(request, None, 'create_itinerary', None, '4 nights'))

    def test_days_or_nights_update_end_date_from_known_start(self):
        for reply, days, end in [('3 days', 3, '2026-10-22'), ('2 nights', 3, '2026-10-22'),
                                 ('three nights', 4, '2026-10-23'), ('4 đêm ở Dương Đông', 5, '2026-10-24')]:
            with self.subTest(reply=reply):
                memory = pending_memory()
                value, _ = resolve_stay_reply(stale_understanding(memory), reply, memory)
                self.assertEqual(value.trip_state.duration_days, days)
                self.assertEqual(value.trip_state.end_date, end)

    def test_keep_dates_uses_inclusive_duration(self):
        memory = pending_memory()
        value, _ = resolve_stay_reply(stale_understanding(memory), 'Keep the dates', memory)
        self.assertEqual(value.trip_state.duration_days, 5)

    def test_unrelated_or_ambiguous_replies_do_not_resume_planning(self):
        for reply in ('Hi', 'What does 4 nights cost?', 'Not 4 nights', '4 nights or 3 days?',
                      '4 nights at Phu Quoc and 2 nights in Hanoi'):
            memory = pending_memory()
            original = stale_understanding(memory)
            value, acknowledgement = resolve_stay_reply(original, reply, memory)
            self.assertIs(value, original)
            self.assertIsNone(acknowledgement)

    def test_nights_are_not_treated_as_a_clarification_outside_pending_flow(self):
        memory = pending_memory()
        memory.trip_state.pending_clarification = None
        original = stale_understanding(memory)
        value, acknowledgement = resolve_stay_reply(original, '4 nights', memory)
        self.assertIs(value, original)
        self.assertIsNone(acknowledgement)

    def test_no_start_date_does_not_invent_dates(self):
        memory = pending_memory()
        memory.trip_state.start_date = memory.trip_state.end_date = None
        value, _ = resolve_stay_reply(stale_understanding(memory), '4 nights', memory)
        self.assertEqual(value.trip_state.duration_days, 5)
        self.assertIsNone(value.trip_state.start_date)
        self.assertIsNone(value.trip_state.end_date)

    def test_excessive_stay_returns_a_public_validation_error(self):
        memory = pending_memory()
        with self.assertRaises(UnsafeInput):
            resolve_stay_reply(stale_understanding(memory), '14 nights', memory)

    def test_conflict_offers_two_clear_choices_without_running_tools(self):
        request = TripRequest.model_validate({key: value for key, value in pending_memory().trip_state.model_dump().items()
                                             if key in TripRequest.model_fields})
        tools, planner = Mock(), Mock()
        result = TravelOrchestrator(config=load_config(), tools=tools, planner=planner).run('Plan a trip', request)
        self.assertIn('5 days and 4 nights', result['answer'])
        self.assertIn('2026-10-22', result['answer'])
        self.assertEqual([choice['reply'] for choice in result['planning']['clarification_options']], ['5 days', '3 days'])
        tools.run.assert_not_called()
        planner.run.assert_not_called()

    def test_missing_saved_plan_resumes_creation_after_details_are_supplied(self):
        result = TravelOrchestrator(config=load_config(), tools=Mock(), planner=Mock()).run(
            'Change my itinerary', TripRequest(), action='revise_itinerary')
        self.assertEqual(result['planning']['action'], 'create_itinerary')
        self.assertEqual(result['pipeline_status'], 'needs_clarification')


if __name__ == '__main__':
    unittest.main()
