from collections import defaultdict
from pathlib import Path

from src.database.sqlite_metadata_store import SQLiteStore
from src.utils.utils import get_sha256, load_config, read_json, read_jsonl, resolve_path
from src.data.preprocessing.scraped_images import find_image


class MetadataIndexer:
    def __init__(
        self,
        database_path: str | Path | None = None,
        places_path: str | Path | None = None,
        manifest_path: str | Path | None = None,
        photos_directory: str | Path | None = None,
        batch_size: int | None = None,
        collection_name: str = "scraped_data",
        store: SQLiteStore | None = None,
    ):
        self.config = load_config()
        self.batch_size = batch_size if batch_size is not None else self.config["metadata_index_batch_size"]
        self.places_path = resolve_path(places_path, self.config["scraped_places_path"])
        self.manifest_path = resolve_path(manifest_path, self.config["image_manifest_path"])
        self.photos_directory = resolve_path(photos_directory, self.config["scraped_photos_directory"])

        self.collection_name = collection_name

        self.store = store if store is not None else SQLiteStore(
            path=database_path,
            collection_name=collection_name,
            batch_size=self.batch_size,
        )

        self.database_path = self.store.path


    read_jsonl = staticmethod(read_jsonl)


    def prepare_images(self) -> list[dict]:
        images = {}
        provenance_from_manifest = set()

        for record in self.read_jsonl(self.manifest_path):
            image_id = record["image_id"]

            if image_id in images:
                continue

            images[image_id] = {
                **record,
                "place_ids": sorted(set(record["place_ids"])),
                "provenance": list(record.get("provenance", [])),
            }

            if "provenance" in record:
                provenance_from_manifest.add(image_id)

        if len(provenance_from_manifest) == len(images):
            return list(images.values())

        for sidecar in sorted(self.photos_directory.rglob("*.json")):
            metadata = read_json(sidecar)

            image_path = find_image(sidecar, metadata)
            if image_path is None:
                raise FileNotFoundError(f"Missing image for sidecar: {sidecar}")

            image_id = get_sha256(image_path)
            place_id = (metadata.get("osm_id") or metadata.get("place_id"))

            image = images.get(image_id)

            if image is None:
                continue

            if place_id not in image["place_ids"]:
                continue

            if image_id in provenance_from_manifest:
                continue

            image["provenance"].append(
                {
                    "place_id": place_id,
                    "path": str(image_path),
                    "metadata": metadata,
                }
            )

        return list(images.values())


    def prepare_places(self, images: list[dict]) -> list[dict]:
        image_ids_by_place = defaultdict(set)

        for image in images:
            for place_id in image["place_ids"]:
                image_ids_by_place[place_id].add(image["image_id"])

        places = []
        seen_place_ids = set()

        for record in self.read_jsonl(self.places_path):
            place_id = record["osm_id"]

            if place_id in seen_place_ids:
                continue

            seen_place_ids.add(place_id)

            places.append(
                {
                    **record,
                    "image_ids": sorted(
                        image_ids_by_place[place_id]
                    ),
                }
            )

        return places


    def index(self, *, replace_existing: bool = False) -> dict[str, int]:
        images = self.prepare_images()
        places = self.prepare_places(images)

        self.store.create_indexes()
        self.store.upsert_metadata(places, images, replace_existing=replace_existing)

        result = {
            "places": len(places),
            "images": len(images),
            "associations": sum(len(image["place_ids"]) for image in images),
        }

        print(f"Imported {result['places']:,} places.")
        print(f"Imported {result['images']:,} unique images.")
        print(f"Imported {result['associations']:,} image/place associations.")
        print(f"Database: {self.database_path}")

        return result


    def close(self) -> None:
        self.store.close()
