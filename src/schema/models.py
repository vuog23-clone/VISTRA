from dataclasses import dataclass, field
from typing import Any, Literal


RetrievalRoute = Literal["text", "hybrid"]
# Image retrieval modes are internal branches, not selectable Jev/API routes.
AtomicRetrievalRoute = Literal["text", "image_text", "image_image"]


@dataclass
class RetrievalResult:
    place_id: str
    score: float
    source: str
    payload: dict[str, Any]
    # Transport identity for photo lookup; never stored as a payload field.
    point_id: str | None = field(default=None, kw_only=True)


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
    distance_km: float | None = None


@dataclass
class SearchResult(EnrichedRetrievalResult):
    """One entity represented by its highest-scoring hit and supporting hits."""

    evidence: list[EnrichedRetrievalResult] = field(default_factory=list)


@dataclass
class LocationScope:
    name: str
    place_ids: list[str] = field(default_factory=list)
    page_ids: list[str] = field(default_factory=list)


@dataclass
class ImageDistanceRanking:
    status: str
    method: str = "haversine"
    reference_entity_id: str | None = None
    reference_name: str | None = None
    reference_latitude: float | None = None
    reference_longitude: float | None = None
    ranked_images: int = 0
    unranked_images: int = 0


@dataclass
class SearchResponse:
    """Primary text or image matches; hybrid retains each source separately."""

    route: RetrievalRoute | AtomicRetrievalRoute
    results: list[SearchResult] = field(default_factory=list)
    related_results: list[SearchResult] = field(default_factory=list)
    location: str | None = None
    location_status: str = ""
    requested_categories: list[str] = field(default_factory=list)
    retrieval_status: str = ""
    branch_status: dict[str, str] = field(default_factory=dict)
    retrieval_warnings: list[str] = field(default_factory=list)
    maps_only: bool = False
    image_reranking: ImageDistanceRanking | None = None
