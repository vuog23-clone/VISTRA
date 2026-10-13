"""Opt-in HTTP checks against an isolated local API container."""

import hashlib
import os
import unittest
from time import monotonic, sleep
from uuid import uuid4

from dotenv import load_dotenv
import httpx

from src.database.postgres_memory_store import PostgresMemoryStore
from src.utils.utils import PROJECT_ROOT


@unittest.skipUnless(os.getenv('VISTRA_TEST_DOCKER') == '1', 'Enable isolated Docker checks explicitly')
class DockerTests(unittest.TestCase):
    def setUp(self):
        load_dotenv(PROJECT_ROOT / '.env', override=False)
        self.client = self.enterContext(httpx.Client(base_url='http://127.0.0.1:18067', timeout=120))
        self.headers = {'X-API-Key': os.environ['VISTRA_API_KEY']}
        deadline = monotonic() + 20
        while monotonic() < deadline:
            try:
                if self.client.get('/health', timeout=2).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            sleep(0.25)
        else:
            self.fail('The isolated API container did not become ready within 20 seconds.')

    def test_health_authentication_and_validation(self):
        self.assertEqual(self.client.get('/health').json(), {'status': 'ok'})
        body = {'user_id': 'container-check', 'session_id': 'check', 'message': 'Hello'}
        self.assertEqual(self.client.post('/v1/chat', json=body).status_code, 401)
        response = self.client.post('/v1/chat', headers=self.headers, json={**body, 'retrieval_limit': 21})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get('/openapi.json').json()['info']['version'], '2.0.0')

    @unittest.skipUnless(os.getenv('VISTRA_TEST_LIVE') == '1', 'Enable real provider calls explicitly')
    def test_live_retrieval_only(self):
        user = 'api-retrieval-check-' + uuid4().hex
        owner = 'api_' + hashlib.sha256(user.encode()).hexdigest()
        with PostgresMemoryStore() as store:
            store.ensure_schema()
            try:
                response = self.client.post('/v1/chat', headers=self.headers, json={'user_id': user,
                    'session_id': 'read-only', 'message': 'What cultural sights does the Hanoi guide describe?',
                    'response_mode': 'retrieval', 'route': 'hybrid', 'location': 'Hanoi',
                    'retrieval_limit': 5, 'retrieve_only': True})
                self.assertEqual(response.status_code, 200, 'Live API retrieval failed')
                data = response.json()
                self.assertEqual(data['memory_status'], 'read_only')
                self.assertTrue(data['retrieval']['results'])
                self.assertTrue(data['retrieval']['related_results'])
                self.assertEqual(store.load_session(owner, 'read-only').version, 0)
            finally:
                store.delete_user_memory(owner)

    @unittest.skipUnless(os.getenv('VISTRA_TEST_LIVE') == '1', 'Enable real provider calls explicitly')
    def test_live_memory_across_sessions(self):
        user = 'api-docker-check-' + uuid4().hex
        owner = 'api_' + hashlib.sha256(user.encode()).hexdigest()
        with PostgresMemoryStore() as store:
            store.ensure_schema()
            try:
                first = self.client.post('/v1/chat', headers=self.headers, json={'user_id': user,
                    'session_id': 'first', 'message': 'Remember that I prefer vegetarian food.',
                    'response_mode': 'llm_only'})
                self.assertEqual(first.status_code, 200, 'Live API turn failed')
                self.assertEqual(first.json()['memory_status'], 'saved')
                self.assertTrue(any(item.key == 'diet' for item in store.recall(owner)))
                second = self.client.post('/v1/chat', headers=self.headers, json={'user_id': user,
                    'session_id': 'second', 'message': 'What food preference do you remember about me?',
                    'response_mode': 'llm_only'})
                self.assertEqual(second.status_code, 200, 'Live memory recall failed')
                self.assertIn('vegetarian', second.json()['answer'].lower())
            finally:
                store.delete_user_memory(owner)


if __name__ == '__main__':
    unittest.main()
