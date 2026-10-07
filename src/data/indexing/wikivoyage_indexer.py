from pathlib import Path
from tqdm import tqdm

from src.database.qdrant_wikivoyage_store import QdrantStore
from src.embedding.text_embedder import TextEmbedder
from src.utils.utils import batched, count_jsonl, load_config, read_jsonl, resolve_path


class WikivoyageIndexer:
    def __init__(
        self,
        path: str | Path | None = None,
        batch_size: int | None = None,
        embedder: TextEmbedder | None = None,
        store: QdrantStore | None = None,
        qdrant_path: str | Path | None = None,
        collection_name: str | None = None,
        vector_size: int | None = None,
    ):
        self.config = load_config()
        self.path = resolve_path(path, self.config["wikivoyage_chunks_path"])
        self.batch_size = batch_size if batch_size is not None else self.config["wikivoyage_index_batch_size"]

        vector_size = vector_size if vector_size is not None else self.config["text_vector_size"]

        self.embedder = embedder if embedder is not None else TextEmbedder(
            truncate_dim=vector_size
        )

        self.store = store if store is not None else QdrantStore(
            path=qdrant_path,
            collection_name=collection_name,
            vector_size=vector_size,
        )


    @staticmethod
    def build_embedding_text(chunk: dict) -> str:
        return (
            f"Title: {chunk['title']}\n"
            f"Section: {chunk['section']}\n"
            f"{chunk['text']}"
        )


    def index_batch(
        self,
        chunks: list[dict]
    ) -> None:
        
        texts = [self.build_embedding_text(chunk) for chunk in chunks]
        embeddings = self.embedder.embed_documents(texts, batch_size=len(chunks))
        self.store.add_chunks(chunks, embeddings)


    def index(self) -> int:
        total_chunks = count_jsonl(self.path)
        indexed_chunks = 0

        with tqdm(
            total=total_chunks,
            desc="Indexing Wikivoyage",
            unit="chunk",
        ) as progress:
            for batch in batched(read_jsonl(self.path), self.batch_size):
                self.index_batch(batch)
                indexed_chunks += len(batch)
                progress.update(len(batch))

        return indexed_chunks

    def close(self) -> None:
        self.store.close()
