"""Offline contract checks for single-case execution; no provider calls."""

import unittest

from fastapi.testclient import TestClient

from benchmarks.scripts.demo import create_app


HEADERS = {'X-VISTRA-Demo': '1'}


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def send(payload):
            self.calls.append(payload)
            return {'answer': 'Xin chào! Tôi là VISTRA.', 'pipeline_status': 'complete',
                    'response_mode': 'llm_only'}

        self.app = create_app(send_chat=send)
        self.client = self.enterContext(TestClient(self.app, base_url='http://127.0.0.1'))
        self.client.get('/')

    def start(self, family='VB1-ROUTE-101', language='vi'):
        response = self.client.post('/api/runs', headers=HEADERS,
                                    json={'family_id': family, 'language': language})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['run_id']

    def turn(self, run_id):
        return self.client.post(f'/api/runs/{run_id}/next', headers=HEADERS, json={})

    def test_catalog_and_preview_do_not_call_agent(self):
        self.assertEqual(len(self.client.get('/api/cases').json()), 300)
        detail = self.client.get('/api/cases/VB1-ROUTE-101').json()
        self.assertEqual(detail['turns'][0]['messages']['vi'], 'Bạn là ai?')
        self.assertIn('assertions', detail['private_expectations'])
        self.assertEqual(self.calls, [])

    def test_public_prompt_only_and_fresh_run_isolation(self):
        first = self.start()
        response = self.turn(first)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['scored'])
        self.assertEqual(self.turn(first).status_code, 409)
        second = self.start(language='en')
        self.assertEqual(self.turn(second).status_code, 200)
        self.assertEqual(set(self.calls[0]), {'user_id', 'session_id', 'message'})
        self.assertEqual(self.calls[0]['message'], 'Bạn là ai?')
        self.assertEqual(self.calls[1]['message'], 'Who are you?')
        self.assertNotEqual(self.calls[0]['user_id'], self.calls[1]['user_id'])
        self.assertNotEqual(self.calls[0]['session_id'], self.calls[1]['session_id'])

    def test_multiturn_preserves_owner_and_session_aliases(self):
        family = 'VB1-MEM-108'
        detail = self.client.get('/api/cases/' + family).json()
        run = self.start(family)
        for _ in detail['turns']:
            self.assertEqual(self.turn(run).status_code, 200)
        owners, sessions = {}, {}
        for turn, payload in zip(detail['turns'], self.calls):
            owners.setdefault(turn['owner_alias'], payload['user_id'])
            sessions.setdefault(turn['session_alias'], payload['session_id'])
            self.assertEqual(owners[turn['owner_alias']], payload['user_id'])
            self.assertEqual(sessions[turn['session_alias']], payload['session_id'])
        self.assertEqual(len(set(owners.values())), 2)

    def test_fault_cases_and_foreign_browser_cannot_execute(self):
        detail = self.client.get('/api/cases/VB1-REL-101')
        if detail.status_code == 404:
            row = next(row for row in self.client.get('/api/cases').json() if row['track'] == 'reliability')
            family = row['family_id']
        else:
            family = detail.json()['family_id']
        response = self.client.post('/api/runs', headers=HEADERS, json={'family_id': family})
        self.assertEqual(response.status_code, 409)
        run = self.start()
        with TestClient(self.app, base_url='http://127.0.0.1') as other:
            other.get('/')
            self.assertEqual(other.post(f'/api/runs/{run}/next', headers=HEADERS, json={}).status_code, 404)
        self.assertEqual(self.client.post('/api/runs', headers={**HEADERS, 'Origin': 'http://other.example'},
                                          json={'family_id': 'VB1-ROUTE-101'}).status_code, 403)
        self.assertEqual(self.calls, [])

    def test_failed_request_requires_fresh_run(self):
        def fail(_):
            raise RuntimeError('An upstream failure')

        with TestClient(create_app(send_chat=fail), base_url='http://127.0.0.1') as client:
            client.get('/')
            run = client.post('/api/runs', headers=HEADERS,
                              json={'family_id': 'VB1-ROUTE-101'}).json()['run_id']
            self.assertEqual(client.post(f'/api/runs/{run}/next', headers=HEADERS, json={}).status_code, 502)
            self.assertEqual(client.post(f'/api/runs/{run}/next', headers=HEADERS, json={}).status_code, 409)


if __name__ == '__main__':
    unittest.main()
