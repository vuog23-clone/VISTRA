"""Add BM25 vectors to existing Wikivoyage points without rebuilding dense vectors."""

from pathlib import Path
from qdrant_client import models
from tqdm import tqdm

from src.database.qdrant_wikivoyage_store import QdrantStore
from src.utils.utils import load_config
from src.embedding.sparse_embedder import SparseEmbedder


class WikivoyageSparseIndexer:
    def __init__(
        self,
        batch_size: int | None = None,
        embedder: SparseEmbedder | None = None,
        store: QdrantStore | None = None,
        qdrant_path: str | Path | None = None,
        collection_name: str | None = None,
    ):
        self.config = load_config()
        self.batch_size = batch_size if batch_size is not None else self.config["wikivoyage_sparse_index_batch_size"]
        self.embedder = embedder if embedder is not None else SparseEmbedder()

        self.store = store if store is not None else QdrantStore(
            path=qdrant_path,
            collection_name=collection_name,
            create_if_missing=False,
        )

        self._owns_store = store is None
        self._closed = False


    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("WikivoyageSparseIndexer is closed.")


    def index_batch(
        self,
        points: list[models.Record]
    ) -> int:
        
        self._check_open()

        if not points:
            return 0

        texts = []
        for point in points:
            chunk = point.payload or {}

            if (
                any(not isinstance(chunk.get(key), str) for key in ("title", "section", "text"))
                or not chunk["text"].strip()
            ):
                raise ValueError(f"Point {point.id!r} requires title, section, and nonblank text.")

            texts.append(
                f"Title: {chunk['title']}\n"
                f"Section: {chunk['section']}\n"
                f"{chunk['text']}"
            )

        embeddings = self.embedder.embed_documents(texts, batch_size=len(points))
        self.store.update_sparse_vectors([point.id for point in points], embeddings)

        return len(points)


    def index(
        self,
        *,
        max_points: int | None = None,
        overwrite: bool = False,
    ) -> int:
        
        self._check_open()

        query_filter = None
        if not overwrite:
            query_filter = models.Filter(
                must_not=[models.HasVectorCondition(has_vector=self.store.sparse_vector_name)]
            )

        total = self.store.client.count(
            collection_name=self.store.collection_name,
            count_filter=query_filter,
            exact=True,
        ).count

        if max_points is not None:
            total = min(total, max_points)

        indexed = 0
        offset = None

        with tqdm(total=total, desc="Indexing Wikivoyage BM25", unit="chunk") as progress:
            while indexed < total:
                points, next_offset = self.store.client.scroll(
                    collection_name=self.store.collection_name,
                    scroll_filter=query_filter,
                    offset=offset,
                    limit=min(self.batch_size, total - indexed),
                    with_payload=["title", "section", "text"],
                    with_vectors=False,
                )

                if not points:
                    break

                count = self.index_batch(points)
                indexed += count
                progress.update(count)

                if next_offset is None:
                    break

                offset = next_offset

        return indexed

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._owns_store:
                self.store.close()

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--max-points", type=int, help="Limit the number of points updated.")
    parser.add_argument("--overwrite", action="store_true", help="Recompute existing BM25 vectors.")
    args = parser.parse_args()
    for name in ("batch_size", "max_points"):
        value = getattr(args, name)
        if value is not None and value < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive.")
    with WikivoyageSparseIndexer(batch_size=args.batch_size) as indexer:
        print(f"Indexed {indexer.index(max_points=args.max_points, overwrite=args.overwrite):,} Wikivoyage BM25 vectors.")


if __name__ == "__main__":
    main()
