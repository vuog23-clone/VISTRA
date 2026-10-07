from pathlib import Path
from typing import Any

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from src.utils.utils import load_config, resolve_path, stable_uuid
from src.database.qdrant_connection import open_qdrant


class QdrantImageStore:
    def __init__(
        self,
        path: str | Path | None = None,
        collection_name: str | None = None,
        vector_size: int | None = None,
    ):
        self.config = load_config()
        self.path = resolve_path(path or self.config["qdrant_images_path"])

        self.vector_size = vector_size if vector_size is not None else self.config["image_vector_size"]
        self.collection_name = collection_name or self.config["scraped_images_collection"]

        self.client = open_qdrant(
            self.path,
            self.config,
            QdrantClient
        )

    def ensure_collection(
        self,
        vector_size: int
    ) -> None:
        if vector_size != self.vector_size:
            raise ValueError(f"Image embeddings have {vector_size} dimensions; expected {self.vector_size}.")
        
        if self.client.collection_exists(self.collection_name):
            vectors = self.client.get_collection(self.collection_name).config.params.vectors

            if not isinstance(vectors, VectorParams) or vectors.size != vector_size or vectors.distance != Distance.COSINE:
                raise ValueError(
                    f"Collection '{self.collection_name}' must use an unnamed "
                    f"{vector_size}-dimensional COSINE vector."
                )
            return
        
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=VectorParams(
                size=vector_size,
                distance=Distance.COSINE,
            ),
        )


    @staticmethod
    def make_point_id(
        image_id: str,
        place_id: str
    ) -> str:

        return stable_uuid(f"{image_id}:{place_id}")


    @staticmethod
    def build_payload(
        record: dict[str, Any],
        place_id: str,
    ) -> dict[str, Any]:

        payload = {
            "image_id": record["image_id"],
            "place_id": place_id,
            "image_path": record["path"],
        }

        quality_status = record.get("quality_status")

        if quality_status is not None:
            payload["quality_status"] = quality_status

        return payload


    def create_points(
        self,
        records: list[dict[str, Any]],
        embeddings: np.ndarray
    ) -> list[PointStruct]:

        if len(records) != len(embeddings):
            raise ValueError("Image records and embeddings must have the same length.")
        points = []

        for record, embedding in zip(records, embeddings):
            image_id = record.get("image_id")
            place_ids = record.get("place_ids", [])

            for place_id in place_ids:
                point = PointStruct(
                    id=self.make_point_id(
                        image_id=image_id,
                        place_id=place_id
                    ),
                    vector=embedding.tolist(),
                    payload=self.build_payload(
                        record=record,
                        place_id=place_id
                    )
                )

                points.append(point)

        return points


    def add_images(self, records: list[dict[str, Any]], embeddings: np.ndarray) -> int:
        points = self.create_points(records, embeddings)
        if not points:
            return 0
        self.client.upsert(
            collection_name=self.collection_name,
            points=points,
            wait=True,
        )
        return len(points)

    def count(self) -> int | None:
        if not self.client.collection_exists(self.collection_name):
            return None
        return self.client.get_collection(self.collection_name).points_count

    def close(self) -> None:
        self.client.close()