"""Discovery searches run before a city is chosen, including chat follow-ups."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import httpx

from src.resolver.jev_location_extractor import JevLocationExtractor
from src.schema.memory import MemoryMessage, SessionSnapshot, TripDetails, TripState, TurnUnderstanding
from src.schema.models import SearchResponse, SearchResult
from src.service.chat_pipeline import run_chat_pipeline
from src.service.chat_service import ChatService


QUESTION = 'I want to travel to a place in Vietnam that have beautiful beaches, any recommendation?'


class DiscoveryTests(unittest.TestCase):
    def run_search(self, query, resolved, choice, *, snapshot=None):
        store = Mock()
        store.load_session.return_value = snapshot or SessionSnapshot()
        store.recall.return_value = []
        store.commit_turn.return_value = 1
        interpreter = Mock()
        interpreter.understand.return_value = TurnUnderstanding(intent='travel', action='search',
            standalone_query=resolved, summary='Discover beach destinations.', preference_changes=[],
            trip_state=TripDetails(destination='Vietnam', goal='Beautiful beaches in Vietnam'))
        provider_calls = []
        def provider(request):
            provider_calls.append(json.loads(request.content))
            return httpx.Response(200, json={'answers': {'location': {'choice': choice}}})
        client = self.enterContext(httpx.Client(transport=httpx.MockTransport(provider)))
        extractor = JevLocationExtractor(api_key='test-key', client=client, catalog_names={'Vietnam': []})
        source = SearchResult(place_id='beach-test', entity_id='wikivoyage:beach-test', score=1,
            source='text', name='Vietnam', url='https://en.wikivoyage.org/wiki/Vietnam',
            payload={'text': 'Vietnam has beaches in Phu Quoc and Nha Trang.', 'section': 'Beaches'})
        search = Mock()
        search.search.return_value = SearchResponse(route='hybrid', location='Vietnam', results=[source])
        generator = SimpleNamespace(model_name='test', last_grounding=None, last_context=None,
            last_vision=None, generate=Mock(return_value='Try Phu Quoc or Nha Trang for beaches. [S1]'))
        router = Mock()
        router.route.side_effect = AssertionError('The conversation LLM already selected travel retrieval.')
        def pipeline(message, **options):
            return run_chat_pipeline(message, search_service=search, location_extractor=extractor,
                generator=generator, response_router=router, **options)
        with ChatService(store=store, interpreter=interpreter, pipeline=pipeline) as service:
            result = service.chat('discovery-test', 'chat', query)
        return result, search, generator, store, provider_calls

    def test_country_recommendation_retrieves_before_a_city_is_chosen(self):
        result, search, generator, store, calls = self.run_search(QUESTION,
            'Recommend beautiful beach destinations in Vietnam.', 'place_0')
        self.assertEqual(result['response_mode'], 'retrieval')
        self.assertEqual(result['pipeline_status'], 'complete')
        self.assertEqual(search.search.call_args.kwargs['location'], 'Vietnam')
        self.assertEqual(search.search.call_args.kwargs['route'], 'hybrid')
        self.assertEqual(len(result['context']['sources']), 1)
        generator.generate.assert_called_once()
        self.assertEqual(calls[0]['state']['location_candidates'], ['Vietnam'])
        self.assertIsNone(store.commit_turn.call_args.kwargs['trip_state'].pending_clarification)

    def test_short_follow_up_keeps_country_and_beaches_in_retrieval_query(self):
        snapshot = SessionSnapshot(trip_state=TripState(destination='Vietnam',
            pending_clarification='Which destination should I search?'), recent_messages=[
                MemoryMessage(role='user', content=QUESTION),
                MemoryMessage(role='assistant', content='Which destination should I search?')])
        result, search, generator, store, _ = self.run_search('suggest me please',
            'Suggest beautiful beach destinations in Vietnam.', 'place_0', snapshot=snapshot)
        self.assertEqual(result['pipeline_status'], 'complete')
        self.assertIn('Vietnam', search.search.call_args.args[0])
        self.assertIn('beach', search.search.call_args.args[0])
        self.assertEqual(generator.generate.call_args.args[0], 'suggest me please')
        self.assertIsNone(store.commit_turn.call_args.kwargs['trip_state'].pending_clarification)

    def test_unscoped_discovery_still_runs_retrieval(self):
        result, search, _, _, _ = self.run_search('Recommend somewhere with beautiful beaches.',
            'Recommend somewhere with beautiful beaches.', 'no_location')
        search.search.assert_called_once()
        self.assertIsNone(search.search.call_args.kwargs['location'])
        self.assertEqual(result['pipeline_status'], 'complete')

    def test_genuinely_ambiguous_location_still_asks_before_search(self):
        result, search, generator, _, _ = self.run_search('What attractions are there?',
            'What attractions are there?', 'ambiguous')
        self.assertEqual(result['pipeline_status'], 'needs_clarification')
        search.search.assert_not_called()
        generator.generate.assert_not_called()

    def test_explicit_response_override_is_respected(self):
        store = Mock(load_session=Mock(return_value=SessionSnapshot()), recall=Mock(return_value=[]))
        interpreter = Mock(understand=Mock(return_value=TurnUnderstanding(intent='travel', action='search',
            standalone_query=QUESTION, summary='Beach question.', trip_state=TripDetails(), preference_changes=[])))
        pipeline = Mock(return_value={'answer': 'Test direct answer.'})
        with ChatService(store=store, interpreter=interpreter, pipeline=pipeline) as service:
            service.chat('discovery-test', 'chat', QUESTION, response_mode='llm_only')
        self.assertEqual(pipeline.call_args.kwargs['response_mode'], 'llm_only')


if __name__ == '__main__':
    unittest.main()
