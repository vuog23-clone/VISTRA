"""Index public Maps cover photos from metadata; never save dataset photos.

Preview: python -m src.data.indexing.google_maps_indexer
Index:   python -m src.data.indexing.google_maps_indexer --apply
"""

import argparse
import json
from pathlib import Path

import httpx

from src.data.preprocessing.google_maps_photos import GoogleMapsPhotoResolver, fetch_photo, load_places, photo_identity
from src.database.postgres_google_metadata_store import PostgresGoogleMetadataStore
from src.database.qdrant_google_image_store import GoogleImageStore
from src.utils.utils import ensure_parent, load_config, resolve_path


def index_maps_photos(dataset_path=None, *, apply=False, place_ids=None, limit=None,
                      resolver=None, fetcher=fetch_photo, embedder=None,
                      vector_store=None, metadata_store=None, on_progress=None,
                      workers=1, on_checkpoint=None):
    """Preview locally; explicit apply resolves/embeds remote pixels.

    Import all places, including places whose photo is unavailable. Index one
    visible cover photo per selected place. Stable IDs allow retries to reuse
    vectors and refresh metadata. Failed pages never become image vectors.
    """
    config = load_config()
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4.")
    places = load_places(dataset_path)
    selected = list(places.values())
    if place_ids:
        missing = set(place_ids) - places.keys()
        if missing:
            raise ValueError(f"Unknown place IDs: {sorted(missing)}")
        selected = [place for place in selected if place["place_id"] in place_ids]
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer.")
        selected = selected[:limit]
    report = {"apply": apply, "places": len(places), "selected_places": len(selected),
              "collection": config["google_images_collection"], "photo_source": "google_maps_url",
              "photos_saved_locally": 0, "metadata_files_changed": 0,
              "photos_embedded": 0, "existing_vectors_reused": 0,
              "resolved_photos": [], "failed_places": []}
    if not apply:
        report["status"] = "preview_only_no_network_or_database_access"
        return report

    owns = {"resolver": resolver is None, "vectors": vector_store is None, "metadata": metadata_store is None}
    if resolver is None:
        if workers > 1:
            from src.data.preprocessing.parallel_maps_photos import ParallelMapsPhotoResolver
            resolver = ParallelMapsPhotoResolver(selected, workers)
        else:
            resolver = GoogleMapsPhotoResolver()
    client = httpx.Client(timeout=30, follow_redirects=False, trust_env=False)
    try:
        if owns["resolver"]:
            # Missing browser dependencies must fail before any database writes.
            resolver._context()
        vector_store = GoogleImageStore() if vector_store is None else vector_store
        metadata_store = PostgresGoogleMetadataStore() if metadata_store is None else metadata_store
        vector_store.ensure_collection(config["image_vector_size"])
        report["metadata_import"] = metadata_store.upsert_metadata(list(places.values()), [])
        for number, place in enumerate(selected, 1):
            if on_progress:
                on_progress({"title": f"Resolve Maps photo: {place['place_name']}",
                             "indexed": number - 1, "total": len(selected)})
            pixels = None
            try:
                photo = resolver.resolve(place)
                expected = {**photo.metadata, "image_url": photo.image_url}
                existing = vector_store.client.retrieve(vector_store.collection_name,
                    ids=[photo.point_id], with_payload=True, with_vectors=False)
                previous_photo = None
                if existing:
                    if len(existing) != 1 or existing[0].payload.get("place_id") != place["place_id"]:
                        raise RuntimeError("The stored image point belongs to a different place.")
                    try:
                        previous_photo = photo_identity(existing[0].payload.get("image_url"))
                    except ValueError:
                        pass
                if existing and previous_photo == photo_identity(photo.image_url):
                    if existing[0].payload != expected:
                        vector_store.client.overwrite_payload(vector_store.collection_name,
                            payload=expected, points=[photo.point_id], wait=True)
                    report["existing_vectors_reused"] += 1
                else:
                    # A changed cover photo replaces this place's stable point.
                    # Never reuse a vector computed from different pixels.
                    pixels = fetcher(photo.image_url, client=client)
                    if embedder is None:
                        from src.embedding.image_embedder import ImageEmbedder
                        embedder = ImageEmbedder()
                    vector_store.add_images([photo], embedder.embed_batch([pixels]))
                    report["photos_embedded"] += 1
                metadata_store.upsert_metadata([place], [{"image_id": photo.point_id,
                    "place_id": place["place_id"], "image_url": photo.image_url}])
                checked = vector_store.client.retrieve(vector_store.collection_name,
                    ids=[photo.point_id], with_payload=True, with_vectors=False)
                if len(checked) != 1 or checked[0].payload != expected:
                    raise RuntimeError("Qdrant photo metadata verification failed.")
                report["resolved_photos"].append({"place_id": place["place_id"],
                    "point_id": photo.point_id, "image_url": photo.image_url,
                    "google_maps_url": place["google_maps_url"]})
            except Exception as error:
                from src.utils.redaction import CredentialRedactor
                report["failed_places"].append({"place_id": place["place_id"],
                    "error": CredentialRedactor().text(str(error))})
            finally:
                if pixels is not None:
                    pixels.close()
            report["processed_places"] = number
            report["status"] = "running"
            if on_checkpoint:
                on_checkpoint(report)
        report["status"] = "complete" if not report["failed_places"] else "partial"
        report["verified_photos"] = len(report["resolved_photos"])
        return report
    finally:
        client.close()
        if owns["resolver"]:
            resolver.close()
        if owns["vectors"] and vector_store is not None:
            vector_store.close()
        if owns["metadata"] and metadata_store is not None:
            metadata_store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--apply", action="store_true", help="Resolve photos, embed pixels and write PostgreSQL/Qdrant.")
    parser.add_argument("--place-id", action="append", help="Index only this place's photo; repeat for more places.")
    parser.add_argument("--limit", type=int, help="Limit the number of Maps pages processed.")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent public Maps pages (1–4).")
    parser.add_argument("--report", default="datasets/processed/google/maps_photo_index_report.json")
    args = parser.parse_args()
    def progress(event):
        print(f"{event['title']} ({event['indexed']}/{event['total']})", flush=True)
    def checkpoint(report):
        from src.utils.json_checkpoint import write_checkpoint
        path = ensure_parent(resolve_path(args.report))
        write_checkpoint(path, report)
    report = index_maps_photos(args.dataset, apply=args.apply, place_ids=args.place_id,
                              limit=args.limit, on_progress=progress, workers=args.workers,
                              on_checkpoint=checkpoint if args.apply else None)
    if args.apply:
        ensure_parent(resolve_path(args.report)).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failed_places"] else 0


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
