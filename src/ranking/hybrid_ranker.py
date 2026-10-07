import re
from copy import deepcopy
from math import fsum, isfinite
from typing import Iterable

from src.schema.models import (
    DestinationResolution,
    HybridSearchResponse,
    RankedDestination,
    SearchResult,
)
from src.utils.utils import load_config


class HybridRanker:
    def __init__(
        self,
        rrf_k: int | None = None,
        *,
        text_weight: float | None = None,
        image_weight: float | None = None,
    ):
        config = load_config()
        rrf_k = rrf_k if rrf_k is not None else config["rrf_k"]
        self.default_limit = config["search_limit"]

        if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k < 1:
            raise ValueError("rrf_k must be a positive integer.")

        self.rrf_k = rrf_k

        if text_weight is None:
            text_weight = config["text_weight"]

        if image_weight is None:
            image_weight = config["image_weight"]

        self.weights = {
            "text": self._positive_weight(text_weight, "text_weight"),
            "image": self._positive_weight(image_weight, "image_weight"),
        }

    @staticmethod
    def _positive_weight(value: float, name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a finite positive number.")
        return float(value)

    @staticmethod
    def _validate_result(
        result: SearchResult,
        source: str,
    ) -> None:
        if not isinstance(result, SearchResult):
            raise TypeError("HybridRanker requires SearchResult objects.")

        if result.source != source:
            raise ValueError(f"Expected {source!r} results, received {result.source!r}.")

        if (
            isinstance(result.score, bool)
            or not isinstance(result.score, (int, float))
            or not isfinite(result.score)
        ):
            raise ValueError("Retrieval scores must be finite numbers.")

        resolution = result.destination
        if resolution is None:
            return

        if not isinstance(resolution, DestinationResolution):
            raise TypeError("result.destination must be a DestinationResolution or None.")

        if resolution.source_entity_id != result.entity_id:
            raise ValueError("Destination resolution belongs to a different source entity.")

        if resolution.resolved:
            page_id = str(resolution.page_id)
            if (
                not re.fullmatch(r"[1-9]\d*", page_id)
                or resolution.destination_id != f"wikivoyage:{page_id}"
            ):
                raise ValueError("Resolved destination_id must agree with its Wikivoyage page_id.")

    def _group_results(
        self,
        results: Iterable[SearchResult],
        source: str,
    ) -> tuple[dict[str, list[SearchResult]], list[SearchResult]]:
        groups = {}
        unresolved = []

        for result in results:
            self._validate_result(result, source)
            copied = deepcopy(result)

            if copied.destination is None or not copied.destination.resolved:
                unresolved.append(copied)
                continue

            destination_id = copied.destination.destination_id
            groups.setdefault(destination_id, []).append(copied)

        return groups, unresolved

    def _rank_source(
        self,
        groups: dict[str, list[SearchResult]],
        source: str,
        destinations: dict[str, RankedDestination],
    ) -> None:
        # Raw scores determine ranks within this source only.
        for hits in groups.values():
            hits.sort(key=lambda hit: (-hit.score, hit.entity_id))

        ordered_ids = sorted(
            groups,
            key=lambda destination_id: (-groups[destination_id][0].score, destination_id),
        )

        for rank, destination_id in enumerate(ordered_ids, start=1):
            hits = groups[destination_id]
            best_hit = hits[0]
            resolution = best_hit.destination

            if destination_id not in destinations:
                # Text is processed first, so its canonical metadata takes priority.
                destinations[destination_id] = RankedDestination(
                    destination_id=destination_id,
                    page_id=str(resolution.page_id),
                    score=0.0,
                    name=resolution.name,
                    latitude=resolution.latitude,
                    longitude=resolution.longitude,
                    url=resolution.url,
                )

            destination = destinations[destination_id]
            destination.source_ranks[source] = rank
            destination.source_scores[source] = float(best_hit.score)
            destination.score_contributions[source] = self.weights[source] / (self.rrf_k + rank)
            destination.evidence.extend(hits)

    def rank(
        self,
        text_results: Iterable[SearchResult] = (),
        image_results: Iterable[SearchResult] = (),
        *,
        limit: int | None = None,
    ) -> HybridSearchResponse:
        """Return a ranking and unresolved hits without modifying the inputs.

        Returned evidence is copied so downstream edits cannot alter the
        retriever's results. Final ties are ordered by destination ID.
        total_destinations counts the resolved union before applying limit.
        """
        limit = limit if limit is not None else self.default_limit
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer.")

        grouped = {}
        unresolved = []

        for source, results in (("text", text_results), ("image", image_results)):
            groups, unresolved_hits = self._group_results(results, source)
            grouped[source] = groups
            unresolved.extend(unresolved_hits)

        destinations: dict[str, RankedDestination] = {}

        for source, groups in grouped.items():
            self._rank_source(groups, source, destinations)

        for destination in destinations.values():
            destination.score = fsum(destination.score_contributions.values())

        ranked = sorted(
            destinations.values(),
            key=lambda destination: (-destination.score, destination.destination_id),
        )

        return HybridSearchResponse(
            destinations=ranked[:limit],
            unresolved=unresolved,
            total_destinations=len(ranked),
        )
