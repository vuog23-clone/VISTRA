"""Place metadata for vector payloads and Maps-only retrieval output."""

from copy import deepcopy


def place_payload(metadata):
    """Keep source place fields, excluding the list of local photo URLs."""
    return deepcopy({key: value for key, value in metadata.items() if key not in {"image", "images"}})


def without_photo_fields(value):
    """Serialize Maps-only results without photo attachments or local URLs."""
    if isinstance(value, dict):
        return {key: without_photo_fields(item) for key, item in value.items()
                if key not in {"image", "images", "image_url", "image_path"}}
    if isinstance(value, list):
        return [without_photo_fields(item) for item in value]
    return value
