from langchain_core.embeddings import Embeddings


class MemoryEmbeddings(Embeddings):
    def __init__(self, embedder=None):
        self._embedder = embedder


    def get_embedder(self):
        if self._embedder is None:
            from src.embedding.text_embedder import TextEmbedder

            self._embedder = TextEmbedder()
        return self._embedder


    def embed_query(self, text: str) -> list[float]:
        return self.get_embedder().embed_query(text).tolist()


    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.get_embedder().embed_documents(texts).tolist()
