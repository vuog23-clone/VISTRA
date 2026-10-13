import hashlib
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import unittest

from fastapi.testclient import TestClient

from api.server import create_app
from src.database.postgres_memory_store import MemoryConflict


KEY = 'api-test-key-32-characters-long-enough'
HEADERS = {'X-API-Key': KEY}
BODY = {'user_id': 'developer-1', 'session_id': 'trip-1', 'message': 'Tell me about Hanoi'}


class ChatDouble:
    calls = []
    error = None
    started = None
    release = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def chat(self, owner, session, message, **options):
        self.calls.append((owner, session, message, options))
        if self.started is not None:
            self.started.set()
            assert self.release.wait(10), 'Test worker did not release the request'
        if self.error:
            raise self.error
        return {'answer': 'A sourced answer [S1]', 'pipeline_status': 'complete',
                'context': {'sources': [{'source_id': 'S1', 'name': 'Hanoi', 'url': 'https://en.wikivoyage.org/wiki/Hanoi'}]},
                'chat': {'version': 2, 'memory_status': 'saved'}}


class ServerTests(unittest.TestCase):
    def setUp(self):
        ChatDouble.calls, ChatDouble.error = [], None
        ChatDouble.started = ChatDouble.release = None
        self.client = self.enterContext(TestClient(create_app(api_key=KEY, chat_factory=ChatDouble)))

    def send(self, **updates):
        return self.client.post('/v1/chat', headers=HEADERS, json={**BODY, **updates})

    def test_health_and_openapi_without_backend_calls(self):
        self.assertEqual(self.client.get('/health').json(), {'status': 'ok'})
        schema = self.client.get('/openapi.json').json()
        self.assertIn('/v1/chat', schema['paths'])
        self.assertEqual(ChatDouble.calls, [])

    def test_api_key_required(self):
        for headers in ({}, {'X-API-Key': 'invalid'}):
            self.assertEqual(self.client.post('/v1/chat', headers=headers, json=BODY).status_code, 401)
        self.assertEqual(ChatDouble.calls, [])

    def test_chat_contract_and_stable_owner_namespace(self):
        response = self.send()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['user_id'], BODY['user_id'])
        self.assertEqual(data['sources'][0]['source_id'], 'S1')
        self.assertEqual(data['version'], 2)
        self.assertEqual(ChatDouble.calls[0][0], 'api_' + hashlib.sha256(BODY['user_id'].encode()).hexdigest())
        self.assertEqual(ChatDouble.calls[0][3]['limit'], 20)
        self.assertNotIn('include_weather', ChatDouble.calls[0][3])
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_dates_weather_and_retrieval_options_forwarded(self):
        response = self.send(start_date='2026-10-13', end_date='2026-10-15', include_weather=False,
                             retrieve_only=True, retrieval_limit=5, route='text')
        self.assertEqual(response.status_code, 200)
        options = ChatDouble.calls[0][3]
        self.assertFalse(options['include_weather'])
        self.assertTrue(options['retrieve_only'])
        self.assertEqual(options['trip_start'], '2026-10-13')
        self.assertEqual(options['limit'], 5)

    def test_itinerary_and_weather_source_ids_resolve(self):
        class PlanningDouble(ChatDouble):
            def chat(self, *args, **options):
                result = super().chat(*args, **options)
                result.update({
                    'notebook': {'entries': [{'entry_id': 'E1', 'tool': 'AttractionSearch', 'city': 'Hanoi',
                        'status': 'available', 'note': 'Indexed metadata', 'records': [{
                            'evidence_id': 'E1R1', 'entity_id': 'google:test-attraction', 'kind': 'attraction',
                            'name': 'Indexed attraction', 'city': 'Hanoi', 'provider': 'Google metadata'}]}]},
                    'itinerary': {'days': [{'day': 1, 'city': 'Hanoi', 'attractions': [{'evidence_id': 'E1R1'}]}]},
                    'weather': {'status': 'available', 'start_date': '2026-10-13', 'end_date': '2026-10-13',
                        'timezone': 'Asia/Bangkok', 'fetched_at': '2026-10-13T00:00:00+00:00',
                        'days': [{'date': '2026-10-13', 'condition': 'Clear sky'}], 'note': 'Test forecast'},
                    'planning': {'status': 'partial', 'action': 'create_itinerary'},
                })
                return result
        with TestClient(create_app(api_key=KEY, chat_factory=PlanningDouble)) as client:
            response = client.post('/v1/chat', headers=HEADERS, json=BODY)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({source['source_id'] for source in response.json()['sources']}, {'S1', 'E1R1', 'W1'})
        self.assertEqual(response.json()['planning']['status'], 'partial')

    def test_invalid_fields_and_limits_do_not_echo_input(self):
        cases = ({'retrieval_limit': 21}, {'retrieval_limit': True}, {'user_id': '../bad'},
                 {'message': ''}, {'image': '.env'}, {'start_date': '2026-10-13'},
                 {'start_date': '2026-10-15', 'end_date': '2026-10-13'},
                 {'start_date': '2026-10-13', 'end_date': '2026-10-27'},
                 {'start_date': 'not-a-date', 'end_date': 'not-a-date'},
                 {'response_mode': 'llm_only', 'location': 'Hanoi'},
                 {'message': 'private-input-must-not-be-echoed', 'retrieval_limit': '20'})
        for values in cases:
            with self.subTest(values=values):
                response = self.send(**values)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn('private-input-must-not-be-echoed', response.text)
        self.assertEqual(ChatDouble.calls, [])

    def test_blank_message_rejected_before_service(self):
        self.assertEqual(self.send(message='   ').status_code, 400)
        self.assertEqual(ChatDouble.calls, [])

    def test_oversized_chunked_body_rejected(self):
        def chunks():
            yield b'{' + b'x' * (40 * 1024)
            yield b'y' * (40 * 1024) + b'}'
        response = self.client.post('/v1/chat', headers={**HEADERS, 'Content-Type': 'application/json'}, content=chunks())
        self.assertEqual(response.status_code, 413)
        self.assertEqual(ChatDouble.calls, [])

    def test_backend_failure_redacted_and_capacity_released(self):
        ChatDouble.error = RuntimeError(KEY + ' internal-diagnostics')
        with self.assertLogs('vistra.api', 'ERROR') as logs:
            response = self.send()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(KEY, response.text)
        self.assertNotIn(KEY, '\n'.join(logs.output))
        ChatDouble.error = None
        self.assertEqual(self.send().status_code, 200)

    def test_memory_conflict_releases_capacity(self):
        ChatDouble.error = MemoryConflict('synthetic conflict')
        self.assertEqual(self.send().status_code, 409)
        ChatDouble.error = None
        self.assertEqual(self.send().status_code, 200)

    def test_busy_owner_capacity_and_health_responsiveness(self):
        ChatDouble.started, ChatDouble.release = Event(), Event()
        with TestClient(create_app(api_key=KEY, chat_factory=ChatDouble, max_concurrent=1)) as client:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(client.post, '/v1/chat', headers=HEADERS, json=BODY)
                try:
                    self.assertTrue(ChatDouble.started.wait(5))
                    self.assertEqual(client.post('/v1/chat', headers=HEADERS, json=BODY).status_code, 409)
                    self.assertEqual(client.post('/v1/chat', headers=HEADERS,
                        json={**BODY, 'user_id': 'another-user'}).status_code, 429)
                    self.assertEqual(client.get('/health').status_code, 200)
                finally:
                    ChatDouble.release.set()
                self.assertEqual(pending.result(timeout=5).status_code, 200)


if __name__ == '__main__':
    unittest.main()
