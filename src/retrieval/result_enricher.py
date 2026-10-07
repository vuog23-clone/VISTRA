from copy import deepcopy
from math import isfinite
from pathlib import Path
import re
from typing import Iterable
from urllib.parse import quote
from src.database.sqlite_metadata_store import SQLiteStore
from src.schema.models import EnrichedRetrievalResult, RetrievalResult
from src.utils.utils import load_config, read_jsonl, resolve_path


class ResultEnricher:
    def __init__(
        self,
        database_path: str | Path | None = None,
        pages_path: str | Path | None = None,
        geo_tags_path: str | Path | None = None,
        store: SQLiteStore | None = None,
    ):
        config = load_config()
        self.database_path = resolve_path(database_path, config["sqlite_path"])
        self.pages_path = resolve_path(pages_path, config["wikivoyage_pages_path"])
        self.geo_tags_path = resolve_path(geo_tags_path, config["wikivoyage_geo_tags_path"])
        self._store = store
        self._owns_store = store is None
        self._pages: dict[str, dict] | None = None
        self._geo_tags: dict[str, dict] | None = None

    @staticmethod
    def _validate_sources(results: list[RetrievalResult], allowed: set[str]) -> None:
        for result in results:
            if result.source not in allowed:
                raise ValueError(f"Unsupported retrieval source: {result.source!r}; expected {sorted(allowed)}.")

    def _image_store(self) -> SQLiteStore:
        if self._store is None:
            self._store = SQLiteStore(path=self.database_path, read_only=True)
        return self._store

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

    @staticmethod
    def _matching_image(
        result: RetrievalResult,
        image: dict | None
    ) -> dict | None:
        
        image_id = result.payload.get("image_id")
        path = result.payload.get("image_path")

        if image is None and image_id is None and path is None:
            return None
        
        metadata = (image or {}).get("metadata") or {}

        for provenance in (image or {}).get("provenance", []):
            if str(provenance.get("place_id")) == str(result.place_id):
                metadata = {**metadata, **(provenance.get("metadata") or {})}
                break

        return {
            "image_id": image_id,
            "image_path": path or (image or {}).get("path"),
            "license": metadata.get("license") or result.payload.get("license"),
            "author": metadata.get("author") or result.payload.get("author"),
            "attribution": metadata.get("attribution") or result.payload.get("attribution"),
            "source_url": metadata.get("source_url") or metadata.get("url") or result.payload.get("source_url"),
        }

    def enrich_image_results(
         self,
        results: Iterable[RetrievalResult]
    ) -> list[EnrichedRetrievalResult]:

        results = list(results)
        self._validate_sources(results, {"image"})

        if not results:
            return []
        
        store = self._image_store()
        places, images = {}, {}
        enriched = []

        for result in results:
            place_id = str(result.place_id)

            if place_id not in places:
                places[place_id] = store.get_place(place_id)

            place = places[place_id]
            image_id = result.payload.get("image_id")

            if image_id is not None and image_id not in images:
                images[image_id] = store.get_image(image_id)

            image = images.get(image_id)
            latitude, longitude = self._coordinates((place or {}).get("coordinates") or {})
            missing = []

            if place is None:
                missing.append("place")

            if image is None:
                missing.append("image")

            if latitude is None:
                missing.append("coordinates")

            enriched.append(
                EnrichedRetrievalResult(
                    place_id=result.place_id,
                    score=result.score,
                    source=result.source,
                    payload=deepcopy(result.payload),
                    entity_id=f"osm:{place_id}",
                    name=(place or {}).get("name"),
                    latitude=latitude,
                    longitude=longitude,
                    url=(place or {}).get("url") or (
                        f"https://www.openstreetmap.org/{place_id}"
                        if re.fullmatch(r"(?:node|way|relation)/\d+", place_id) else None
                ),
                category=(place or {}).get("category"),
                province=(place or {}).get("province"),
                address=(place or {}).get("address"),
                image=self._matching_image(result, image),
                metadata=deepcopy({"place": place, "image": image}),
                missing_metadata=missing,
            ))
        return enriched

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

    def enrich_results(
        self,
        results: Iterable[RetrievalResult]
    ) -> list[EnrichedRetrievalResult]:
        
        results = list(results)
        self._validate_sources(results, {"image", "text"})
        by_source = {
            "image": iter(self.enrich_image_results(r for r in results if r.source == "image")),
            "text": iter(self.enrich_text_results(r for r in results if r.source == "text")),
        }
        return [next(by_source[result.source]) for result in results]

    def close(self) -> None:
        if self._owns_store and self._store is not None:
            self._store.close()
            self._store = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
