import re
import unicodedata
from math import asin, cos, isfinite, radians, sin, sqrt
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from src.schema.models import DestinationResolution, EnrichedRetrievalResult
from src.utils.utils import load_config, read_json, read_jsonl, resolve_path


class DestinationResolver:
    def __init__(
        self,
        pages_path: str | Path | None = None,
        geo_tags_path: str | Path | None = None,
        mappings_path: str | Path | None = None,
        *,
        max_distance_km: float | None = None,
    ):
        config = load_config()
        self.pages_path = resolve_path(pages_path, config["wikivoyage_pages_path"])
        self.geo_tags_path = resolve_path(geo_tags_path, config["wikivoyage_geo_tags_path"])
        if mappings_path is None:
            mappings_path = config["destination_mappings_path"]
        self.mappings_path = resolve_path(mappings_path) if mappings_path is not None else None

        if max_distance_km is None:
            max_distance_km = config["destination_max_distance_km"]
        self.max_distance_km = self._radius(max_distance_km)

        self._pages: dict[str, dict] | None = None
        self._coordinates: dict[str, tuple[float, float]] = {}
        self._aliases: dict[str, set[str]] = {}
        self._radii: dict[str, float] = {}
        self._place_mappings: dict[str, dict] = {}

    @staticmethod
    def _radius(value: float) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value <= 0
        ):
            raise ValueError("max_distance_km must be a finite positive number.")
        return float(value)

    @staticmethod
    def _page_id(value: str | int) -> str:
        if (
            isinstance(value, bool)
            or not isinstance(value, (str, int))
            or not re.fullmatch(r"[1-9]\d*", str(value))
        ):
            raise ValueError(f"Invalid Wikivoyage page_id: {value!r}")
        return str(value)

    @staticmethod
    def _place_id(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"Invalid OSM place_id: {value!r}")

        value = value.removeprefix("osm:")
        if not re.fullmatch(r"(?:node|way|relation)/[1-9]\d*", value):
            raise ValueError(f"Invalid OSM place_id: {value!r}")
        return value

    @staticmethod
    def _normalize(value: str) -> str:
        value = unicodedata.normalize("NFKD", value.casefold().replace("đ", "d"))
        value = "".join(char for char in value if not unicodedata.combining(char))
        return " ".join(re.sub(r"[^\w]+", " ", value.replace("_", " ")).split())

    @staticmethod
    def _valid_coordinates(
        latitude,
        longitude,
    ) -> tuple[float, float] | None:
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in (latitude, longitude)
        ):
            return None

        if (
            isfinite(latitude)
            and isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            return float(latitude), float(longitude)
        return None

    @staticmethod
    def _distance_km(
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> float:
        lat1, lon1, lat2, lon2 = map(radians, (*first, *second))
        value = (
            sin((lat2 - lat1) / 2) ** 2
            + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
        )
        return 6371.0088 * 2 * asin(sqrt(min(1.0, max(0.0, value))))

    def _load_pages(self) -> dict[str, dict]:
        pages = {}

        for page in read_jsonl(self.pages_path):
            page_id = self._page_id(page["page_id"])

            if page_id in pages:
                raise ValueError(f"Duplicate page_id {page_id!r} in {self.pages_path}")

            if not isinstance(page.get("title"), str) or not page["title"].strip():
                raise ValueError(f"Missing title for page {page_id}")

            pages[page_id] = page

        return pages

    def _load_coordinates(self, pages: dict[str, dict]) -> dict[str, tuple[float, float]]:
        coordinates = {}
        seen_geo_ids = set()

        for geo in read_jsonl(self.geo_tags_path):
            page_id = self._page_id(geo["page_id"])

            if page_id in seen_geo_ids:
                raise ValueError(f"Duplicate geo page_id {page_id!r} in {self.geo_tags_path}")

            seen_geo_ids.add(page_id)
            position = self._valid_coordinates(geo.get("latitude"), geo.get("longitude"))

            if position is not None and page_id in pages:
                coordinates[page_id] = position

        return coordinates

    def _load_mappings(
        self,
        pages: dict[str, dict],
    ) -> tuple[dict[str, set[str]], dict[str, float], dict[str, dict]]:
        config = {"version": 1, "destinations": [], "place_mappings": []}
        if self.mappings_path is not None:
            config = read_json(self.mappings_path)

        if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1:
            raise ValueError("Destination mapping file must have version 1.")

        for key in ("destinations", "place_mappings"):
            if not isinstance(config.get(key), list):
                raise ValueError(f"Destination mapping field {key!r} must be a list.")

        aliases = {}
        radii = {}
        mappings = {}

        for entry in config["destinations"]:
            if not isinstance(entry, dict):
                raise ValueError("Each destination must be an object.")

            page_id = self._page_id(entry["page_id"])

            if page_id not in pages:
                raise ValueError(f"Destination mapping references unknown page {page_id}")

            if page_id in radii:
                raise ValueError(f"Duplicate configured destination {page_id}")

            radii[page_id] = self._radius(entry.get("max_distance_km", self.max_distance_km))
            names = entry.get("aliases", [])

            if not isinstance(names, list) or any(
                not isinstance(name, str) or not self._normalize(name)
                for name in names
            ):
                raise ValueError(f"Invalid aliases for destination {page_id}")

            for name in [pages[page_id]["title"], *names]:
                aliases.setdefault(self._normalize(name), set()).add(page_id)

        for entry in config["place_mappings"]:
            if not isinstance(entry, dict):
                raise ValueError("Each place mapping must be an object.")

            place_id = self._place_id(entry["place_id"])
            page_id = self._page_id(entry["page_id"])

            if page_id not in pages:
                raise ValueError(f"Place mapping references unknown page {page_id}")

            if place_id in mappings:
                raise ValueError(f"Duplicate mapping for OSM place {place_id}")

            if not isinstance(entry.get("note"), str) or not entry["note"].strip():
                raise ValueError(f"Place mapping for {place_id} requires an evidence note.")

            mappings[place_id] = {**entry, "page_id": page_id}

        return aliases, radii, mappings

    def _load(self) -> None:
        if self._pages is not None:
            return

        pages = self._load_pages()
        coordinates = self._load_coordinates(pages)
        aliases, radii, mappings = self._load_mappings(pages)

        # Publish caches only after the entire catalog validates successfully.
        self._coordinates = coordinates
        self._aliases = aliases
        self._radii = radii
        self._place_mappings = mappings
        self._pages = pages

    def _match(
        self,
        result: EnrichedRetrievalResult,
        page_id: str,
        method: str,
        confidence: float,
        evidence: list[dict],
    ) -> DestinationResolution:
        page = self._pages[page_id]
        latitude, longitude = self._coordinates.get(page_id, (None, None))
        title = quote(page["title"].replace(" ", "_"), safe="/")

        return DestinationResolution(
            source_entity_id=result.entity_id,
            destination_id=f"wikivoyage:{page_id}",
            page_id=page_id,
            name=page["title"],
            latitude=latitude,
            longitude=longitude,
            url=f"https://en.wikivoyage.org/wiki/{title}",
            method=method,
            confidence=confidence,
            evidence=evidence,
        )

    @staticmethod
    def _unresolved(
        result: EnrichedRetrievalResult,
        reason: str,
        candidates: set[str] | None = None,
        evidence: list[dict] | None = None,
    ) -> DestinationResolution:
        return DestinationResolution(
            source_entity_id=result.entity_id,
            reason=reason,
            candidate_ids=[f"wikivoyage:{page_id}" for page_id in sorted(candidates or set())],
            evidence=evidence or [],
        )

    def _resolve_text(self, result: EnrichedRetrievalResult) -> DestinationResolution:
        page_id = self._page_id(result.place_id)

        if result.entity_id != f"wikivoyage:{page_id}":
            raise ValueError("Text entity_id conflicts with place_id.")

        payload_page_id = result.payload.get("page_id")
        if payload_page_id is not None and self._page_id(payload_page_id) != page_id:
            raise ValueError("Text payload page_id conflicts with place_id.")

        self._load()
        if page_id not in self._pages:
            return self._unresolved(result, "unknown_wikivoyage_page")

        return self._match(
            result,
            page_id,
            method="wikivoyage_identity",
            confidence=1.0,
            evidence=[{"page_id": page_id}],
        )

    def _resolve_image(self, result: EnrichedRetrievalResult) -> DestinationResolution:
        place_id = self._place_id(result.place_id)

        if result.entity_id != f"osm:{place_id}":
            raise ValueError("Image entity_id conflicts with place_id.")

        self._load()

        mapping = self._place_mappings.get(place_id)
        if mapping:
            return self._match(
                result,
                mapping["page_id"],
                method="curated_mapping",
                confidence=1.0,
                evidence=[{"place_id": place_id, "note": mapping["note"]}],
            )

        # Match whole administrative labels, never substrings or POI names.
        province_candidates = set()
        address_candidates = set()
        evidence = []
        labels = [("province", result.province)]

        if isinstance(result.address, str):
            labels.extend(("address", label.strip()) for label in result.address.split(","))

        for field, label in labels:
            if not isinstance(label, str):
                continue

            candidates = self._aliases.get(self._normalize(label), set())
            if not candidates:
                continue

            if field == "province":
                province_candidates.update(candidates)
            else:
                address_candidates.update(candidates)

            evidence.append({
                "field": field,
                "label": label,
                "candidate_ids": [f"wikivoyage:{page_id}" for page_id in sorted(candidates)],
            })

        candidates = province_candidates | address_candidates
        if not candidates:
            return self._unresolved(result, "no_location_label_match")

        if province_candidates and address_candidates:
            common_candidates = province_candidates & address_candidates
            if not common_candidates:
                return self._unresolved(result, "conflicting_location_labels", candidates, evidence)
            candidates = common_candidates

        position = self._valid_coordinates(result.latitude, result.longitude)
        if position is None:
            return self._unresolved(result, "missing_or_invalid_coordinates", candidates, evidence)

        eligible = set()
        for page_id in sorted(candidates):
            if page_id not in self._coordinates:
                evidence.append({
                    "destination_id": f"wikivoyage:{page_id}",
                    "reason": "missing_destination_coordinates",
                })
                continue

            distance = self._distance_km(position, self._coordinates[page_id])
            evidence.append({
                "destination_id": f"wikivoyage:{page_id}",
                "distance_km": distance,
                "max_distance_km": self._radii[page_id],
            })

            if distance <= self._radii[page_id]:
                eligible.add(page_id)

        if len(eligible) > 1:
            return self._unresolved(result, "ambiguous_destinations", eligible, evidence)

        # A same-name candidate with unknown coordinates cannot be ruled out.
        if eligible and any(page_id not in self._coordinates for page_id in candidates):
            return self._unresolved(result, "incomplete_destination_coordinates", candidates, evidence)

        if not eligible:
            if any(page_id in self._coordinates for page_id in candidates):
                reason = "outside_destination_radius"
            else:
                reason = "missing_destination_coordinates"
            return self._unresolved(result, reason, candidates, evidence)

        return self._match(
            result,
            eligible.pop(),
            method="location_label_and_proximity",
            confidence=0.8,
            evidence=evidence,
        )

    def resolve(self, result: EnrichedRetrievalResult) -> DestinationResolution:
        """Resolve one hit without mutating its identity, score, or evidence."""
        if not isinstance(result, EnrichedRetrievalResult):
            raise TypeError("DestinationResolver requires an enriched retrieval result.")

        if result.source not in {"text", "image"}:
            raise ValueError(f"Unsupported retrieval source: {result.source!r}")

        if result.source == "text":
            return self._resolve_text(result)

        return self._resolve_image(result)

    def resolve_results(
        self,
        results: Iterable[EnrichedRetrievalResult],
    ) -> list[DestinationResolution]:
        """Preserve input order and multiplicity; resolution does not rank or merge."""
        return [self.resolve(result) for result in results]


if __name__ == "__main__":
    from src.retrieval.result_enricher import ResultEnricher

    # Edit these values, then run: python -m src.resolver.destination_resolver
    source = "image"  # "text" or "image"
    query = "A place like this in Vietnam"
    image_path = r"E:\VISTRA\lmao.jpg"
    limit = 5

    print(f"Starting destination resolver example | Source: {source}", flush=True)
    if source == "text":
        from src.retrieval.text_retriever import TextRetriever

        print("Loading text retriever...", flush=True)
        with TextRetriever() as retriever:
            print("Retrieving text matches...", flush=True)
            hits = retriever.search(query, limit=limit)
    elif source == "image":
        from src.retrieval.image_retriever import ImageRetriever

        if not Path(image_path).is_file():
            raise FileNotFoundError(f"Example image not found: {image_path}")
        print("Loading image retriever and SigLIP model...", flush=True)
        with ImageRetriever() as retriever:
            print(f"Retrieving image matches for {image_path}...", flush=True)
            hits = retriever.search(image_path, limit=limit)
    else:
        raise ValueError('Set source to "text" or "image".')

    print(f"Retrieved {len(hits)} matches. Enriching metadata...", flush=True)
    with ResultEnricher() as enricher:
        enriched = enricher.enrich_results(hits)
    print(f"Resolving destinations for {len(enriched)} matches...", flush=True)
    resolutions = DestinationResolver().resolve_results(enriched)

    print(f"\nDestination resolutions: {len(resolutions)}")
    for index, (hit, resolution) in enumerate(zip(enriched, resolutions), start=1):
        print(f"\n{index}. {hit.name} | {hit.entity_id} | Retrieval score: {hit.score:.4f}")
        print(f"Resolved: {resolution.resolved} | Method: {resolution.method}")
        if resolution.resolved:
            print(f"Destination: {resolution.name} | {resolution.destination_id}")
            print(f"Coordinates: {resolution.latitude}, {resolution.longitude}")
            print(f"URL: {resolution.url}")
        else:
            print(f"Reason: {resolution.reason}")
            print(f"Candidates: {resolution.candidate_ids}")
        print(f"Evidence: {resolution.evidence}")
