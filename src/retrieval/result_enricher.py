from copy import deepcopy
from math import isfinite
from pathlib import Path
from typing import Iterable
from urllib.parse import quote
from src.retrieval.google_metadata_catalog import GoogleMetadataCatalog
from src.data.google_payload import place_payload
from src.data.preprocessing.google_maps_photos import checked_photo_url
from src.schema.models import EnrichedRetrievalResult, RetrievalResult
from src.utils.utils import load_config, read_jsonl, resolve_path
from src.utils.retrieval_activity import tracked_tool


class ResultEnricher:
    def __init__(
        self,
        pages_path: str | Path | None = None,
        geo_tags_path: str | Path | None = None,
        store=None,
        catalog=None,
    ):
        config = load_config()
        self.pages_path = resolve_path(pages_path, config["wikivoyage_pages_path"])
        self.geo_tags_path = resolve_path(geo_tags_path, config["wikivoyage_geo_tags_path"])
        if store is not None and catalog is not None:
            raise ValueError("Provide either a PostgreSQL store or a Google catalog, not both.")
        self.catalog = catalog if catalog is not None else GoogleMetadataCatalog(store=store)
        self._owns_catalog = catalog is None
        self._pages: dict[str, dict] | None = None
        self._geo_tags: dict[str, dict] | None = None

    @staticmethod
    def _validate_sources(results: list[RetrievalResult], allowed: set[str]) -> None:
        for result in results:
            if result.source not in allowed:
                raise ValueError(f"Unsupported retrieval source: {result.source!r}; expected {sorted(allowed)}.")

    @staticmethod
    def _load_pages(path: Path) -> dict[str, dict]:
        records = {}
        for record in read_jsonl(path):
            page_id = str(record["page_id"])
            records[page_id] = record
        return records

    @staticmethod
    def _coordinates(*records: dict) -> tuple[float | None, float | None]:
        for record in records:
            lat = record.get("latitude", record.get("lat"))
            lon = record.get("longitude", record.get("lon"))
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (lat, lon)):
                continue
            if isfinite(lat) and isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
                return float(lat), float(lon)
        return None, None

    def enrich_image_results(self, results: Iterable[RetrievalResult]) -> list[EnrichedRetrievalResult]:
        results = list(results)
        self._validate_sources(results, {"image"})
        if not results:
            return []
        places, images = self.catalog.lookup(results)
        enriched = []
        for result in results:
            if result.payload.get("place_id") != result.place_id:
                raise ValueError("Google photo payload belongs to a different place.")
            place = places.get(result.place_id)
            image = images.get(str(result.point_id))
            if image is not None and image["place_id"] != result.place_id:
                raise ValueError("Google photo UUID belongs to a different PostgreSQL location.")
            latitude, longitude = self._coordinates(place or {})
            if image is not None:
                checked_photo_url(image["image_url"])
            missing = []
            if place is None:
                missing.append("place")
            if image is None:
                missing.append("image")
            if latitude is None:
                missing.append("coordinates")
            enriched.append(EnrichedRetrievalResult(
                place_id=result.place_id, point_id=result.point_id, score=result.score,
                source="image", payload=deepcopy(result.payload), entity_id="google:" + result.place_id,
                name=(place or {}).get("place_name"), latitude=latitude, longitude=longitude,
                url=(place or {}).get("google_maps_url"), category=(place or {}).get("category"),
                province=(place or {}).get("province"), address=(place or {}).get("address"),
                image=deepcopy(image), metadata=deepcopy({"place": place, "image": image}),
                missing_metadata=missing,
            ))
        return enriched

    @staticmethod
    @tracked_tool("Qdrant payload", "Read place metadata and Google Maps links")
    def enrich_image_places_from_payload(results):
        """Maps-only mode: no photo lookup, file checks, or external Google calls."""
        results = list(results)
        ResultEnricher._validate_sources(results, {"image"})
        enriched = []
        for result in results:
            place = place_payload(result.payload)
            if place.get("place_id") != result.place_id:
                raise ValueError("Google photo payload belongs to a different place.")
            latitude, longitude = ResultEnricher._coordinates(place.get("coordinates") or {}, place)
            missing = []
            if not place.get("place_name"):
                missing.append("name")
            if not place.get("google_maps_url"):
                missing.append("google_maps_url")
            if latitude is None:
                missing.append("coordinates")
            enriched.append(EnrichedRetrievalResult(
                place_id=result.place_id, point_id=result.point_id, score=result.score,
                source="image", payload=place, entity_id="google:" + result.place_id,
                name=place.get("place_name"), latitude=latitude, longitude=longitude,
                url=place.get("google_maps_url"), category=place.get("category"),
                province=place.get("province"), address=place.get("address"),
                metadata={"place": deepcopy(place)}, missing_metadata=missing,
            ))
        return enriched

    @tracked_tool("Local Wikivoyage metadata", "Read destination metadata")
    def enrich_text_results(
        self,
        results: Iterable[RetrievalResult]
    ) -> list[EnrichedRetrievalResult]:
        
        results = list(results)
        self._validate_sources(results, {"text"})

        if not results:
            return []
        
        if self._pages is None:
            self._pages = self._load_pages(self.pages_path)

        if self._geo_tags is None:
            self._geo_tags = self._load_pages(self.geo_tags_path)

        enriched = []
        for result in results:
            page_id = str(result.place_id)
            page = self._pages.get(page_id)
            geo_tag = self._geo_tags.get(page_id)
            title = (page or {}).get("title") or result.payload.get("title")
            latitude, longitude = self._coordinates(geo_tag or {}, result.payload)
            url = result.payload.get("url")

            if not url and title:
                url = "https://en.wikivoyage.org/wiki/" + quote(title.replace(" ", "_"), safe="/")
            missing = []

            if page is None:
                missing.append("page")

            if latitude is None:
                missing.append("coordinates")
                
            enriched.append(EnrichedRetrievalResult(
                place_id=result.place_id,
                score=result.score,
                source=result.source,
                payload=deepcopy(result.payload),
                entity_id=f"wikivoyage:{page_id}",
                name=title,
                latitude=latitude,
                longitude=longitude, url=url,
                metadata=deepcopy({"page": page, "geo_tag": geo_tag}),
                missing_metadata=missing,
            ))
        return enriched


    def close(self) -> None:
        if self._owns_catalog:
            self.catalog.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
