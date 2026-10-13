"""PostgreSQL storage for Google locations and resolved remote photo URLs.

Connection credentials live in .env, never config.yaml. Set POSTGRES_DSN for an
existing server, or POSTGRES_HOST/PORT/DB/USER/PASSWORD for the local Docker DB.
This store does not read OSM data or modify Qdrant vectors and payloads.
"""

from math import isfinite
import json
import os
import re
from uuid import UUID

from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.utils.utils import PROJECT_ROOT, batched, load_config
from src.data.preprocessing.google_maps_photos import checked_photo_url


_SCHEMA_SQL = """
-- Location details occur once, regardless of how many photos a place has.
CREATE TABLE IF NOT EXISTS google_places (
    place_id TEXT PRIMARY KEY,
    place_name TEXT NOT NULL CHECK (length(trim(place_name)) > 0),
    google_maps_url TEXT,
    category TEXT,
    address TEXT,
    province TEXT,
    rating DOUBLE PRECISION CHECK (rating BETWEEN 0 AND 5),
    review_count BIGINT CHECK (review_count >= 0),
    opening_hours JSONB,
    price_range JSONB,
    latitude DOUBLE PRECISION CHECK (latitude BETWEEN -90 AND 90),
    longitude DOUBLE PRECISION CHECK (longitude BETWEEN -180 AND 180)
);

-- The image UUID matches the stable place-cover point in google_maps_images.
-- Only resolved public photo URLs are stored; never local paths or pixels.
CREATE TABLE IF NOT EXISTS google_images (
    image_id UUID PRIMARY KEY,
    place_id TEXT NOT NULL REFERENCES google_places(place_id),
    image_url TEXT NOT NULL UNIQUE
);

-- Reject accidental reintroduction of legacy local-image URLs.
ALTER TABLE google_images DROP CONSTRAINT IF EXISTS google_images_image_url_check;
ALTER TABLE google_images ADD CONSTRAINT google_images_image_url_check CHECK (
    image_url ~ '^https://lh[0-9]+[.](googleusercontent[.]com|ggpht[.]com)/(p|gps-cs-s|gps-proxy|grass-cs|grass-proxy)/'
    OR image_url LIKE 'https://streetviewpixels-pa.googleapis.com/v1/thumbnail?%'
);

CREATE INDEX IF NOT EXISTS google_places_province_idx ON google_places(province);
CREATE INDEX IF NOT EXISTS google_places_category_idx ON google_places(category);
CREATE INDEX IF NOT EXISTS google_images_place_id_idx ON google_images(place_id);

COMMENT ON TABLE google_places IS 'Google location metadata, including locations without photos.';
COMMENT ON TABLE google_images IS 'Resolved photo URLs, one row per Qdrant image point; no photo bytes.';
COMMENT ON COLUMN google_images.image_id IS 'Point UUID in the google_maps_images Qdrant collection.';
COMMENT ON COLUMN google_places.opening_hours IS 'Original opening_hours object; null days mean unknown.';
COMMENT ON COLUMN google_places.price_range IS 'Original price_range object, including currency and null bounds.';
"""


PLACE_COLUMNS = (
    "place_id", "place_name", "google_maps_url", "category", "address", "province",
    "rating", "review_count", "opening_hours", "price_range", "latitude", "longitude",
)
SOURCE_FIELDS = set(PLACE_COLUMNS[:-2]) | {"coordinates"}
IMAGE_COLUMNS = ("image_id", "place_id", "image_url")


def _number(value, name, minimum, maximum):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int))
            or not isfinite(value) or not minimum <= value <= maximum):
        raise ValueError(f"{name} must be null or a finite number between {minimum} and {maximum}.")
    return value


