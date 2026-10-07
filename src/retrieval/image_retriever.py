from pathlib import Path

from PIL import Image
from qdrant_client import QdrantClient, models

from src.embedding.image_embedder import ImageEmbedder
from src.schema.models import RetrievalResult
from src.utils.utils import load_config, resolve_path
from src.database.qdrant_connection import open_qdrant


class ImageRetriever:
    def __init__(
        self,
        embedder: ImageEmbedder | None = None,
        qdrant_path: str | Path | None = None,
        collection_name: str | None = None,
    ):
        self.config = load_config()
        self.qdrant_path = resolve_path(qdrant_path or self.config["qdrant_images_path"])
        self.collection_name = collection_name or self.config["scraped_images_collection"]
        self.client = open_qdrant(self.qdrant_path, self.config, QdrantClient)
        self.embedder = embedder if embedder is not None else ImageEmbedder()

    def search(
        self,
        query: str | Path | Image.Image,
        limit: int | None = None,
        score_threshold: float | None = None,
        *,
        place_ids: list[str] | None = None,
    ) -> list[RetrievalResult]:
 
        limit = limit if limit is not None else self.config["retrieval_limit"]
        self._validate_query(query, limit)
        if place_ids == []:
            return []
        query_vector = self.embedder.embed_image(query)
        return self._query_points(query_vector, limit, score_threshold, place_ids)

    def search_text(
        self,
        query: str,
        limit: int | None = None,
        score_threshold: float | None = None,
        *,
        place_ids: list[str] | None = None,
    ) -> list[RetrievalResult]:

        self._validate_text(query)
        limit = limit if limit is not None else self.config["retrieval_limit"]
        self._validate_query(query, limit)
        if place_ids == []:
            return []
        vector = self.embedder.embed_text(query).squeeze(0)
        return self._query_points(vector, limit, score_threshold, place_ids)

    @staticmethod
    def _location_filter(place_ids: list[str] | None) -> models.Filter | None:
        if place_ids is None:
            return None
        return models.Filter(must=[models.FieldCondition(
            key="place_id", match=models.MatchAny(any=sorted(set(place_ids))),
        )])

    def _query_points(self, vector, limit, score_threshold, place_ids=None):
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=vector.tolist(),
            query_filter=self._location_filter(place_ids),
            limit=limit,
            score_threshold=score_threshold,
            with_payload=True,
            with_vectors=False,
        )

        return self._results_from_points(response.points)

    @staticmethod
    def _validate_text(query: str) -> None:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a nonblank visual description.")

    @staticmethod
    def _validate_query(query: str | Path | Image.Image, limit: int) -> None:
        if not isinstance(query, (str, Path, Image.Image)):
            raise TypeError("query must be an image path or a PIL image.")
        if isinstance(query, str) and not query.strip():
            raise ValueError("query image path cannot be blank.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer.")

    def search_grouped(
        self,
        query: str | Path | Image.Image,
        limit: int | None = None,
        group_size: int | None = None,
        score_threshold: float | None = None,
        *,
        place_ids: list[str] | None = None,
    ) -> list[list[RetrievalResult]]:

        limit = limit if limit is not None else self.config["search_limit"]
        group_size = group_size if group_size is not None else self.config["retrieval_group_size"]
        self._validate_query(query, limit)
        if place_ids == []:
            return []

        query_vector = self.embedder.embed_image(query)
        return self._query_groups(query_vector, limit, group_size, score_threshold, place_ids)

    def search_text_grouped(
        self,
        query: str,
        limit: int | None = None,
        group_size: int | None = None,
        score_threshold: float | None = None,
        *,
        place_ids: list[str] | None = None,
    ) -> list[list[RetrievalResult]]:
        
        self._validate_text(query)
        limit = limit if limit is not None else self.config["search_limit"]
        group_size = group_size if group_size is not None else self.config["retrieval_group_size"]
        self._validate_query(query, limit)
        if place_ids == []:
            return []

        vector = self.embedder.embed_text(query).squeeze(0)
        return self._query_groups(vector, limit, group_size, score_threshold, place_ids)

    def _query_groups(self, vector, limit, group_size, score_threshold, place_ids=None):
        response = self.client.query_points_groups(
            collection_name=self.collection_name,
            query=vector.tolist(),
            query_filter=self._location_filter(place_ids),
            group_by="place_id",
            limit=limit,
            group_size=group_size,
            score_threshold=score_threshold,
            with_payload=True,
            with_vectors=False,
        )
        groups = [self._results_from_points(group.hits) for group in response.groups]
        return [group for group in groups if group]

    @staticmethod
    def _results_from_points(points) -> list[RetrievalResult]:
        results = []
        for point in points:
            payload = point.payload or {}
            place_id = payload.get("place_id")
            if place_id is None:
                continue
            results.append(RetrievalResult(
                place_id=str(place_id),
                score=point.score,
                source="image",
                payload=payload,
            ))
        return results

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
