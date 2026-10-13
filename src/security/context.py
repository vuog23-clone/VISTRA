"""Quarantine poisoned sources and restrict RAG context and rendered answers."""

import json
import re
from math import isfinite
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.security.guardrails import MAX_CONTEXT_BYTES, MAX_ANSWER_CHARS, UnsafeInput, validate_text
from src.utils.redaction import CredentialRedactor
from src.schema.weather import WeatherEvidence, WEATHER_SOURCE_URL
from src.schema.planning import EvidenceNotebook


_URL = re.compile(r"https?://[^\s<>\[\]{}\"`]+", re.I)
_HTML = re.compile(r"</?[A-Za-z][^>]*>|<!--|<!DOCTYPE", re.I)
_IMAGE = re.compile(r"!\[|!\s*\[", re.I)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Excerpt(StrictModel):
    section: str | None = None
    text: str


class Price(StrictModel):
    min: float | int | None = Field(default=None, ge=0, allow_inf_nan=False)
    max: float | int | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str | None = None


class Window(StrictModel):
    open: str | None = None
    close: str | None = None


class Hours(StrictModel):
    timezone: str | None = None
    monday: list[Window] | None = Field(default=None, max_length=8)
    tuesday: list[Window] | None = Field(default=None, max_length=8)
    wednesday: list[Window] | None = Field(default=None, max_length=8)
    thursday: list[Window] | None = Field(default=None, max_length=8)
    friday: list[Window] | None = Field(default=None, max_length=8)
    saturday: list[Window] | None = Field(default=None, max_length=8)
    sunday: list[Window] | None = Field(default=None, max_length=8)


class Place(StrictModel):
    address: str | None = None
    province: str | None = None
    category: str | None = None
    rating: float | int | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    review_count: int | None = Field(default=None, ge=0)
    price_range: Price | None = None
    opening_hours: Hours | None = None


class Photo(StrictModel):
    url: str = Field(max_length=4096)
    input_status: Literal["pending", "attached", "unavailable", "disabled", "omitted_budget"] = "pending"


class Source(StrictModel):
    source_id: str = Field(pattern=r"^S[1-9][0-9]?$", max_length=3)
    entity_id: str = Field(max_length=180)
    source: Literal["text", "image"]
    name: str | None = None
    url: str | None = None
    latitude: float | int | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | int | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    missing_metadata: list[str] = Field(default_factory=list, max_length=10)
    excerpts: list[Excerpt] = Field(default_factory=list, max_length=10)
    place_metadata: Place | None = None
    metadata_source: str | None = None
    retrieval_basis: Literal["image_similarity", "text_to_image_similarity", "metadata_lookup"] | None = None
    photo_match_note: str | None = None
    distance_km: float | int | None = Field(default=None, ge=0, le=21000, allow_inf_nan=False)
    photo: Photo | None = None


class ImageRanking(StrictModel):
    status: Literal["complete", "partial", "skipped_no_images", "skipped_no_reference"]
    method: Literal["haversine"] = "haversine"
    reference_entity_id: str | None = Field(default=None, max_length=180)
    reference_name: str | None = None
    reference_latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    reference_longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    ranked_images: int = Field(default=0, ge=0, le=100)
    unranked_images: int = Field(default=0, ge=0, le=100)


class Context(StrictModel):
    route: Literal["text", "hybrid"]
    location: str | None = None
    location_status: str = ""
    retrieval_warnings: list[str] = Field(default_factory=list, max_length=20)
    branch_status: dict[str, str] = Field(default_factory=dict)
    guardrail_warnings: list[str] = Field(default_factory=list, max_length=20)
    sources: list[Source] = Field(max_length=10)
    weather: WeatherEvidence | None = None
    notebook: EvidenceNotebook | None = None
    image_reranking: ImageRanking | None = None


