from concurrent.futures import ThreadPoolExecutor
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from api.runtime import SharedModel, RetrievalRuntime


class RuntimeTests(unittest.TestCase):
    def test_lazy_single_initialization_bounded_cache_and_safe_copies(self):
        calls, loads = [], []
        def factory():
            loads.append(1)
            return SimpleNamespace(embed_query=lambda query: calls.append(query) or [len(query)])
        model = SharedModel(factory, cache_size=2)
        self.assertEqual(loads, [])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(model.embed_query, ['Hanoi'] * 4))
        self.assertEqual(loads, [1])
        self.assertEqual(calls, ['Hanoi'])
        results[0].append(999)
        self.assertEqual(model.embed_query('Hanoi'), [5])
        for query in ['Hue', 'Da Nang', 'Hanoi']:
            model.embed_query(query)
        self.assertEqual(calls.count('Hanoi'), 2)
        model.close()
        model.embed_query('Hanoi')
        self.assertEqual(len(loads), 2)

    def test_failed_model_load_can_retry(self):
        attempts = []
        def factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError('Missing model')
            return SimpleNamespace(embed_query=lambda query: query)
        model = SharedModel(factory)
        with self.assertRaises(RuntimeError):
            model.embed_query('Hanoi')
        self.assertEqual(model.embed_query('Hanoi'), 'Hanoi')

    def test_direct_pipeline_never_constructs_search_service(self):
        runtime = RetrievalRuntime()
        with patch('src.service.chat_pipeline.run_chat_pipeline', return_value={'answer': 'Hello'}) as pipeline:
            self.assertEqual(runtime.pipeline('Hello')['answer'], 'Hello')
            self.assertIsNone(pipeline.call_args.kwargs['search_service'].service)
        self.assertIsNone(runtime.text._model)

    def test_requests_share_models_and_close_separate_search_services(self):
        opened, closed = [], []
        class SearchDouble:
            def __init__(self, **factories):
                self.factories = factories
                opened.append(self)
            def __enter__(self):
                return self
            def __exit__(self, *_):
                closed.append(self)
            def search(self, query):
                return self.factories['text_embedder_factory']()
        def pipeline(query, **options):
            return options['search_service'].search(query)
        runtime = RetrievalRuntime()
        with patch.dict(sys.modules, {'src.service.search_service': SimpleNamespace(SearchService=SearchDouble)}), \
             patch('src.service.chat_pipeline.run_chat_pipeline', side_effect=pipeline):
            self.assertIs(runtime.pipeline('Hanoi'), runtime.text)
            self.assertIs(runtime.pipeline('Hue'), runtime.text)
        self.assertEqual(len(opened), 2)
        self.assertEqual(closed, opened)
        self.assertIsNot(opened[0], opened[1])


if __name__ == '__main__':
    unittest.main()
