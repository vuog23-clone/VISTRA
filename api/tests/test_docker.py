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
        self.client = self.enterContext(httpx.Client(
            base_url=os.getenv('VISTRA_TEST_API_URL', 'http://127.0.0.1:18067'), timeout=180))
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

    @unittest.skipUnless(os.getenv('VISTRA_TEST_LIVE') == '1', 'Enable real provider calls explicitly')
    def test_live_grounded_answer_and_direct_greeting(self):
        import re
        user = 'api-answer-check-' + uuid4().hex
        owner = 'api_' + hashlib.sha256(user.encode()).hexdigest()
        with PostgresMemoryStore() as store:
            store.ensure_schema()
            try:
                response = self.client.post('/v1/chat', headers=self.headers, json={'user_id': user,
                    'session_id': 'qa', 'message': 'What cultural sights does the Hanoi guide describe? Cite the provided sources.',
                    'response_mode': 'retrieval', 'route': 'hybrid', 'location': 'Hanoi',
                    'planning_mode': 'off', 'include_weather': False, 'retrieval_limit': 20})
                self.assertEqual(response.status_code, 200, 'Live grounded answer API failed')
                data = response.json()
                self.assertTrue(data['answer'])
                self.assertEqual(data['response_mode'], 'retrieval')
                self.assertIsNone(data['itinerary'])
                self.assertTrue(data['retrieval']['results'])
                self.assertTrue(data['retrieval']['related_results'])
                citations = set(re.findall(r'\[(S\d+)\]', data['answer']))
                self.assertTrue(citations)
                self.assertTrue(citations <= {s['source_id'] for s in data['sources']})
                self.assertEqual(store.load_session(owner, 'qa').version, 1)
                direct = self.client.post('/v1/chat', headers=self.headers, json={'user_id': user,
                    'session_id': 'greeting', 'message': 'Hi, who are you?', 'planning_mode': 'off'})
                self.assertEqual(direct.status_code, 200, 'Live direct answer API failed')
                self.assertEqual(direct.json()['response_mode'], 'llm_only')
                self.assertTrue(direct.json()['answer'])
                self.assertIsNone(direct.json()['retrieval'])
                self.assertEqual(direct.json()['sources'], [])
            finally:
                store.delete_user_memory(owner)

    @unittest.skipUnless(os.getenv('VISTRA_TEST_LIVE') == '1', 'Enable real provider calls explicitly')
    def test_live_three_day_plan_and_scoped_follow_up(self):
        user = 'api-plan-check-' + uuid4().hex
        owner = 'api_' + hashlib.sha256(user.encode()).hexdigest()
        message = ('Create a 3-day Hanoi itinerary for two adults from October 14 to October 16, 2026. '
            'We are already in Hanoi; no flights or intercity transport are needed. Budget: 6,000,000 VND '
            'for both adults, excluding flights. Prefer vegetarian food, museums and cultural attractions. '
            'Use walking and a relaxed pace. Include breakfast, lunch, dinner and attractions every day, '
            'and two nights in a private double room near the Old Quarter. Start around 08:00 and finish '
            'by 20:00. Include weather and walking estimates; identify unknown prices and availability.')
        body = {'user_id': user, 'session_id': 'trip', 'message': message, 'retrieval_limit': 20,
                'include_weather': True}
        with PostgresMemoryStore() as store:
            store.ensure_schema()
            try:
                first = self.client.post('/v1/chat', headers=self.headers, json=body)
                self.assertEqual(first.status_code, 200, 'Live itinerary API failed')
                data = first.json()
                self.assertIn((data.get('planning') or {}).get('status'), ('partial', 'verified'),
                    'Initial itinerary did not pass validation')
                plan = data['itinerary']
                self.assertEqual(len(plan['days']), 3)
                self.assertEqual([day['date'] for day in plan['days']],
                                 ['2026-10-14', '2026-10-15', '2026-10-16'])
                self.assertFalse(any(i['severity'] == 'error' for i in data['planning']['issues']))
                self.assertTrue(data['retrieval']['results'])
                self.assertTrue(data['retrieval']['related_results'])
                self.assertTrue(data['weather']['days'])
                choices = [choice for day in plan['days'] for choice in
                    [day['breakfast'], day['lunch'], day['dinner'], *day['attractions']] if choice]
                self.assertEqual(len(choices), len({choice['entity_id'] for choice in choices}))
                self.assertEqual(data['version'], 1)
                second = self.client.post('/v1/chat', headers=self.headers, json={**body,
                    'message': 'Change only day 2 dinner to start at 19:00 and end at 20:00. Keep the same venues; keep days 1 and 3 unchanged.'})
                self.assertEqual(second.status_code, 200, 'Live follow-up API failed')
                revised = second.json()
                self.assertIn((revised.get('planning') or {}).get('status'), ('partial', 'verified'),
                    'Scoped edit did not pass validation')
                self.assertEqual(revised['planning']['action'], 'revise_itinerary')
                self.assertFalse(any(i['severity'] == 'error' for i in revised['planning']['issues']))
                self.assertEqual(revised['version'], 2)
                for old, new in zip(plan['days'], revised['itinerary']['days']):
                    def facts(day):
                        return {slot: [(c['entity_id'], c['start_time'], c['end_time']) for c in
                            (day[slot] if isinstance(day[slot], list) else [day[slot]]) if c]
                            for slot in ('breakfast', 'attractions', 'lunch', 'dinner', 'accommodation', 'transportation')}
                    if old['day'] != 2:
                        self.assertEqual(facts(old), facts(new))
                    self.assertEqual(old['dinner']['entity_id'], new['dinner']['entity_id'])
                self.assertEqual(revised['itinerary']['days'][1]['dinner']['start_time'], '19:00')
                self.assertEqual(revised['itinerary']['days'][1]['dinner']['end_time'], '20:00')
                snapshot = store.load_session(owner, 'trip')
                self.assertEqual(snapshot.version, 2)
                self.assertEqual(len(snapshot.recent_messages), 4)
                self.assertEqual(snapshot.trip_state.itinerary.model_dump(), revised['itinerary'])
                third = self.client.post('/v1/chat', headers=self.headers, json={**body,
                    'message': 'Change only days 2 and 3 dinner to start at 18:30 and end at 19:30. Keep the same venues and keep day 1 unchanged.'})
                self.assertEqual(third.status_code, 200, 'Live multi-day follow-up API failed')
                multi = third.json()
                self.assertIn((multi.get('planning') or {}).get('status'), ('partial', 'verified'),
                    'Multi-day edit did not pass validation')
                self.assertFalse(any(i['severity'] == 'error' for i in multi['planning']['issues']))
                self.assertEqual(facts(multi['itinerary']['days'][0]), facts(revised['itinerary']['days'][0]))
                for day in multi['itinerary']['days'][1:]:
                    self.assertEqual(day['dinner']['start_time'], '18:30')
                    self.assertEqual(day['dinner']['end_time'], '19:30')
                snapshot = store.load_session(owner, 'trip')
                self.assertEqual(snapshot.version, 3)
                self.assertEqual(len(snapshot.recent_messages), 6)
                self.assertEqual(snapshot.trip_state.itinerary.model_dump(), multi['itinerary'])
            finally:
                store.delete_user_memory(owner)


if __name__ == '__main__':
    unittest.main()