def checked_source_url(value, source):
    if value is None:
        return None
    value = validate_text(value, "source URL", max_chars=2048, reject_secrets=False)
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == "https" and parts.port in (None, 443)
                 and not parts.username and not parts.password and "\\" not in value
                 and not any(c.isspace() for c in value))
        if source == "text":
            valid &= (parts.hostname == "en.wikivoyage.org" and parts.path.startswith("/wiki/")
                      and not parts.query)
        else:
            valid &= (parts.hostname in {"www.google.com", "google.com", "maps.google.com"}
                      and parts.path.startswith("/maps/place/"))
    except ValueError:
        valid = False
    if not valid:
        raise UnsafeInput("unsafe_source_url", "Source links must belong to the indexed travel providers.")
    return value


def _clean_json(value, *, budget=None, depth=0, key=None):
    budget = [0, 0] if budget is None else budget
    budget[0] += 1
    if depth > 12 or budget[0] > 4096:
        raise UnsafeInput("context_structure", "Source context exceeds its structural limits.")
    if isinstance(value, str):
        budget[1] += len(value.encode("utf-8", errors="surrogatepass"))
        if budget[1] > MAX_CONTEXT_BYTES:
            raise UnsafeInput("context_size", "Source context exceeds its byte limit.")
        if not value.strip():
            return ""
        value = validate_text(value, "source context", max_chars=8000, reject_secrets=False)
        if _HTML.search(value) or _IMAGE.search(value):
            raise UnsafeInput("active_source_content", "Source context contains active markup.")
        if key != "url":
            value = _URL.sub("[external link omitted]", value)
        return value
    if isinstance(value, dict) and len(value) <= 100:
        if any(not isinstance(k, str) or len(k) > 80 for k in value):
            raise UnsafeInput("context_structure", "Invalid source context keys.")
        return {k: _clean_json(v, budget=budget, depth=depth + 1, key=k) for k, v in value.items()}
    if isinstance(value, list) and len(value) <= 200:
        return [_clean_json(v, budget=budget, depth=depth + 1) for v in value]
    if type(value) is int and not -(2**63) <= value <= 2**63 - 1:
        raise UnsafeInput("context_number", "Source integers exceed their numeric limit.")
    if type(value) is float and not isfinite(value):
        raise UnsafeInput("context_number", "Source numbers must be finite.")
    if value is None or type(value) in (bool, int, float):
        return value
    raise UnsafeInput("context_structure", "Source context must contain bounded JSON data.")


