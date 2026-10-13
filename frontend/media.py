"""Place cards retain the identity pairing established by retrieval enrichment."""

from math import isfinite
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.data.preprocessing.google_maps_photos import checked_photo_url
from src.security.context import Hours, Price, checked_source_url
from src.security.guardrails import UnsafeInput
from src.schema.weather import WeatherEvidence
from src.schema.planning import Itinerary, PlanningReport


class PlaceCard(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    place_id: str = Field(min_length=1, max_length=180)
    name: str = Field(min_length=1, max_length=300)
    address: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=150)
    province: str | None = Field(default=None, max_length=300)
    rating: float | int | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    review_count: int | None = Field(default=None, ge=0)
    latitude: float | int | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | int | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    distance_km: float | int | None = Field(default=None, ge=0, allow_inf_nan=False)
    price_range: Price | None = None
    opening_hours: Hours | None = None
    maps_url: str | None = Field(default=None, max_length=4096)
    photo_url: str | None = Field(default=None, max_length=4096)
    source_id: str | None = Field(default=None, pattern=r"^(S[1-9][0-9]?|E[1-9][0-9]?R[1-9][0-9]?)$", max_length=6)


class ReplyMedia(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cards: list[PlaceCard] = Field(default_factory=list, max_length=20)
    sources: list[dict[str, Annotated[str | None, Field(max_length=4096)]]] = Field(default_factory=list, max_length=72)
    weather: WeatherEvidence | None = None
    itinerary: Itinerary | None = None
    planning: PlanningReport | None = None


def build_place_cards(result, limit=20):
    retrieval = result.get("retrieval") or {}
    if retrieval.get("maps_only"):
        return []
    source_ids = {source.get("entity_id"): source["source_id"]
                  for source in (result.get("context") or {}).get("sources", [])}
    if result.get('notebook') and result.get('itinerary'):
        from src.utils.reply_sources import reply_sources
        source_ids.update({source['entity_id']:source['source_id'] for source in reply_sources(result)
                           if source.get('entity_id') and source['source_id'].startswith('E')})
    hits = [*retrieval.get("results", []), *retrieval.get("related_results", [])]
    cards, seen = [], set()
    def text(value, maximum):
        return value[:maximum] if isinstance(value, str) and value.strip() else None
    def number(value, minimum=0, maximum=float("inf")):
        return value if type(value) in (int, float) and isfinite(value) and minimum <= value <= maximum else None
    def structured(model, value):
        try:
            return model.model_validate(value) if isinstance(value, dict) else None
        except ValidationError:
            return None
    for hit in hits:
        place_id = hit.get("place_id")
        if hit.get("source") != "image" or not isinstance(place_id, str) or place_id in seen:
            continue
        place = (hit.get("metadata") or {}).get("place") or hit.get("payload") or {}
        if place.get("place_id") != place_id:
            continue
        photo = hit.get("image") or {}
        photo_url = None
        if photo.get("place_id") == place_id and photo.get("image_url"):
            try:
                photo_url = checked_photo_url(photo["image_url"])
            except (ValueError, UnsafeInput):
                pass
        try:
            maps_url = checked_source_url(hit.get("url"), "image")
        except UnsafeInput:
            maps_url = None
        count = place.get("review_count")
        try:
            card = PlaceCard(place_id=place_id, name=text(hit.get("name"), 300) or text(place.get("place_name"), 300) or "Unnamed place",
                address=text(hit.get("address"), 2000), category=text(hit.get("category"), 150),
                province=text(hit.get("province"), 300), rating=number(place.get("rating"), 0, 5),
                review_count=count if type(count) is int and count >= 0 else None,
                latitude=number(hit.get("latitude"), -90, 90), longitude=number(hit.get("longitude"), -180, 180),
                distance_km=number(hit.get("distance_km")),
                price_range=structured(Price, place.get("price_range")), opening_hours=structured(Hours, place.get("opening_hours")),
                maps_url=maps_url, photo_url=photo_url, source_id=source_ids.get(hit.get("entity_id")))
        except ValidationError:
            continue
        cards.append(card.model_dump())
        seen.add(place_id)
        if len(cards) >= limit:
            break
    return cards


def public_media(media, session_id, version):
    """Expose an owner-authorized proxy path rather than accepting arbitrary URLs."""
    from urllib.parse import quote
    checked = ReplyMedia.model_validate(media)
    cards = []
    for index, card in enumerate(checked.cards):
        data = card.model_dump(exclude={"photo_url"})
        data["image_url"] = (f"/api/photos/{quote(session_id, safe='')}/{version}/{index}"
                             if card.photo_url is not None else None)
        cards.append(data)
    return {"cards": cards, "sources": checked.sources, "weather": checked.weather.model_dump() if checked.weather else None,
            'itinerary': checked.itinerary.model_dump() if checked.itinerary else None,
            'planning': checked.planning.model_dump() if checked.planning else None}
