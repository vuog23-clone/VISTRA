from dataclasses import dataclass, field
from typing import Literal, TypedDict

from pydantic import BaseModel, Field

from src.schema.models import DestinationResolution, EnrichedRetrievalResult, RetrievalRoute, SearchResponse, SearchResult


def restore_search_response(value: dict | None) -> SearchResponse | None:
    if value is None:
        return None

    def restore_result(result: dict) -> SearchResult:
        destination = result.get("destination")
        return SearchResult(
            **{key: item for key, item in result.items() if key not in ("evidence", "destination")},
            evidence=[EnrichedRetrievalResult(**hit) for hit in result.get("evidence", [])],
            destination=DestinationResolution(**destination) if destination else None,
        )

    return SearchResponse(
        route=value["route"],
        results=[restore_result(result) for result in value["results"]],
        related_results=[restore_result(result) for result in value.get("related_results", [])],
        follow_up_status=value.get("follow_up_status", ""),
        location=value.get("location"),
        location_status=value.get("location_status", ""),
    )


class MemoryFact(BaseModel):
    name: str
    value: str


class ChatPlan(BaseModel):
    action: Literal["search", "reuse", "reply"]
    search_query: str
    use_previous_image: bool
    summary: str
    memory_updates: list[MemoryFact]
    reference: Literal["none", "selected", "ambiguous"] = "none"
    referenced_entity_ids: list[str] = Field(default_factory=list)
    location: str | None = None
    visual_query: str = ""


@dataclass
class Conversation:
    conversation_id: str
    user_id: str
    messages: list[dict] = field(default_factory=list)
    summary: str = ""
    search_response: SearchResponse | None = None
    image_path: str | None = None
    selected_entity_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict):
        return cls(
            **{key: item for key, item in value.items() if key != "search_response"},
            search_response=restore_search_response(value.get("search_response")),
        )


@dataclass
class ChatResponse:
    conversation_id: str
    answer: str
    search_query: str
    search_response: SearchResponse
    action: str
    images: list[dict] = field(default_factory=list)
    activity: list[dict] = field(default_factory=list)
    places: list[dict] = field(default_factory=list)


class ChatState(TypedDict):
    conversation: dict
    message: str
    image_path: str | None
    route: RetrievalRoute | None
    limit: int | None
    result: dict