def prepare_context(context):
    """Copy, redact, validate, and quarantine unsafe sources without provider calls."""
    if not isinstance(context, dict) or not isinstance(context.get("sources"), list):
        raise UnsafeInput("invalid_context", "RAG requires a structured source context.")
    if len(context["sources"]) > 10:
        raise UnsafeInput("context_size", "RAG accepts at most ten sources.")
    redactor = CredentialRedactor()
    # Sanitize before recursive redaction so attacker-controlled nesting cannot
    # exhaust the Python stack. Redact strings individually inside the walk.
    def redact_bounded(value):
        if isinstance(value, str):
            return redactor.text(value)
        if isinstance(value, dict):
            return {k: redact_bounded(v) for k, v in value.items()}
        if isinstance(value, list):
            return [redact_bounded(v) for v in value]
        return value
    budget = [0, 0]
    header = _clean_json({k: v for k, v in context.items() if k != "sources"}, budget=budget)
    safe = []
    warnings = []
    seen = set()
    for raw in context["sources"]:
        try:
            source = Source.model_validate(redact_bounded(_clean_json(raw, budget=budget)))
            if source.source_id in seen:
                raise UnsafeInput("duplicate_source", "Source IDs must be unique.")
            seen.add(source.source_id)
            prefix = "wikivoyage:" if source.source == "text" else "google:"
            if not source.entity_id.startswith(prefix):
                raise UnsafeInput("source_identity", "Source identity does not match its dataset.")
            source.url = checked_source_url(source.url, source.source)
            if source.source == "text" and (not source.excerpts or source.place_metadata is not None or source.photo is not None):
                raise UnsafeInput("source_evidence", "Guide sources require excerpts and their own facts.")
            if source.source == "image" and source.excerpts:
                raise UnsafeInput("source_evidence", "Photo sources cannot supply guide excerpts.")
            if source.photo is not None:
                from src.data.preprocessing.google_maps_photos import checked_photo_url
                try:
                    source.photo.url = checked_photo_url(source.photo.url)
                except ValueError:
                    raise UnsafeInput("unsafe_photo_url", "Retrieved photos must use trusted Google image hosts.") from None
            safe.append(source.model_dump(exclude_unset=True))
        except (UnsafeInput, ValidationError):
            warnings.append("A retrieved source was excluded by safety validation.")
    header["sources"] = safe
    header["guardrail_warnings"] = (header.get("guardrail_warnings", []) + warnings)[:20]
    try:
        cleaned = Context.model_validate(redact_bounded(header)).model_dump(exclude_unset=True)
        serialized = json.dumps(cleaned, ensure_ascii=False, allow_nan=False)
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise UnsafeInput("invalid_context", "RAG context failed schema validation.") from None
    if len(serialized.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise UnsafeInput("context_size", "RAG context exceeds its byte limit.")
    return cleaned


def answer_safety_issues(answer, context):
    try:
        answer = validate_text(answer, "answer", max_chars=MAX_ANSWER_CHARS,
                               check_injection=False, reject_secrets=True)
    except UnsafeInput:
        return ["The answer contains unsafe characters, exposed credentials, or excessive content."]
    visible = re.sub(r"<(https?://[^>\n]+)>", r"\1", answer)
    if _HTML.search(visible) or _IMAGE.search(answer):
        return ["Answers must not contain HTML or image embeds."]
    # Disallow executable/local URI schemes and reference-style Markdown links;
    # source URLs are sufficient for this pipeline's answer format.
    if re.search(r"\b(?:javascript|vbscript|file|blob|ftp|mailto)\s*:|\bdata\s*:\s*(?:[a-z]+/[a-z.+-]+|;base64)|^\s*\[[^\]]+\]:", answer, re.I | re.M):
        return ["The answer contains an unsafe link."]
    allowed = {source.get("url") for source in context["sources"] if source.get("url")}
    if context.get('notebook'):
        try:
            notebook = EvidenceNotebook.model_validate(context['notebook'])
            allowed.update(record.url for record in notebook.records().values() if record.url)
            if any(entry.weather and entry.weather.days for entry in notebook.entries):
                allowed.add(WEATHER_SOURCE_URL)
        except ValidationError:
            return ['Planning notebook evidence is invalid.']
    if context.get('weather'):
        try:
            weather = WeatherEvidence.model_validate(context['weather'])
            if weather.days:
                allowed.add(WEATHER_SOURCE_URL)
        except ValidationError:
            return ['Weather evidence is invalid.']
    remainder = answer
    for url in sorted(allowed, key=len, reverse=True):
        remainder = re.sub(r"(?<!!)\[[^\]\n]*\]\(" + re.escape(url) + r"\)", "[source link]", remainder)
    if re.search(r"\[[^\]\n]*\]\s*\(", remainder):
        return ["Answer links must exactly match supplied source URLs."]
    for match in _URL.finditer(answer):
        url = match.group()
        if not any(url == known or (url.startswith(known) and url[len(known):]
                    and all(char in ".,;!?)" for char in url[len(known):])) for known in allowed):
            return ["The answer contains a URL outside the supplied sources."]
    # GFM renderers also link bare www domains. They must appear inside a
    # complete, already-validated HTTPS source URL.
    without_urls = _URL.sub("", answer)
    if re.search(r"\bwww\.[^\s<>]+", without_urls, re.I):
        return ["Bare domain links are not allowed in answers."]
    return []
