from pathlib import Path
from functools import lru_cache

from qdrant_client import QdrantClient, models

from src.schema.models import RetrievalResult
from src.utils.utils import load_config, resolve_path
from src.database.qdrant_connection import open_qdrant
from src.embedding.text_embedder import TextEmbedder
from src.embedding.sparse_embedder import SparseEmbedder
from src.utils.retrieval_activity import tool_call
from src.utils.progress import progress
from src.security.guardrails import (validate_text, bounded_int, validate_threshold,
    validate_ids, MAX_LIMIT, MAX_GROUP_SIZE, MAX_CANDIDATES)

class TextRetriever:

    def __init__(
        self,
        embedder: TextEmbedder | None = None,
        qdrant_path: str | Path | None = None,
        collection_name: str | None = None,
        sparse_embedder: SparseEmbedder | None = None,
    ):
        self.config = load_config()
        self.qdrant_path = resolve_path(qdrant_path or self.config["qdrant_path"])
        self.collection_name = collection_name or self.config["wikivoyage_collection"]
        self.sparse_vector_name = self.config["wikivoyage_sparse_vector"]
        self.client = open_qdrant(self.qdrant_path, self.config, QdrantClient)
        self.embedder = embedder if embedder is not None else TextEmbedder()
        self.sparse_embedder = sparse_embedder if sparse_embedder is not None else SparseEmbedder()
        self._embed_query = lru_cache(maxsize=self.config["query_embedding_cache_size"])(self.embedder.embed_query)
        self._embed_sparse_query = lru_cache(maxsize=self.config["query_embedding_cache_size"])(self.sparse_embedder.embed_query)

    def _search_hybrid(
        self,
        query: str,
        limit: int,
        score_threshold: float | None,
        page_ids: list[str] | None = None,
    ):
        query = validate_text(query, check_intent=True)
        validate_ids(page_ids, "page_ids")
        validate_threshold(score_threshold)
        query_filter = None
        if page_ids is not None:
            if not page_ids:
                return []
            ids = sorted(set(str(page_id) for page_id in page_ids))
            conditions = [models.FieldCondition(key="page_id", match=models.MatchAny(any=ids))]
            numbers = [int(page_id) for page_id in ids if page_id.isdecimal()]
            if numbers:
                conditions.append(models.FieldCondition(key="page_id", match=models.MatchAny(any=numbers)))
            # Page membership is mandatory; the inner OR accommodates string
            # and integer payload IDs. Both vector searches receive this filter.
            query_filter = models.Filter(must=[models.Filter(should=conditions)])
        query = query.strip()
        candidate_limit = max(
            self.config["candidate_min_limit"],
            limit * self.config["candidate_multiplier"],
        )
        bounded_int(candidate_limit, "candidate_limit", MAX_CANDIDATES)
        scope_detail = f"Allowed page IDs: {', '.join(ids[:10])}" if page_ids is not None else "No destination constraint."
        progress("text_scope", "Text scope applied before retrieval",
            f"{scope_detail}\nDocument limit: {limit} chunks. Dense and BM25 queries receive the same location filter before ranking."
            f"\nCandidate pool per query: {candidate_limit}.",
            filter_stage="before_retrieval", document_limit=limit,
            scoped=page_ids is not None, allowed_page_count=len(ids) if page_ids is not None else 0)
        dense_vector = self._embed_query(query)
        sparse_vector = self._embed_sparse_query(query)

        with tool_call("Qdrant", "Search Wikivoyage dense vectors"):
            dense = self.client.query_points(
                collection_name=self.collection_name,
                query=dense_vector.tolist(),
                query_filter=query_filter,
                limit=candidate_limit,
                with_payload=True,
                with_vectors=False,
            )
        with tool_call("Qdrant", "Search Wikivoyage BM25 vectors"):
            sparse = self.client.query_points(
                collection_name=self.collection_name,
                query=sparse_vector,
                using=self.sparse_vector_name,
                query_filter=query_filter,
                limit=candidate_limit,
                with_payload=True,
                with_vectors=False,
            )

        points, scores = {}, {}
        for hits in (dense.points, sparse.points):
            for rank, point in enumerate(hits, start=1):
                points[point.id] = point
                scores[point.id] = scores.get(point.id, 0.0) + 1 / (self.config["rrf_k"] + rank)

        ranked_ids = sorted(points, key=lambda point_id: (-scores[point_id], str(point_id)))
        return [
            points[point_id].model_copy(update={"score": scores[point_id]})
            for point_id in ranked_ids
            if score_threshold is None or scores[point_id] >= score_threshold
        ]

    def search(
        self,
        query: str,
        limit: int | None = None,
        score_threshold: float | None = None,
        *,
        page_ids: list[str] | None = None,
    ) -> list[RetrievalResult]:
        """Return chunks ranked by dense + BM25 RRF; threshold uses fused scores."""
        limit = limit if limit is not None else self.config["retrieval_limit"]
        self._validate_query(query, limit)
        points = self._search_hybrid(query, limit, score_threshold, page_ids)
        return self._results_from_points(points)[:limit]


    def search_grouped(
        self,
        query: str,
        limit: int | None = None,
        group_size: int | None = None,
        score_threshold: float | None = None,
        *,
        page_ids: list[str] | None = None,
    ) -> list[list[RetrievalResult]]:
        """Retain at most limit chunks in total, then group them for page cards.

        The limit is a document budget, not a page count multiplied by a group
        size. By default, all retained documents may belong to one filtered page.
        An explicit group_size can impose an additional per-page cap.
        """
        limit = limit if limit is not None else self.config["retrieval_limit"]
        explicit_group_size = group_size is not None
        group_size = group_size if explicit_group_size else limit

        self._validate_query(query, limit)
        self._validate_query(query, group_size)
        bounded_int(group_size, "group_size", MAX_GROUP_SIZE if explicit_group_size else MAX_LIMIT)
        points = self._search_hybrid(query, limit, score_threshold, page_ids)

        groups = {}
        for result in self._results_from_points(points)[:limit]:
            group = groups.setdefault(result.place_id, [])
            if len(group) < group_size:
                group.append(result)

        output = list(groups.values())
        document_count = sum(len(group) for group in output)
        progress("text_documents", "Text document retrieval completed",
            f"{document_count} chunks retained across {len(output)} pages; document limit: {limit}.",
            document_count=document_count, page_count=len(output), document_limit=limit)
        return output

    @staticmethod
    def _validate_query(query, limit):
        validate_text(query, check_intent=True)
        bounded_int(limit, "limit", MAX_LIMIT)

    @staticmethod
    def _results_from_points(points) -> list[RetrievalResult]:
        results = []

        for point in points:
            payload = point.payload or {}

            page_id = payload.get("page_id")

            if page_id is None:
                continue

            results.append(
                RetrievalResult(
                    place_id=str(page_id),
                    score=point.score,
                    source="text",
                    payload=payload
                )
            )

        return results

    def close(self) -> None:
        self._embed_query.cache_clear()
        self._embed_sparse_query.cache_clear()
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
