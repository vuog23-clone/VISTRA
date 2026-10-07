from pathlib import Path
import json
from PIL import Image

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.utils.utils import get_sha256, load_config, read_json, read_jsonl, resolve_path, write_jsonl


def load_sidecars(photo_dir: Path):
    records = []

    for json_path in sorted(photo_dir.rglob("*.json")):
        data = read_json(json_path)
        if not isinstance(data, dict):
            raise ValueError(f"Expected photo metadata object: {json_path}")
        records.append((json_path, data))

    return records


def find_image(json_path: Path, metadata: dict | None = None):
    """Match both photo.jpg.json and photo.json without assuming image format."""
    image_path = json_path.with_suffix("")
    if image_path.is_file():
        return image_path
    candidates = [
        path for path in json_path.parent.iterdir()
        if path.is_file() and path.suffix.lower() != ".json" and path.stem == json_path.stem
    ]
    if len(candidates) == 1:
        return candidates[0]
    filename = (metadata or {}).get("file")
    if filename:
        named = json_path.parent / Path(filename).name
        if named.is_file() and named.suffix.lower() != ".json":
            if not candidates or named in candidates:
                return named
    if candidates:
        raise ValueError(f"Ambiguous image files for sidecar: {json_path}")
    return None


def build_manifest(
    photo_dir: Path,
    output_path: Path,
    places_path: Path | None = None,
):

    photo_dir = resolve_path(photo_dir)
    output_path = resolve_path(output_path)
    if not photo_dir.is_dir():
        raise FileNotFoundError(f"Photo directory not found: {photo_dir}")
    images = {}
    valid_place_ids = None
    if places_path is not None:
        places = list(read_jsonl(places_path))
        valid_place_ids = {place["osm_id"] for place in places}
        if len(valid_place_ids) != len(places):
            raise ValueError("Duplicate place IDs in source metadata.")
    excluded = []
    sidecars = load_sidecars(photo_dir)
    mismatched_hashes = 0

    for index, (sidecar, meta) in enumerate(sidecars, start=1):
        image_path = find_image(sidecar, meta)

        if image_path is None:
            raise FileNotFoundError(f"Missing image for sidecar: {sidecar}")

        image_hash = get_sha256(image_path)
        mismatched_hashes += meta.get("sha256") not in (None, image_hash)
        place_id = meta.get("osm_id") or meta.get("place_id")
        if not place_id:
            raise ValueError(f"Missing place ID in sidecar: {sidecar}")
        if valid_place_ids is not None and place_id not in valid_place_ids:
            excluded.append({"place_id": place_id, "image_id": image_hash,
                             "path": str(image_path), "sidecar_path": str(sidecar),
                             "reason": "place_id_missing_from_current_places", "metadata": meta})
            continue
        # Actual downloaded file identity is authoritative; raw metadata is kept
        # separately in provenance so corrections never discard source details.
        normalized_meta = {**meta, "file": image_path.name,
                           "sha256": image_hash, "bytes": image_path.stat().st_size}
        with Image.open(image_path) as image:
            width, height = image.size
            image.load()

        if image_hash not in images:
            images[image_hash] = {
                "image_id": image_hash,
                "path": str(image_path),
                "place_ids": [],
                "metadata": normalized_meta,
                "width": width,
                "height": height,
                "provenance": [],
            }

        if place_id not in images[image_hash]["place_ids"]:
            images[image_hash]["place_ids"].append(place_id)
        images[image_hash]["provenance"].append({
            "place_id": place_id, "path": str(image_path),
            "sidecar_path": str(sidecar), "metadata": meta,
        })
        if index % 512 == 0:
            print(f"Preprocessed {index:,}/{len(sidecars):,} photos", flush=True)

    if not images:
        raise RuntimeError("No image/metadata pairs found; manifest was not replaced.")
    write_jsonl(output_path, images.values())
    write_jsonl(output_path.with_suffix(".excluded.jsonl"), excluded)

    print(f"Unique images: {len(images)}")
    print(f"Saved to: {output_path}")
    result = {"sidecars": len(sidecars), "unique_images": len(images),
              "associations": sum(len(record["place_ids"]) for record in images.values()),
              "corrected_hashes": mismatched_hashes,
              "excluded_associations": len(excluded),
              "excluded_place_ids": sorted({record["place_id"] for record in excluded}),
              "photo_directory": str(photo_dir), "manifest_sha256": get_sha256(output_path)}
    output_path.with_suffix(".summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Preprocessing summary: {result}", flush=True)
    return result


if __name__ == "__main__":
    config = load_config()
    build_manifest(
        photo_dir=resolve_path(config["scraped_photos_directory"]),
        output_path=resolve_path(config["image_manifest_path"]),
        places_path=resolve_path(config["scraped_places_path"]),
    )