def prepare_rows(places, images):
    """Validate all rows before connecting or making any database/file changes."""
    place_rows, image_rows, place_ids, image_ids, urls = [], [], set(), set(), set()
    for place in places:
        unknown = set(place) - SOURCE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported Google metadata fields: {sorted(unknown)}. Extend the schema before importing them.")
        place_id = place.get("place_id")
        if not isinstance(place_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", place_id):
            raise ValueError("Google place_id must be a nonempty filename-safe string.")
        if place_id in place_ids:
            raise ValueError(f"Duplicate Google place_id: {place_id}")
        place_ids.add(place_id)
        name = place.get("place_name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Missing Google place_name: {place_id}")
        for field in ("google_maps_url", "category", "address", "province"):
            if place.get(field) is not None and not isinstance(place[field], str):
                raise ValueError(f"{field} must be null or a string: {place_id}")
        rating = _number(place.get("rating"), "rating", 0, 5)
        count = place.get("review_count")
        if count is not None and (type(count) is not int or not 0 <= count <= 2**63 - 1):
            raise ValueError(f"review_count must be null or a nonnegative bigint: {place_id}")
        coordinates = place.get("coordinates")
        if coordinates is not None and (not isinstance(coordinates, dict)
                or set(coordinates) - {"latitude", "longitude"}):
            raise ValueError(f"coordinates must contain only latitude/longitude: {place_id}")
        coordinates = coordinates or {}
        latitude = _number(coordinates.get("latitude"), "latitude", -90, 90)
        longitude = _number(coordinates.get("longitude"), "longitude", -180, 180)
        for field in ("opening_hours", "price_range"):
            if place.get(field) is not None and not isinstance(place[field], dict):
                raise ValueError(f"{field} must be null or an object: {place_id}")
        json.dumps(place, allow_nan=False)
        place_rows.append(dict(zip(PLACE_COLUMNS, (
            place_id, name, place.get("google_maps_url"), place.get("category"),
            place.get("address"), place.get("province"), rating, count,
            place.get("opening_hours"), place.get("price_range"), latitude, longitude,
        ), strict=True)))
    for image in images:
        if set(image) != set(IMAGE_COLUMNS):
            raise ValueError("Each image must contain only image_id, place_id, and image_url.")
        image_id = str(UUID(str(image["image_id"])))
        place_id, url = image["place_id"], image["image_url"]
        if place_id not in place_ids:
            raise ValueError(f"Photo refers to a place outside this import: {place_id}")
        if not isinstance(url, str):
            raise ValueError("image_url must be a photo URL.")
        checked_photo_url(url)
        if image_id in image_ids or url in urls:
            raise ValueError(f"Duplicate photo UUID or URL: {image_id}")
        image_ids.add(image_id)
        urls.add(url)
        image_rows.append(dict(image_id=image_id, place_id=place_id, image_url=url))
    return place_rows, image_rows


class PostgresGoogleMetadataStore:
    def __init__(self, dsn=None, *, connection=None):
        self._owns_connection = connection is None
        self.connection = connection
        self.batch_size = load_config()["metadata_index_batch_size"]
        if self.connection is None:
            load_dotenv(PROJECT_ROOT / ".env")
            dsn = dsn or os.getenv("POSTGRES_DSN")
            if dsn:
                self.connection = psycopg.connect(dsn, autocommit=True,
                    row_factory=dict_row, connect_timeout=10)
            else:
                password = os.getenv("POSTGRES_PASSWORD")
                if not password:
                    raise ValueError("Set POSTGRES_DSN or POSTGRES_PASSWORD in .env before importing Google metadata.")
                self.connection = psycopg.connect(
                    host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
                    port=os.getenv("POSTGRES_PORT", "5432"),
                    dbname=os.getenv("POSTGRES_DB", "vistra"),
                    user=os.getenv("POSTGRES_USER", "vistra"), password=password,
                    autocommit=True, row_factory=dict_row, connect_timeout=10,
                )

    def ensure_schema(self):
        """Create the Google tables, constraints, and indexes defined in this module."""
        with self.connection.transaction():
            self.connection.execute(_SCHEMA_SQL, prepare=False)

    def _upsert_rows(self, cursor, table, columns, key, rows, batch_size):
        # Table/column names are fixed constants from this module, never user input.
        other = [column for column in columns if column != key]
        statement = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
            f"ON CONFLICT ({key}) DO UPDATE SET " +
            ", ".join(f"{column} = EXCLUDED.{column}" for column in other) +
            f" WHERE ({', '.join(f'{table}.{column}' for column in other)}) IS DISTINCT FROM "
            f"({', '.join(f'EXCLUDED.{column}' for column in other)})"
        )
        changed = 0
        for batch in batched(rows, batch_size):
            values = [tuple(Jsonb(row[column]) if column in {"opening_hours", "price_range"}
                and row[column] is not None else row[column] for column in columns) for row in batch]
            cursor.executemany(statement, values)
            changed += cursor.rowcount
        return changed

    def upsert_metadata(self, places, images, *, batch_size=None):
        """Atomically add/update rows; unchanged rows and unrelated rows are retained."""
        places, images = list(places), list(images)
        place_rows, image_rows = prepare_rows(places, images)
        batch_size = self.batch_size if batch_size is None else batch_size
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer.")
        with self.connection.transaction():
            self.ensure_schema()
            with self.connection.cursor(row_factory=dict_row) as cursor:
                # Serialize imports, so verification covers one consistent write.
                cursor.execute("LOCK TABLE google_places, google_images IN SHARE ROW EXCLUSIVE MODE")
                places_written = self._upsert_rows(cursor, "google_places", PLACE_COLUMNS,
                    "place_id", place_rows, batch_size)
                images_written = self._upsert_rows(cursor, "google_images", IMAGE_COLUMNS,
                    "image_id", image_rows, batch_size)
                for rows, table, key in ((place_rows, "google_places", "place_id"),
                        (image_rows, "google_images", "image_id")):
                    for batch in batched(rows, batch_size):
                        cast = "::uuid[]" if key == "image_id" else "::text[]"
                        cursor.execute(f"SELECT * FROM {table} WHERE {key} = ANY(%s{cast})",
                            ([row[key] for row in batch],))
                        actual = cursor.fetchall()
                        if key == "image_id":
                            for row in actual:
                                row[key] = str(row[key])
                        if {row[key]: row for row in actual} != {row[key]: row for row in batch}:
                            raise RuntimeError(f"PostgreSQL metadata verification failed for {table}.")
                cursor.execute("SELECT (SELECT count(*) FROM google_places) AS places, "
                    "(SELECT count(*) FROM google_images) AS images")
                totals = cursor.fetchone()
        return {"places_written": places_written, "images_written": images_written,
            "database_places": totals["places"], "database_images": totals["images"], "verified": True}

    def get_place(self, place_id):
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT * FROM google_places WHERE place_id = %s", (place_id,))
            return cursor.fetchone()

    def list_places(self):
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT * FROM google_places ORDER BY place_id")
            return cursor.fetchall()

    def get_retrieval_records(self, place_ids, image_ids):
        """Read place/photo metadata in two queries on the owning connection."""
        place_ids = sorted(set(place_ids))
        image_ids = sorted({str(UUID(str(value))) for value in image_ids if value is not None})
        with self.connection.transaction():
            with self.connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute("SELECT * FROM google_places WHERE place_id = ANY(%s::text[])", (place_ids,))
                places = {row["place_id"]: row for row in cursor.fetchall()}
                cursor.execute("SELECT * FROM google_images WHERE image_id = ANY(%s::uuid[])", (image_ids,))
                images = {}
                for row in cursor.fetchall():
                    row["image_id"] = str(row["image_id"])
                    images[row["image_id"]] = row
        return places, images

    def get_image(self, image_id):
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT * FROM google_images WHERE image_id = %s", (str(UUID(str(image_id))),))
            row = cursor.fetchone()
            if row:
                row["image_id"] = str(row["image_id"])
            return row

    def get_images_for_place(self, place_id):
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT * FROM google_images WHERE place_id = %s ORDER BY image_url", (place_id,))
            rows = cursor.fetchall()
            for row in rows:
                row["image_id"] = str(row["image_id"])
            return rows

    def places_without_images(self):
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT p.place_id, p.place_name, p.province FROM google_places p "
                "WHERE NOT EXISTS (SELECT 1 FROM google_images i WHERE i.place_id = p.place_id) "
                "ORDER BY p.place_id")
            return cursor.fetchall()

    def close(self):
        if self._owns_connection and self.connection is not None:
            self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
