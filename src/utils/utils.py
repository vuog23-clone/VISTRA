import yaml
import json
import os
from math import isfinite
import re
import uuid
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO, TypeVar


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
T = TypeVar("T")


def resolve_path(
    path: str | Path | None,
    default: str | Path | None = None,
) -> Path:
    """Resolve a configured path, or its default, against the project root."""
    value = path if path is not None else default
    if value is None:
        raise ValueError("A path or default path is required.")
    result = Path(value).expanduser()
    return (result if result.is_absolute() else PROJECT_ROOT / result).resolve()


def ensure_parent(path: str | Path) -> Path:
    """Create an output file's parent directories and return its Path."""
    result = Path(path)
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


def read_json(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as file:
        return json.load(file)


def read_jsonl(
    path: str | Path,
    *,
    skip_invalid: bool = False,
) -> Iterator[dict]:
    """Stream nonblank records; optionally warn and skip malformed JSON."""
    with Path(path).open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("Expected a JSON object.")
                yield record
            except ValueError as error:
                message = f"Invalid JSON: {path}, line {line_number}: {error}"
                if not skip_invalid:
                    raise ValueError(message) from error
                print(f"[WARNING] {message}")


def write_jsonl_record(file: TextIO, record: dict) -> None:
    file.write(json.dumps(record, ensure_ascii=False) + "\n")


def count_jsonl(path: str | Path) -> int:
    with Path(path).open(encoding="utf-8") as file:
        return sum(bool(line.strip()) for line in file)


def batched(items: Iterable[T], batch_size: int) -> Iterator[list[T]]:
    """Yield bounded batches from a sequence or a streaming iterator."""
    if batch_size < 1:
        raise ValueError("batch_size must be greater than zero.")
    iterator = iter(items)
    while batch := list(islice(iterator, batch_size)):
        yield batch


def stable_uuid(value: str) -> str:
    """Use the existing URL namespace to preserve indexed point IDs."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def load_rgb_image(image):
    """Safely decode a bounded workspace raster and strip its metadata."""
    from src.security.images import load_safe_image
    return load_safe_image(image)


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", text.lower().strip())
    return text.strip("_") or "general"

def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError(f"Expected a YAML mapping in {CONFIG_PATH}.")
    # Docker uses the service DNS name; local Python retains config.yaml defaults.
    if os.getenv("QDRANT_URL"):
        config["qdrant_url"] = os.environ["QDRANT_URL"]
    url = config.get("qdrant_url")
    if url is not None and (not isinstance(url, str) or not url.startswith(("http://", "https://"))):
        raise ValueError("Config qdrant_url must be null or an HTTP(S) server URL.")
    for key in (
        "text_vector_size", "image_vector_size", "text_embedding_batch_size",
        "wikivoyage_index_batch_size", "metadata_index_batch_size",
        "chunk_size", "chunk_min_tokens", "search_limit", "retrieval_limit",
        "retrieval_group_size", "rrf_k", "candidate_min_limit", "candidate_multiplier",
        "query_embedding_cache_size",
    ):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f"Config {key} must be a positive integer.")
    if type(config["chunk_overlap"]) is not int or not 0 <= config["chunk_overlap"] < config["chunk_size"] or config["chunk_min_tokens"] > config["chunk_size"]:
        raise ValueError("Config requires 0 <= chunk_overlap < chunk_size and chunk_min_tokens <= chunk_size.")
    for key in ("qdrant_timeout_seconds", "jev_timeout_seconds"):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
            raise ValueError(f"Config {key} must be a finite positive number.")
        maximum = 30 if key == "jev_timeout_seconds" else 120
        if value > maximum:
            raise ValueError(f"Config {key} must not exceed {maximum} seconds.")
    for key in ("models_local_files_only", "image_normalize"):
        if type(config[key]) is not bool:
            raise ValueError(f"Config {key} must be a boolean.")
    for key in ("jev_routing_enabled", "jev_response_routing_enabled", "weather_enabled", "planning_enabled", "ors_enabled"):
        if key in config and type(config[key]) is not bool:
            raise ValueError(f"Config {key} must be a boolean.")
    timeout = config.get('weather_timeout_seconds', 8.0)
    if type(timeout) not in (int, float) or not isfinite(timeout) or not 0 < timeout <= 15:
        raise ValueError('Weather timeout must be between 0 and 15 seconds.')
    timeout = config.get('ors_timeout_seconds', 8.0)
    if type(timeout) not in (int, float) or not isfinite(timeout) or not 0 < timeout <= 15:
        raise ValueError('Routing timeout must be between 0 and 15 seconds.')
    maximum = config.get('ors_max_locations', 12)
    if type(maximum) is not int or not 2 <= maximum <= 16:
        raise ValueError('Routing location limit must be from 2 to 16.')
    if config.get('planning_data_mode', 'vistra') not in ('vistra', 'benchmark'):
        raise ValueError('Planning data mode must be vistra or benchmark.')
    for key, minimum, maximum in (('planning_max_steps', 1, 24), ('planning_max_repairs', 0, 2)):
        value = config.get(key, 20 if key == 'planning_max_steps' else 1)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f'{key} must be from {minimum} to {maximum}.')
    for key, minimum, maximum in (
        ("memory_recent_messages", 1, 20), ("memory_message_chars", 100, 8000),
        ("memory_context_chars", 4000, 32000), ("memory_preference_limit", 1, 20),
        ("memory_trip_limit", 1, 10),
    ):
        if key in config and (type(config[key]) is not int or not minimum <= config[key] <= maximum):
            raise ValueError(f"Config {key} must be an integer between {minimum} and {maximum}.")
    return config
