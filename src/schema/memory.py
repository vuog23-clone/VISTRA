"""Bounded conversation state; memory is context, never retrieved travel evidence."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.security.guardrails import UnsafeInput, validate_text
from src.utils.redaction import CredentialRedactor


PreferenceKey = Literal["diet", "pace", "interests", "lodging", "transport", "language",
                        "home_location", "accessibility", "travel_style"]


def checked_identity(value, name):
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,128}", value):
        raise UnsafeInput("invalid_memory_identity", f"{name} must be a nonblank identifier of at most 128 characters.")
    return value


def memory_text(value, *, maximum=6000):
    return validate_text(CredentialRedactor().text(value), "memory", max_chars=maximum,
                         check_injection=False)


class MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TripDetails(MemoryModel):
    destination: str | None = Field(default=None, max_length=160)
    dates: str | None = Field(default=None, max_length=300)
    start_date: str | None = Field(default=None, min_length=10, max_length=10)
    end_date: str | None = Field(default=None, min_length=10, max_length=10)
    duration_days: int | None = Field(default=None, ge=1, le=14)
    origin: str | None = Field(default=None, max_length=160)
    destinations: list[str] = Field(default_factory=list, max_length=3)
    budget_amount: float | int | None = Field(default=None, ge=0, le=1e12, allow_inf_nan=False)
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    budget_basis: Literal['total', 'per_person'] = 'total'
    cuisines: list[str] = Field(default_factory=list, max_length=8)
    room_type: str | None = Field(default=None, max_length=80)
    room_rules: list[str] = Field(default_factory=list, max_length=8)
    transport_constraints: list[Literal['no_flight', 'no_driving']] = Field(default_factory=list, max_length=2)
    transport_mode: Literal['driving', 'taxi', 'public_transit', 'walking'] | None = None
    budget: str | None = Field(default=None, max_length=300)
    travelers: int | None = Field(default=None, ge=1, le=100)
    requirements: list[str] = Field(default_factory=list, max_length=20)
    goal: str | None = Field(default=None, max_length=1500)
    pending_clarification: str | None = Field(default=None, max_length=1000)
    pending_planning_action: Literal['create_itinerary', 'revise_itinerary'] | None = None
    include_weather: bool | None = None

    @model_validator(mode="after")
    def clean(self):
        from src.schema.weather import iso_date
        start = iso_date(self.start_date) if self.start_date else None
        end = iso_date(self.end_date) if self.end_date else None
        # User requirements may conflict while awaiting clarification. The
        # orchestrator resolves chronological conflicts before planning/fetching.
        for key in ("destination", "dates", "budget", "goal", "pending_clarification"):
            value = getattr(self, key)
            if value is not None:
                setattr(self, key, memory_text(value, maximum=1500))
        self.requirements = [memory_text(value, maximum=300) for value in self.requirements]
        for field in ('destinations', 'cuisines', 'room_rules'):
            setattr(self, field, [memory_text(value, maximum=160) for value in getattr(self, field)])
        return self


from src.schema.planning import Itinerary


class TripState(TripDetails):
    itinerary: Itinerary | None = None


class MemoryMessage(MemoryModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=24000)
    truncated: bool = False


class MemoryRecord(MemoryModel):
    kind: Literal["preference", "trip"]
    key: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=6000)
    source_quote: str = Field(min_length=1, max_length=1000)


class ConversationMemory(MemoryModel):
    summary: str = Field(default="", max_length=2000)
    trip_state: TripState = Field(default_factory=TripState)
    recent_messages: list[MemoryMessage] = Field(default_factory=list, max_length=20)
    long_term: list[MemoryRecord] = Field(default_factory=list, max_length=30)
    itinerary_available: bool = False

    @model_validator(mode="after")
    def clean(self):
        if self.summary:
            self.summary = memory_text(self.summary, maximum=2000)
        for message in self.recent_messages:
            message.content = memory_text(message.content, maximum=24000)
        for record in self.long_term:
            record.content = memory_text(record.content)
            record.source_quote = memory_text(record.source_quote, maximum=1000)
        if len(json.dumps(self.model_dump(), ensure_ascii=False).encode("utf-8")) > 128 * 1024:
            raise UnsafeInput("memory_context_size", "Conversation memory exceeds its context budget.")
        return self


class PreferenceChange(MemoryModel):
    operation: Literal["set", "delete"]
    key: PreferenceKey
    # Persist the user's actual words rather than a model-generated factual value.
    source_quote: str = Field(min_length=1, max_length=1000)


class TurnUnderstanding(MemoryModel):
    intent: Literal["memory", "travel", "other"] = "other"
    standalone_query: str = Field(min_length=1, max_length=4000)
    summary: str = Field(max_length=2000)
    trip_state: TripDetails
    action: Literal['answer', 'search', 'create_itinerary', 'revise_itinerary'] | None = None
    revision_day: int | None = Field(default=None, ge=1, le=14)
    include_weather: bool | None = None
    preference_changes: list[PreferenceChange] = Field(max_length=9)


class SessionSnapshot(MemoryModel):
    version: int = Field(default=0, ge=0)
    summary: str = Field(default="", max_length=2000)
    trip_state: TripState = Field(default_factory=TripState)
    recent_messages: list[MemoryMessage] = Field(default_factory=list, max_length=20)
