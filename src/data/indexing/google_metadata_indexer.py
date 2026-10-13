"""Import Google place metadata without restoring image fields or local files.

Use google_maps_indexer to also resolve public cover photos and index pixels.
"""

import argparse
import json
from pathlib import Path

from src.data.preprocessing.google_maps_photos import load_places
from src.database.postgres_google_metadata_store import PostgresGoogleMetadataStore


def index_google_metadata(dataset_path=None, *, store=None, batch_size=None,
                          dry_run=False, on_progress=None):
    places = list(load_places(dataset_path).values())
    report = {"database": "PostgreSQL", "tables": ["google_places"], "locations": len(places),
              "photos": 0, "metadata_files_changed": 0, "dry_run": dry_run}
    if on_progress:
        on_progress({"step": "google_metadata", "title": "Google place metadata prepared",
                     "status": "done", "locations": len(places), "photos": 0})
    if dry_run:
        return report
    owns_store = store is None
    try:
        store = PostgresGoogleMetadataStore() if store is None else store
        report.update(store.upsert_metadata(places, [], batch_size=batch_size))
        return report
    finally:
        if owns_store and store is not None:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--apply", action="store_true", help="Import places (default: preview only).")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; retained for compatibility.")
    args = parser.parse_args()
    print(json.dumps(index_google_metadata(args.dataset, batch_size=args.batch_size,
        dry_run=not args.apply or args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
