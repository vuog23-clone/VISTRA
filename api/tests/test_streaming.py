"""Streaming preserves final validation, hides secrets and isolates requests."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
import unittest

from src.generation.direct_generation import DirectGenerator
from src.generation.rag_generation import RAGGenerator
from src.generation.streaming import AnswerStream, answer_stream, current_answer_stream
from src.security.guardrails import MAX_ANSWER_CHARS
from src.utils.redaction import CredentialRedactor
from src.utils.utils import load_config


class StreamingTests(unittest.TestCase):
    def test_complete_words_and_split_credentials_are_redacted(self):
        events = []
        stream = AnswerStream(events.append, clock=lambda: 100)
        stream.redactor = CredentialRedactor(extra_secrets=['alpha beta secret', 'sk-proj-12345678901234567890'])
        stream.begin()
        for delta in ['Hello ', 'alpha ', 'beta ', 'secret ', 'sk-proj-', '12345678901234567890', ' ', 'Hanoi']:
            stream.last_sent = 0
            stream.feed(delta)
        stream.finish()
        texts = [event['text'] for event in events]
        self.assertIn('Hello ', texts)
        self.assertFalse(any('alpha' in text or 'sk-proj' in text for text in texts))
        self.assertEqual(texts[-1], 'Hello [credential redacted] [credential redacted] Hanoi')

    def test_size_limit_and_request_context_reset(self):
        self.assertIsNone(current_answer_stream())
        with self.assertRaises(ValueError):
            with answer_stream(lambda _: None) as stream:
                self.assertIs(current_answer_stream(), stream)
                stream.feed('a'*(MAX_ANSWER_CHARS+1))
        self.assertIsNone(current_answer_stream())
        def task(word):
            events = []
            with answer_stream(events.append) as stream:
                stream.begin(); stream.feed(word+' '); stream.finish()
            return events[-1]['text']
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(task, ['Hanoi', 'Hue'])), ['Hanoi ', 'Hue '])

    def test_provider_stream_ignores_reasoning_and_returns_final_response(self):
        events, calls = [], []
        completion = SimpleNamespace(status='completed', output_text='Hello Hanoi')
        class Stream:
            def __iter__(self):
                yield SimpleNamespace(type='response.reasoning_summary_text.delta', delta='PRIVATE REASONING')
                yield SimpleNamespace(type='response.output_text.delta', delta='Hello ')
                yield SimpleNamespace(type='response.function_call_arguments.delta', delta='TOOL JSON')
                yield SimpleNamespace(type='response.output_text.delta', delta='Hanoi')
            def get_final_response(self):
                return completion
        @contextmanager
        def sdk_stream(**options):
            calls.append(options)
            yield Stream()
        generator = DirectGenerator(config=load_config(), client=SimpleNamespace(
            responses=SimpleNamespace(stream=sdk_stream, create=lambda **_: self.fail('Must use SDK streaming'))))
        with answer_stream(events.append):
            answer = generator.generate('Say hello')
        self.assertEqual(answer, 'Hello Hanoi')
        self.assertEqual(events[-1]['text'], answer)
        self.assertFalse(any('PRIVATE' in event['text'] or 'TOOL' in event['text'] for event in events))
        self.assertFalse(calls[0]['store'])

    def test_regular_api_call_keeps_non_streaming_contract(self):
        completion = SimpleNamespace(status='completed', output_text='Hello')
        generator = RAGGenerator(config=load_config(), client=SimpleNamespace(responses=SimpleNamespace(
            create=lambda **_: completion, stream=lambda **_: self.fail('No preview was requested'))))
        self.assertIs(generator._text_completion(model='test'), completion)

    def test_grounding_rejection_keeps_checked_fallback_as_final_answer(self):
        from src.generation.rag_generation import build_rag_context
        from src.schema.models import SearchResponse, SearchResult
        from src.schema.rag import GroundingReview
        drafts = iter(['Unsupported draft [S1].', 'Unsupported repaired draft [S1].'])
        class Stream:
            def __init__(self, text):
                self.text = text
            def __iter__(self):
                yield SimpleNamespace(type='response.output_text.delta', delta=self.text)
            def get_final_response(self):
                return SimpleNamespace(status='completed', output_text=self.text)
        @contextmanager
        def sdk_stream(**options):
            yield Stream(next(drafts))
        config = load_config()
        config['generation_vision_enabled'] = False
        hit = SearchResult(place_id='hanoi',entity_id='wikivoyage:hanoi',name='Hanoi',score=1,source='text',
                           payload={'text':'Hanoi has cultural attractions.'},url='https://en.wikivoyage.org/wiki/Hanoi')
        context = build_rag_context(SearchResponse(route='text',results=[hit]),config)
        generator = RAGGenerator(config=config,client=SimpleNamespace(responses=SimpleNamespace(stream=sdk_stream,
            parse=lambda **_: SimpleNamespace(status='completed',output_parsed=GroundingReview(
                supported=False,issues=['Unsupported claim'])))))
        events = []
        with answer_stream(events.append):
            final = generator.generate('Tell me about Hanoi',context)
        self.assertEqual(generator.last_grounding['status'],'metadata_fallback')
        self.assertIn('Hanoi has cultural attractions.',final)
        self.assertNotIn('Unsupported draft',final)
        self.assertEqual(sum(event['text']=='' for event in events),2)

    def test_failed_stream_closes_without_emitting_a_checked_final(self):
        events, closed = [], []
        @contextmanager
        def sdk_stream(**options):
            try:
                def tokens():
                    yield SimpleNamespace(type='response.output_text.delta', delta='Partial reply ')
                    raise RuntimeError('Provider disconnected')
                yield iter(tokens())
            finally:
                closed.append(True)
        generator = RAGGenerator(config=load_config(), client=SimpleNamespace(responses=SimpleNamespace(stream=sdk_stream)))
        with self.assertRaises(RuntimeError), answer_stream(events.append):
            generator._text_completion(model='test')
        self.assertEqual(closed, [True])
        self.assertFalse(any(event['phase'] == 'checking' for event in events))


if __name__ == '__main__':
    unittest.main()
