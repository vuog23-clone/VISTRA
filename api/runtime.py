"""Process-wide lazy models; database/vector clients remain request-scoped."""

from collections import OrderedDict
from contextlib import ExitStack
from copy import deepcopy
from threading import RLock


class SharedModel:
    def __init__(self, factory, *, cache_size=32):
        self.factory = factory
        self.cache_size = cache_size
        self._model = None
        self._lock = RLock()
        self._cache = OrderedDict()

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)

        def invoke(*args, **kwargs):
            cacheable = (name in {'embed_query', 'embed_text'} and len(args) == 1
                         and isinstance(args[0], str) and not kwargs)
            key = (name, args[0]) if cacheable else None
            with self._lock:
                if key is not None and key in self._cache:
                    self._cache.move_to_end(key)
                    return deepcopy(self._cache[key])
                if self._model is None:
                    self._model = self.factory()
                result = getattr(self._model, name)(*args, **kwargs)
                if key is not None and self.cache_size:
                    self._cache[key] = deepcopy(result)
                    while len(self._cache) > self.cache_size:
                        self._cache.popitem(last=False)
                return result
        return invoke

    def close(self):
        with self._lock:
            self._cache.clear()
            self._model = None


def _text_model():
    from src.embedding.text_embedder import TextEmbedder
    return TextEmbedder()


def _image_model():
    from src.embedding.image_embedder import ImageEmbedder
    return ImageEmbedder()


def _sparse_model():
    from src.embedding.sparse_embedder import SparseEmbedder
    return SparseEmbedder()


class RetrievalRuntime:
    def __init__(self, *, cache_size=32):
        self.text = SharedModel(_text_model, cache_size=cache_size)
        self.image = SharedModel(_image_model, cache_size=cache_size)
        self.sparse = SharedModel(_sparse_model, cache_size=cache_size)

    def pipeline(self, query, **options):
        from src.service.chat_pipeline import run_chat_pipeline
        with ExitStack() as resources:
            runtime = self

            class RequestSearch:
                service = None

                def search(self, *args, **kwargs):
                    if self.service is None:
                        from src.service.search_service import SearchService
                        self.service = resources.enter_context(SearchService(
                            text_embedder_factory=lambda: runtime.text,
                            image_embedder_factory=lambda: runtime.image,
                            sparse_embedder_factory=lambda: runtime.sparse))
                    return self.service.search(*args, **kwargs)

            options['search_service'] = RequestSearch()
            return run_chat_pipeline(query, **options)

    def close(self):
        for model in (self.text, self.image, self.sparse):
            model.close()
