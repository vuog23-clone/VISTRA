"""Opt-in PostgreSQL tests use unique owners and never call model providers."""

import hashlib
import os
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from api.server import create_app
from api.tests.test_server import KEY, HEADERS
from src.database.postgres_memory_store import PostgresMemoryStore
from src.schema.memory import TurnUnderstanding, TripDetails, TripState, PreferenceChange
from src.service.chat_service import ChatService


@unittest.skipUnless(os.getenv('VISTRA_TEST_POSTGRES') == '1', 'Enable isolated PostgreSQL checks explicitly')
class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.user = 'api-check-' + uuid4().hex
        self.owner = 'api_' + hashlib.sha256(self.user.encode()).hexdigest()
        self.store = self.enterContext(PostgresMemoryStore())
        self.store.ensure_schema()
        self.addCleanup(self.store.delete_user_memory, self.owner)
        self.contexts = []
        contexts = self.contexts

        class Interpreter:
            def understand(self, query, memory):
                change = ([PreferenceChange(operation='set', key='diet', source_quote=query)]
                          if query == 'Remember that I prefer vegetarian food.' else [])
                return TurnUnderstanding(intent='memory', standalone_query=query, summary='Diet discussed.',
                    trip_state=TripDetails.model_validate(memory.trip_state.model_dump(exclude={'itinerary'})),
                    action='answer', preference_changes=change)

        def pipeline(query, **options):
            contexts.append(options['conversation_memory'])
            return {'answer': None if options.get('retrieve_only') else 'Checked memory reply.',
                    'pipeline_status': 'complete', 'context': {'sources': []}}

        def factory():
            return ChatService(interpreter=Interpreter(), pipeline=pipeline)

        self.client = self.enterContext(TestClient(create_app(api_key=KEY, chat_factory=factory)))

    def send(self, message, session='first-chat', **options):
        return self.client.post('/v1/chat', headers=HEADERS,
            json={'user_id': self.user, 'session_id': session, 'message': message, **options})

    def test_short_and_long_term_memory_across_sessions(self):
        first = self.send('Remember that I prefer vegetarian food.')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()['version'], 1)
        second = self.send('What food do I prefer?')
        self.assertEqual(second.json()['version'], 2)
        self.assertEqual(len(self.contexts[-1]['recent_messages']), 2)
        self.assertEqual(self.contexts[-1]['long_term'][0]['key'], 'diet')
        third = self.send('What food do I prefer?', session='second-chat')
        self.assertEqual(third.json()['version'], 1)
        self.assertEqual(self.contexts[-1]['recent_messages'], [])
        self.assertEqual(self.contexts[-1]['long_term'][0]['source_quote'], 'Remember that I prefer vegetarian food.')
        self.assertEqual(self.store.recall(self.user), [])

    def test_retrieval_only_does_not_commit_memory(self):
        response = self.send('Show indexed Hanoi evidence.', retrieve_only=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['memory_status'], 'read_only')
        snapshot = self.store.load_session(self.owner, 'first-chat')
        self.assertEqual(snapshot.version, 0)
        self.assertEqual(snapshot.recent_messages, [])

    def test_failed_edit_keeps_the_saved_itinerary(self):
        from src.schema.planning import Itinerary
        previous = Itinerary.model_validate({'days': [{'day': 1, 'city': 'Hanoi'}]})
        self.store.commit_turn(self.owner, 'trip', expected_version=0, user_message='Create a Hanoi plan.',
            answer='Saved test plan.', summary='Trip planned.',
            trip_state=TripState(destinations=['Hanoi'], duration_days=1, itinerary=previous))
        class Interpreter:
            def understand(self, query, memory):
                return TurnUnderstanding(intent='travel', standalone_query=query, summary='Edit requested.',
                    action='revise_itinerary', revision_day=1,
                    trip_state=TripDetails(destinations=['Hanoi'], duration_days=1), preference_changes=[])
        def pipeline(query, **options):
            return {'answer': 'The edit failed validation; the previous plan is retained.',
                    'pipeline_status': 'failed', 'itinerary': None, 'context': {'sources': []}}
        with ChatService(interpreter=Interpreter(), pipeline=pipeline) as service:
            service.chat(self.owner, 'trip', 'Change day 1 dinner.')
        snapshot = self.store.load_session(self.owner, 'trip')
        self.assertEqual(snapshot.version, 2)
        self.assertEqual(snapshot.trip_state.itinerary, previous)

    def test_provider_exception_commits_no_messages_or_preferences(self):
        class Interpreter:
            def understand(self, query, memory):
                return TurnUnderstanding(intent='memory', standalone_query=query, summary='Diet mentioned.',
                    action='answer', trip_state=TripDetails(),
                    preference_changes=[PreferenceChange(operation='set', key='diet', source_quote=query)])
        def pipeline(query, **options):
            raise RuntimeError('Test provider disconnected')
        with ChatService(interpreter=Interpreter(), pipeline=pipeline) as service:
            with self.assertRaises(RuntimeError):
                service.chat(self.owner, 'failed', 'Remember that I prefer vegetarian food.')
        snapshot = self.store.load_session(self.owner, 'failed')
        self.assertEqual(snapshot.version, 0)
        self.assertEqual(snapshot.recent_messages, [])
        self.assertEqual(self.store.recall(self.owner), [])

    def test_short_nights_reply_corrects_and_saves_pending_trip_details(self):
        from api.tests.test_clarification import pending_memory, stale_understanding
        from src.planning.orchestrator import trip_request, clarification
        memory = pending_memory()
        self.store.commit_turn(self.owner, 'clarify', expected_version=0,
            user_message='Plan three days from October 20 to October 24, 2026.',
            answer='Which stay length should I use?', summary='Stay length needs clarification.',
            trip_state=memory.trip_state)
        class Interpreter:
            def understand(self, query, context):
                return stale_understanding(context)
        def pipeline(query, **options):
            request = trip_request(query, options['conversation_memory']['trip_state'])
            self.assertEqual(request.duration_days, 5)
            self.assertIsNone(clarification(request, None, 'create_itinerary', None, query))
            return {'answer': 'Your trip details are ready.', 'context': {'sources': []}, 'pipeline_status': 'partial'}
        with ChatService(interpreter=Interpreter(), pipeline=pipeline) as service:
            result = service.chat(self.owner, 'clarify', '4 nights at Duong Dong town')
        self.assertIn('5 days and 4 nights', result['answer'])
        snapshot = self.store.load_session(self.owner, 'clarify')
        self.assertEqual(snapshot.trip_state.duration_days, 5)
        self.assertEqual(snapshot.trip_state.end_date, '2026-10-24')
        self.assertEqual(snapshot.trip_state.budget_amount, 6000000)
        self.assertIsNone(snapshot.trip_state.pending_clarification)
        self.assertEqual(snapshot.version, 2)


if __name__ == '__main__':
    unittest.main()
