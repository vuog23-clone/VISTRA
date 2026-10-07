from pathlib import Path
from qdrant_client import QdrantClient, models
from src.utils.utils import load_config, resolve_path, stable_uuid
from src.database.qdrant_connection import open_qdrant


class QdrantStore:
    def __init__(
        self,
        path: str | Path | None = None,
        collection_name: str | None = None,
        vector_size: int | None = None,
        *,
        create_if_missing: bool = True,
    ):
        self.config = load_config()
        vector_size = vector_size if vector_size is not None else self.config["text_vector_size"]

        self.path = resolve_path(path or self.config["qdrant_path"])

        self.collection_name = collection_name or self.config["wikivoyage_collection"]
        self.sparse_vector_name = self.config.get("wikivoyage_sparse_vector", "bm25")

        self.client = open_qdrant(self.path, self.config, QdrantClient)

        if self.client.collection_exists(self.collection_name):
            vectors = self.client.get_collection(self.collection_name).config.params.vectors
            if not isinstance(vectors, models.VectorParams) or vectors.size != vector_size or vectors.distance != models.Distance.COSINE:
                self.client.close()
                raise ValueError(
                    f"Collection '{self.collection_name}' must use an unnamed "
                    f"{vector_size}-dimensional COSINE vector."
                )
        else:
            if not create_if_missing:
                self.client.close()
                raise ValueError(f"Collection '{self.collection_name}' does not exist.")

            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=vector_size,
                    distance=models.Distance.COSINE
                ),
            )

        try:
            self._ensure_sparse_vector()
        except Exception:
            self.client.close()
            raise


    def add_chunks(
        self,
        chunks: list[dict],
        embeddings,
        sparse_embeddings: list[models.SparseVector],
    ) -> None:
        if (
            len(chunks) != len(embeddings)
            or len(chunks) != len(sparse_embeddings)
        ):
            raise ValueError("Chunks and embeddings must have the same length.")

        points = [
            models.PointStruct(
                id=stable_uuid(f"wikivoyage:{chunk['chunk_id']}"),
                vector={
                    "": embedding.tolist(),
                    self.sparse_vector_name: sparse_embedding,
                },
                payload=chunk,
            )
            for chunk, embedding, sparse_embedding in zip(
                chunks,
                embeddings,
                sparse_embeddings,
            )
        ]

        if not points:
            return

        self.client.upsert(
            collection_name=self.collection_name,
            points=points,
            wait=True,
        )


    def search(
        self,
        query_vector,
        limit=None
    ):
        result = self.client.query_points(
            collection_name=self.collection_name,
            query=query_vector.tolist(),
            limit=limit if limit is not None else load_config()["retrieval_limit"],
        )

        return result.points


    def search_sparse(
        self,
        query_vector: models.SparseVector,
        limit=None
    ):
        result = self.client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            using=self.sparse_vector_name,
            limit=limit if limit is not None else load_config()["retrieval_limit"],
        )

        return result.points


    def _ensure_sparse_vector(self) -> None:
        params = self.client.get_collection(
            self.collection_name
        ).config.params

        sparse_vectors = params.sparse_vectors or {}
        existing = sparse_vectors.get(self.sparse_vector_name)

        if existing is not None:
            if existing.modifier != models.Modifier.IDF:
                raise ValueError("The BM25 sparse vector must use Modifier.IDF.")
            return

        self.client.create_vector_name(
            collection_name=self.collection_name,
            vector_name=self.sparse_vector_name,
            vector_name_config=models.SparseVectorNameConfig(
                sparse=models.SparseVectorConfig(
                    modifier=models.Modifier.IDF,
                )
            ),
        )


    def update_sparse_vectors(
        self,
        point_ids: list[str | int],
        embeddings: list[models.SparseVector],
    ) -> None:
        if len(point_ids) != len(embeddings):
            raise ValueError("Point IDs and embeddings must have the same length.")

        points = [
            models.PointVectors(
                id=point_id,
                vector={self.sparse_vector_name: embedding},
            )
            for point_id, embedding in zip(point_ids, embeddings)
        ]

        if not points:
            return

        self.client.update_vectors(
            collection_name=self.collection_name,
            points=points,
            wait=True,
        )

    
    def close(self) -> None:
        self.client.close()
