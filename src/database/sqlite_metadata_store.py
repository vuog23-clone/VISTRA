import json
import sqlite3
from pathlib import Path

from src.utils.utils import batched, ensure_parent, load_config, resolve_path


class SQLiteStore:
    def __init__(
        self,
        path: str | Path | None = None,
        collection_name: str = "scraped_data",
        batch_size: int | None = None,
        read_only: bool = False,
    ):
        config = load_config()
        self.batch_size = batch_size if batch_size is not None else config["metadata_index_batch_size"]
        self.collection_name = collection_name
        self.path = resolve_path(path, config["sqlite_path"])

        if read_only:
            self.connection = sqlite3.connect(
                self.path.as_uri() + "?mode=ro", uri=True, timeout=30,
            )
        else:
            ensure_parent(self.path)
            self.connection = sqlite3.connect(self.path, timeout=30)

        self.connection.execute("PRAGMA foreign_keys = ON")

    def create_indexes(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS places (
                place_id TEXT PRIMARY KEY,
                name TEXT,
                province TEXT,
                category TEXT,
                metadata_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS images (
                image_id TEXT PRIMARY KEY,
                image_path TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS image_places (
                image_id TEXT NOT NULL REFERENCES images(image_id),
                place_id TEXT NOT NULL REFERENCES places(place_id),
                PRIMARY KEY (image_id, place_id)
            );

            CREATE INDEX IF NOT EXISTS idx_places_province
                ON places(province);
            CREATE INDEX IF NOT EXISTS idx_places_category
                ON places(category);
            CREATE INDEX IF NOT EXISTS idx_image_places_place
                ON image_places(place_id);
            """
        )


    def _execute_batches(self, statement: str, rows: list[tuple]) -> None:
        for batch in batched(rows, self.batch_size):
            self.connection.executemany(statement, batch)

    def upsert_metadata(self, places: list[dict], images: list[dict], *, replace_existing: bool = False) -> None:
        place_rows = [
            (
                place["osm_id"],
                place.get("name"),
                place.get("province"),
                place.get("category"),
                json.dumps(place, ensure_ascii=False, allow_nan=False),
            )
            for place in places
        ]
        image_rows = [
            (
                image["image_id"],
                image["path"],
                json.dumps(image, ensure_ascii=False, allow_nan=False),
            )
            for image in images
        ]
        association_rows = [
            (image["image_id"], place_id)
            for image in images
            for place_id in sorted(set(image["place_ids"]))
        ]

        with self.connection:
            if replace_existing:
                self.connection.execute("DELETE FROM image_places")
                self.connection.execute("DELETE FROM images")
                self.connection.execute("DELETE FROM places")
            self._execute_batches(
                """
                INSERT INTO places
                    (place_id, name, province, category, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(place_id) DO UPDATE SET
                    name = excluded.name,
                    province = excluded.province,
                    category = excluded.category,
                    metadata_json = excluded.metadata_json
                """,
                place_rows,
            )
            self._execute_batches(
                """
                INSERT INTO images (image_id, image_path, metadata_json)
                VALUES (?, ?, ?)
                ON CONFLICT(image_id) DO UPDATE SET
                    image_path = excluded.image_path,
                    metadata_json = excluded.metadata_json
                """,
                image_rows,
            )

            self._execute_batches(
                "DELETE FROM image_places WHERE image_id = ?",
                [(image["image_id"],) for image in images],
            )
            self._execute_batches(
                "INSERT INTO image_places (image_id, place_id) VALUES (?, ?)",
                association_rows,
            )


    def get_place(self, place_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT metadata_json FROM places WHERE place_id = ?",
            (place_id,),
        ).fetchone()
        return json.loads(row[0]) if row else None


    def list_places(self) -> list[dict]:
        rows = self.connection.execute("SELECT metadata_json FROM places").fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_image(self, image_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT metadata_json FROM images WHERE image_id = ?",
            (image_id,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def get_images_for_place(self, place_id: str) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT images.metadata_json FROM images
            JOIN image_places ON image_places.image_id = images.image_id
            WHERE image_places.place_id = ?
            ORDER BY images.image_id
            """,
            (place_id,),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def close(self) -> None:
        self.connection.close()
