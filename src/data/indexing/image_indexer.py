from pathlib import Path
from typing import Any
import numpy as np

from src.database.qdrant_image_store import QdrantImageStore
from src.embedding.image_embedder import ImageEmbedder
from src.utils.utils import batched, load_config, read_jsonl, resolve_path


class ImageIndexer:
    def __init__(
        self,
        qdrant_path: str | Path | None = None,
        collection_name: str | None = None,
        embedder: ImageEmbedder | None = None,
        store: QdrantImageStore | None = None,
    ):
        self.config = load_config()

        self.embedder = embedder if embedder is not None else ImageEmbedder()

        self.store = store if store is not None else QdrantImageStore(
            path=qdrant_path,
            collection_name=collection_name,
        )
        
        self.qdrant_path = self.store.path
        self.collection_name = self.store.collection_name


    def load_manifest(
        self,
        manifest_path: str | Path
    ) -> list[dict[str, Any]]:

        manifest_path = resolve_path(manifest_path)
        records = list(read_jsonl(manifest_path))
        print(
            f"[INFO] Loaded {len(records)} "
            f"unique image records"
        )

        return records


    def embed_batch(
        self,
        records: list[dict[str, Any]]
    ) -> np.ndarray:

        embeddings = np.asarray(
            self.embedder.embed_batch([record["path"] for record in records]),
            dtype=np.float32,
        )

        if embeddings.ndim == 1:
            embeddings = embeddings.reshape(1, -1)

        return embeddings


    @staticmethod
    def validate_record(
        record: dict[str, Any]
    ) -> bool:

        required_fields = [
            "image_id",
            "path",
            "place_ids",
        ]

        for field in required_fields:
            if field not in record:
                print(
                    f"[WARNING] Missing '{field}': "
                    f"{record}"
                )
                return False

        path = resolve_path(record["path"])

        if not path.is_file():
            print(
                f"[WARNING] Image does not exist: "
                f"{path}"
            )
            return False

        record["path"] = str(path)

        if not record["place_ids"]:
            print(
                f"[WARNING] No place_ids for: "
                f"{record['image_id']}"
            )
            return False

        return True


    def index(
        self,
        manifest_path: str | Path | None = None,
        batch_size: int | None = None,
    ) -> None:

        manifest_path = resolve_path(manifest_path, self.config["image_manifest_path"])
        batch_size = batch_size if batch_size is not None else self.config["image_index_batch_size"]

        records = self.load_manifest(manifest_path)
        records = [record for record in records if self.validate_record(record)]

        print(
            f"[INFO] Valid images: "
            f"{len(records)}"
        )

        total_images = len(records)

        indexed_images = 0
        indexed_points = 0

        collection_ready = False

        for batch_index, batch in enumerate(batched(records, batch_size)):
            start = batch_index * batch_size
            end = start + len(batch)

            try:
                embeddings = self.embed_batch(batch)

            except Exception as e:
                print(
                    f"[ERROR] Failed embedding batch "
                    f"{start}:{end}: {e}"
                )
                continue

            if not collection_ready:
                vector_size = int(embeddings.shape[1])

                self.store.ensure_collection(vector_size)

                collection_ready = True

            try:
                point_count = self.store.add_images(batch, embeddings)

            except Exception as e:
                print(
                    f"[ERROR] Qdrant upsert failed "
                    f"for batch {start}:{end}: {e}"
                )
                continue

            if not point_count:
                continue

            indexed_images += len(batch)
            indexed_points += point_count

            print(
                f"[INFO] "
                f"{end}/{total_images} images | "
                f"{indexed_points} Qdrant points"
            )

        print()
        print("=" * 60)
        print("IMAGE INDEXING COMPLETE" if indexed_images == total_images else "IMAGE INDEXING INCOMPLETE")
        print("=" * 60)

        print(
            f"Images processed : "
            f"{indexed_images}"
        )

        print(
            f"Points inserted  : "
            f"{indexed_points}"
        )

        print(
            f"Collection       : "
            f"{self.collection_name}"
        )

        print(
            f"Qdrant path      : "
            f"{self.qdrant_path}"
        )

        self.print_collection_info()

        if indexed_images != total_images:
            raise RuntimeError(
                f"Image indexing incomplete: {indexed_images}/{total_images} images indexed."
            )


    def close(self) -> None:
        self.store.close()


    def print_collection_info(
        self,
    ) -> None:

        point_count = self.store.count()
        if point_count is not None:
            print(f"Qdrant points    : {point_count}")



if __name__ == "__main__":

    indexer = ImageIndexer()

    try:
        indexer.index()
    finally:
        indexer.close()
