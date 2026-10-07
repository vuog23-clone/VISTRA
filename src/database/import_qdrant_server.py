import argparse
from contextlib import closing
from pathlib import Path
import pickle
import sqlite3
from urllib.parse import urlsplit

from qdrant_client import QdrantClient, models

from src.utils.utils import load_config, resolve_path


def import_collection(
    client,
    source: Path,
    name: str,
    vector_size: int,
    batch_size: int
):
    
    database = (source / "collection" / name / "storage.sqlite").resolve()
    
    with closing(
        sqlite3.connect(
            database.as_uri() + "?mode=ro",
            uri=True
        )
    ) as connection:

        connection.execute("PRAGMA query_only = ON")
        total = connection.execute("SELECT COUNT(*) FROM points").fetchone()[0]

        client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(
                size=vector_size,
                distance=models.Distance.COSINE,
                on_disk=True
            ),
            on_disk_payload=True,
            hnsw_config=models.HnswConfigDiff(
                on_disk=True,
                max_indexing_threads=1
            ),
            optimizers_config=models.OptimizersConfigDiff(indexing_threshold=0),
        )

        cursor = connection.execute("SELECT point FROM points")
        transferred = 0

        while rows := cursor.fetchmany(batch_size):
            points = [pickle.loads(row[0]) for row in rows]
            client.upsert(
                collection_name=name,
                points=points,
                wait=True
            )
            transferred += len(points)

            print(f"{name}: {transferred}/{total}", flush=True)

        client.update_collection(
            collection_name=name,
            optimizers_config=models.OptimizersConfigDiff(
                indexing_threshold=20000
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:6333")
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 256:
        parser.error("Use a batch size between 1 and 256.")
    url = urlsplit(args.url)
    if url.scheme not in {"http", "https"} or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("This local migration requires a localhost Qdrant server.")
    config = load_config()
    client = QdrantClient(url=args.url, timeout=120)
    try:
        for path, name, size in (
            ("qdrant_path", "wikivoyage_collection", "text_vector_size"),
            ("qdrant_images_path", "scraped_images_collection", "image_vector_size"),
        ):
            import_collection(client, resolve_path(config[path]), config[name], config[size], args.batch_size)
    finally:
        client.close()


if __name__ == "__main__":
    main()
