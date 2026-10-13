"""Gallery identity, unknown metadata, routing and twenty-document regression checks."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from frontend.media import build_place_cards, public_media
from src.security.guardrails import UnsafeInput


def retrieval_fixture(count=1):
    hits = []
    for index in range(count):
        identity = f'place-{index}'
        hits.append({'source': 'image', 'place_id': identity, 'entity_id': f'google:{identity}',
                     'name': f'Lake {index}', 'address': 'Hanoi, Vietnam', 'category': 'Lake', 'province': 'Hanoi',
                     'latitude': 21.03, 'longitude': 105.85, 'distance_km': 1.5,
                     'url': f'https://www.google.com/maps/place/?q=place_id:{identity}',
                     'image': {'place_id': identity, 'image_url': f'https://lh3.googleusercontent.com/p/photo-{index}'},
                     'metadata': {'place': {'place_id': identity, 'rating': 4.7, 'review_count': 120,
                                           'price_range': None, 'opening_hours': {'monday': [], 'tuesday': None}}}})
    return {'retrieval': {'results': [], 'related_results': hits},
            'context': {'sources': [{'source_id': 'S1', 'entity_id': 'google:place-0'}]}}


class GalleryTests(unittest.TestCase):
    def test_full_retrieval_not_context_budget_and_deduplication(self):
        result = retrieval_fixture(25)
        result['retrieval']['related_results'].insert(1, result['retrieval']['related_results'][0])
        cards = build_place_cards(result)
        self.assertEqual(len(cards), 20)
        self.assertEqual(cards[0]['source_id'], 'S1')
        self.assertIsNone(cards[1]['source_id'])
        self.assertEqual(len(build_place_cards(result, 5)), 5)

    def test_unknown_values_are_not_free_zero_or_closed(self):
        result = retrieval_fixture()
        card = build_place_cards(result)[0]
        self.assertIsNone(card['price_range'])
        self.assertIsNone(card['opening_hours']['tuesday'])
        self.assertEqual(card['opening_hours']['monday'], [])
        place = result['retrieval']['related_results'][0]['metadata']['place']
        place.update(rating=True, review_count=-1)
        card = build_place_cards(result)[0]
        self.assertIsNone(card['rating'])
        self.assertIsNone(card['review_count'])

    def test_photo_and_metadata_must_match_the_place(self):
        result = retrieval_fixture()
        hit = result['retrieval']['related_results'][0]
        hit['image']['place_id'] = 'other'
        self.assertIsNone(build_place_cards(result)[0]['photo_url'])
        hit['image']['place_id'] = hit['place_id']
        hit['image']['image_url'] = 'https://example.com/arbitrary.jpg'
        self.assertIsNone(build_place_cards(result)[0]['photo_url'])
        hit['metadata']['place']['place_id'] = 'other'
        self.assertEqual(build_place_cards(result), [])

    def test_public_media_hides_remote_photo_url(self):
        cards = build_place_cards(retrieval_fixture())
        public = public_media({'cards': cards}, 'trip', 1)
        self.assertNotIn('photo_url', public['cards'][0])
        self.assertEqual(public['cards'][0]['image_url'], '/api/photos/trip/1/0')

    def test_planning_cards_use_the_same_citation_as_the_itinerary(self):
        result = retrieval_fixture(1)
        result['itinerary'] = {'days':[{'day':1,'city':'Hanoi','attractions':[{'evidence_id':'E1R1'}]}]}
        result['notebook'] = {'entries':[{'entry_id':'E1','tool':'AttractionSearch','city':'Hanoi',
            'status':'available','note':'Test venue.', 'records':[{'evidence_id':'E1R1',
                'entity_id':'google:place-0','kind':'attraction','city':'Hanoi','name':'Lake 0',
                'provider':'Google metadata','url':'https://www.google.com/maps/place/?q=place_id:place-0'}]}]}
        self.assertEqual(build_place_cards(result)[0]['source_id'], 'E1R1')

    def test_travel_retrieval_includes_photos_but_direct_memory_chat_does_not(self):
        from frontend.server import run_demo_pipeline
        from src.schema.response import ResponseDecision
        router = SimpleNamespace(route=lambda _: ResponseDecision(mode='retrieval', decision_source='fallback'))
        with patch('main.run_pipeline', return_value={}) as pipeline:
            run_demo_pipeline('Find lakes in Hanoi', response_router=router)
            self.assertEqual(pipeline.call_args.kwargs['route'], 'hybrid')
            run_demo_pipeline('Find lakes in Hanoi without photos', response_router=router)
            self.assertEqual(pipeline.call_args.kwargs['route'], 'text')
            run_demo_pipeline('What preferences do you remember?', response_mode='llm_only')
            self.assertNotIn('route', pipeline.call_args.kwargs)

    def test_text_retrieval_keeps_twenty_chunks_from_one_page(self):
        from src.retrieval.text_retriever import TextRetriever
        retriever = object.__new__(TextRetriever)
        retriever.config = {'retrieval_limit': 20}
        retriever._search_hybrid = lambda *_: [SimpleNamespace(payload={'page_id': 'hanoi'}, score=1) for _ in range(25)]
        groups = retriever.search_grouped('Hanoi')
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 20)
        self.assertEqual(len(retriever.search_grouped('Hanoi', group_size=5)[0]), 5)
        with self.assertRaises(UnsafeInput):
            retriever.search_grouped('Hanoi', group_size=11)


if __name__ == '__main__':
    unittest.main()
