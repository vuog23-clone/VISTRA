"""One image vector per point, with place metadata and no local photo URL list."""

from pathlib import Path

import numpy as np
from qdrant_client import QdrantClient, models

from src.database.qdrant_connection import open_qdrant
from src.data.google_payload import place_payload
from src.data.preprocessing.google_maps_photos import checked_photo_url
from src.utils.utils import load_config, resolve_path


class GoogleImageStore:
    def __init__(self, path: str | Path | None = None, collection_name=None, vector_size=None):
        self.config = load_config()
        self.path = resolve_path(path, self.config["qdrant_google_images_path"])
        self.collection_name = collection_name or self.config["google_images_collection"]
        self.vector_size = self.config["image_vector_size"] if vector_size is None else vector_size
        self.client = open_qdrant(self.path, self.config, QdrantClient)

    def ensure_collection(self, vector_size):
        if vector_size != self.vector_size:
            raise ValueError(f"Image embeddings have {vector_size} dimensions; expected {self.vector_size}.")
        if self.client.collection_exists(self.collection_name):
            vectors = self.client.get_collection(self.collection_name).config.params.vectors
            if (not isinstance(vectors, models.VectorParams) or vectors.size != vector_size
                    or vectors.distance != models.Distance.COSINE):
                raise ValueError(f"Collection '{self.collection_name}' must use an unnamed "
                    f"{vector_size}-dimensional COSINE vector.")
        else:
            self.client.create_collection(self.collection_name,
                vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE))
        info = self.client.get_collection(self.collection_name)
        if "place_id" not in info.payload_schema:
            self.client.create_payload_index(self.collection_name, "place_id",
                field_schema=models.PayloadSchemaType.KEYWORD, wait=True)

    def create_points(self, photos, embeddings):
        vectors = np.asarray(embeddings, dtype=np.float32)
        if vectors.shape != (len(photos), self.vector_size) or not np.isfinite(vectors).all():
            raise ValueError("Google image embeddings must be finite and match the collection dimensions.")
        if np.any(np.linalg.norm(vectors, axis=1) == 0):
            raise ValueError("Google image embeddings must be nonzero.")
        points = []
        for photo, vector in zip(photos, vectors, strict=True):
            payload = place_payload(photo.metadata)
            payload["image_url"] = checked_photo_url(photo.image_url)
            points.append(models.PointStruct(id=photo.point_id, vector=vector.tolist(), payload=payload))
        return points

    def add_images(self, photos, embeddings):
        points = self.create_points(photos, embeddings)
        if not points:
            return 0
        self.client.upsert(self.collection_name, points=points, wait=True)
        return len(points)

    def count(self):
        if not self.client.collection_exists(self.collection_name):
            return None
        return self.client.get_collection(self.collection_name).points_count

    def close(self):
        self.client.close()
