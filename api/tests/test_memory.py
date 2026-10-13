"""Opt-in PostgreSQL tests use unique owners and never call model providers."""

import hashlib
import os
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from api.server import create_app
from api.tests.test_server import KEY, HEADERS
from src.database.postgres_memory_store import PostgresMemoryStore
from src.schema.memory import TurnUnderstanding, TripDetails, PreferenceChange
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


if __name__ == '__main__':
    unittest.main()
