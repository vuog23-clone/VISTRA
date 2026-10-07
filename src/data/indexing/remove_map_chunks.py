from pathlib import Path
import json
from qdrant_client import QdrantClient, models

from src.utils.utils import load_config, resolve_path, stable_uuid


def remove_retired_chunks(
    ids_path=None, qdrant_path=None, collection_name=None,
) -> int:
    config = load_config()
    ids_path = resolve_path(ids_path, config["removed_chunk_ids_path"])
    collection_name = collection_name if collection_name is not None else config["wikivoyage_collection"]
    chunk_ids = json.loads(ids_path.read_text(encoding="utf-8"))
    if not isinstance(chunk_ids, list) or not all(isinstance(value, str) for value in chunk_ids):
        raise ValueError("Expected a JSON list of chunk IDs.")
    storage = resolve_path(qdrant_path, config["qdrant_path"])
    if not storage.exists():
        print("No existing Qdrant database; nothing to remove.")
        return 0
    client = QdrantClient(path=str(storage))
    try:
        if not client.collection_exists(collection_name):
            print(f"No {collection_name} collection; nothing to remove.")
            return 0
        point_ids = [stable_uuid(f"wikivoyage:{chunk_id}") for chunk_id in chunk_ids]
        if point_ids:
            client.delete(
                collection_name=collection_name,
                points_selector=models.PointIdsList(points=point_ids),
                wait=True,
            )
        print(f"Removed any existing points for {len(point_ids)} retired chunk IDs.")
        return len(point_ids)
    finally:
        client.close()