from dataclasses import dataclass, field
from typing import Any, Literal


RetrievalRoute = Literal["text", "image_text", "image_image"]


@dataclass
class RetrievalResult:
    place_id: str
    score: float
    source: str
    payload: dict[str, Any]


@dataclass
class EnrichedRetrievalResult(RetrievalResult):
    """A hit plus display metadata; text scores use RRF, image scores use similarity."""

    entity_id: str
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    url: str | None = None
    category: str | None = None
    province: str | None = None
    address: str | None = None
    image: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    missing_metadata: list[str] = field(default_factory=list)


@dataclass
class DestinationResolution:
    """Destination association; confidence is a rule score, not a probability."""

    source_entity_id: str
    destination_id: str | None = None
    page_id: str | None = None
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    url: str | None = None
    method: str = "unresolved"
    confidence: float = 0.0
    reason: str | None = None
    candidate_ids: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.destination_id is not None


@dataclass
class SearchResult(EnrichedRetrievalResult):
    """One entity represented by its highest-scoring hit and supporting hits."""

    evidence: list[EnrichedRetrievalResult] = field(default_factory=list)
    destination: DestinationResolution | None = None
    result_number: int | None = None


@dataclass
class LocationScope:
    name: str
    place_ids: list[str] = field(default_factory=list)
    page_ids: list[str] = field(default_factory=list)


@dataclass
class SearchResponse:
    """Primary matches plus separately identified destination context, without score fusion."""

    route: RetrievalRoute
    results: list[SearchResult] = field(default_factory=list)
    related_results: list[SearchResult] = field(default_factory=list)
    follow_up_status: str = ""
    location: str | None = None
    location_status: str = ""


@dataclass
class RankedDestination:
    """A destination scored by rank fusion; raw scores remain in its evidence."""

    destination_id: str
    page_id: str
    score: float
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    url: str | None = None
    source_ranks: dict[str, int] = field(default_factory=dict)
    source_scores: dict[str, float] = field(default_factory=dict)
    score_contributions: dict[str, float] = field(default_factory=dict)
    evidence: list[SearchResult] = field(default_factory=list)


@dataclass
class HybridSearchResponse:
    """Limited destination ranking plus all unresolved candidate hits."""

    destinations: list[RankedDestination] = field(default_factory=list)
    unresolved: list[SearchResult] = field(default_factory=list)
    total_destinations: int = 0
