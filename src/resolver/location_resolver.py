from pathlib import Path
import re
import unicodedata

from src.database.sqlite_metadata_store import SQLiteStore
from src.schema.models import LocationScope
from src.utils.utils import load_config, read_json, read_jsonl, resolve_path


class LocationResolver:
    def __init__(
        self,
        store: SQLiteStore | None = None,
        mappings_path: str | Path | None = None,
        pages_path: str | Path | None = None,
    ):
        config = load_config()
        mappings_path = mappings_path or config.get("location_mappings_path")
        self.mappings = read_json(resolve_path(mappings_path)) if mappings_path else {}
        self.pages_path = resolve_path(pages_path, config["wikivoyage_pages_path"])
        self.database_path = resolve_path(config["sqlite_path"])
        self._store = store
        self._owns_store = store is None
        self._places: list[dict] | None = None
        self._pages: list[dict] | None = None


    @staticmethod
    def _normalize(value: str) -> str:
        value = unicodedata.normalize("NFKD", value.casefold().replace("đ", "d"))
        value = "".join(char for char in value if not unicodedata.combining(char))
        value = " ".join(re.sub(r"[^\w]+", " ", value.replace("_", " ")).split())
        value = re.sub(r"^(?:thanh pho|thi xa|tinh|huyen|quan|phuong|xa|tp|city of|province of) ", "", value)
        value = re.sub(r" (?:city|province|island)$", "", value)
        return value.replace(" ", "")


    def _load(self) -> None:
        if self._places is None:
            if self._store is None and self.database_path.is_file():
                self._store = SQLiteStore(path=self.database_path, read_only=True)
            self._places = [{
                "place_id": str(place["osm_id"]),
                "province": place.get("province") or "",
                "address": place.get("address") or "",
            } for place in self._store.list_places()] if self._store is not None else []
        if self._pages is None:
            self._pages = list(read_jsonl(self.pages_path)) if self.pages_path.is_file() else []


    def resolve(self, location: str) -> LocationScope | None:
        self._load()
        name, mapping = location.strip(), {}
        key = self._normalize(name)
        if not key:
            return None
        for candidate, record in self.mappings.items():
            if key in {self._normalize(alias) for alias in [candidate, *record.get("aliases", [])]}:
                name, mapping = candidate, record
                break
        aliases = {self._normalize(alias) for alias in [name, *mapping.get("aliases", [])]}
        known_ids = set(mapping.get("place_ids", []))
        place_ids = []
        for place in self._places:
            address = place["address"]
            labels = list(address.values()) if isinstance(address, dict) else re.split(r"[,;]", str(address))
            labels.append(place["province"])
            if place["place_id"] in known_ids or any(self._normalize(str(label)) in aliases for label in labels if label):
                place_ids.append(place["place_id"])
        page_ids = [str(page["page_id"]) for page in self._pages if self._normalize(page["title"]) in aliases]
        if not mapping and not place_ids and not page_ids:
            return None
        return LocationScope(name=name, place_ids=sorted(set(place_ids)), page_ids=page_ids)


    def close(self) -> None:
        if self._owns_store and self._store is not None:
            self._store.close()


def main() -> None:
    import argparse
    from contextlib import closing
    import json
    import sys

    from qdrant_client import QdrantClient, models
    from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
    from src.database.qdrant_connection import open_qdrant

    parser = argparse.ArgumentParser(description="Read-only location coverage audit; no models are loaded.")
    parser.add_argument("location", help="Destination name, for example Phu Quoc")
    parser.add_argument("--qdrant", action="store_true", help="Also check the existing Qdrant image collection")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    config = load_config()
    with closing(SQLiteStore(read_only=True)) as store, closing(LocationResolver(store=store)) as resolver:
        scope = resolver.resolve(args.location)
        images = {image["image_id"]: image for place_id in scope.place_ids
                  for image in store.get_images_for_place(place_id)} if scope else {}
        output = {
            "location": scope.name if scope else args.location,
            "resolved": scope is not None,
            "eligible_places": len(scope.place_ids) if scope else 0,
            "associated_images": len(images),
            "missing_local_images": [image_id for image_id, image in images.items() if not resolve_path(image["path"]).is_file()],
            "wikivoyage_page_ids": scope.page_ids if scope else [],
        }
        if args.qdrant and scope:
            try:
                with closing(open_qdrant(resolve_path(config["qdrant_images_path"]), config, QdrantClient)) as client:
                    points, offset = [], None
                    query_filter = models.Filter(must=[models.FieldCondition(key="place_id", match=models.MatchAny(any=scope.place_ids))])
                    while scope.place_ids:
                        batch, offset = client.scroll(
                            collection_name=config["scraped_images_collection"],
                            scroll_filter=query_filter, offset=offset, limit=100, with_vectors=False,
                        )
                        points.extend(batch)
                        if offset is None:
                            break
                    indexed = {point.payload.get("image_id") for point in points}
                    output["indexed_place_image_pairs"] = len(points)
                    output["unindexed_images"] = sorted(set(images) - indexed)
            except (ResponseHandlingException, UnexpectedResponse):
                output["qdrant_error"] = "Could not read the image collection. Start Docker/Qdrant and check qdrant_url."
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
