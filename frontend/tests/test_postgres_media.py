"""Optional isolated PostgreSQL checks for persisted gallery snapshots."""

import os
import unittest
from uuid import uuid4

from frontend.media import build_place_cards
from frontend.server import MemoryRepository
from frontend.tests.test_media import retrieval_fixture
from src.schema.memory import TripState
from src.schema.weather import WeatherEvidence


@unittest.skipUnless(os.getenv('VISTRA_TEST_POSTGRES') == '1', 'Enable isolated PostgreSQL integration checks')
class PostgresGalleryTests(unittest.TestCase):
    def setUp(self):
        self.owner = 'gallery-test-' + uuid4().hex
        self.repo = MemoryRepository().__enter__()
        self.turn = self.repo.store.commit_turn(self.owner, 'trip', expected_version=0,
            user_message='Gallery test Hanoi', answer='Gallery test reply', summary='Test travel conversation',
            trip_state=TripState(destination='Hanoi', start_date='2026-10-13', end_date='2026-10-13'))
        self.media = {'cards': build_place_cards(retrieval_fixture(20)), 'sources': []}
        self.media['weather'] = WeatherEvidence(status='available', location='Hanoi',
            start_date='2026-10-13', end_date='2026-10-13', timezone='Asia/Ho_Chi_Minh',
            fetched_at='2026-10-12T05:00:00+00:00', note='Forecasts may change.',
            days=[{'date': '2026-10-13', 'condition': 'Moderate rain', 'temperature_min_c': 22, 'temperature_max_c': 29}]).model_dump()

    def tearDown(self):
        self.repo.store.delete_user_memory(self.owner)
        self.repo.__exit__(None, None, None)

    def test_reopen_restores_twenty_cards_and_owner_authorization(self):
        self.assertTrue(self.repo.save_media(self.owner, 'trip', self.turn, self.media))
        with MemoryRepository() as reopened:
            assistant = reopened.detail(self.owner, 'trip')['messages'][1]
            self.assertEqual(len(assistant['cards']), 20)
            self.assertEqual(assistant['weather'], self.media['weather'])
            self.assertEqual(reopened.detail(self.owner, 'trip')['trip_state']['start_date'], '2026-10-13')
            self.assertEqual(assistant['cards'][0]['image_url'], '/api/photos/trip/1/0')
            self.assertEqual(reopened.photo(self.owner, 'trip', self.turn, 0), self.media['cards'][0]['photo_url'])
            self.assertIsNone(reopened.photo('other-owner', 'trip', self.turn, 0))

    def test_clear_removes_gallery_but_preserves_preferences(self):
        self.repo.store.set_preference(self.owner, 'diet', 'I am vegetarian')
        self.repo.save_media(self.owner, 'trip', self.turn, self.media)
        self.repo.clear(self.owner, 'trip')
        self.assertEqual(self.repo.detail(self.owner, 'trip')['messages'], [])
        self.assertIsNone(self.repo.photo(self.owner, 'trip', self.turn, 0))
        self.assertEqual(len(self.repo.store.recall(self.owner)), 1)
        self.assertFalse(self.repo.save_media(self.owner, 'trip', self.turn, self.media))

    def test_core_reset_does_not_expose_orphan_photos(self):
        self.repo.save_media(self.owner, 'trip', self.turn, self.media)
        self.repo.store.clear_session(self.owner, 'trip')
        self.assertIsNone(self.repo.photo(self.owner, 'trip', self.turn, 0))


if __name__ == '__main__':
    unittest.main()
