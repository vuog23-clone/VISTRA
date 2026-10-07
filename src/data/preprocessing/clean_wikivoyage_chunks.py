"""Move embedded GeoJSON out of chunk text, including split map fragments."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.utils.utils import clean_text, ensure_parent, get_sha256, load_config, read_jsonl, resolve_path, write_jsonl_record


GEOJSON_TYPES = {
    "Feature", "FeatureCollection", "Point", "MultiPoint", "LineString",
    "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection",
}
JSON_START = re.compile(r'\{(?=\s*")')


def decode_commented_object(text: str, start: int) -> tuple[dict, int]:
    """Decode map JSON with comments/trailing commas, preserving source offsets."""
    chars = []
    stack = []
    position = start
    quoted = False
    escaped = False
    while position < len(text):
        char = text[position]
        if quoted:
            chars.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            position += 1
            continue
        if char == '"':
            quoted = True
        elif text.startswith("//", position):
            end = text.find("\n", position)
            end = len(text) if end < 0 else end
            chars.extend(" " * (end - position))
            position = end
            continue
        elif text.startswith("/*", position):
            end = text.find("*/", position + 2)
            if end < 0:
                raise ValueError("Unclosed map comment")
            end += 2
            chars.extend("\n" if c == "\n" else " " for c in text[position:end])
            position = end
            continue
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack or stack.pop() != ("{" if char == "}" else "["):
                raise ValueError("Unbalanced map object")
            if not stack:
                chars.append(char)
                position += 1
                break
        chars.append(char)
        position += 1
    if stack or quoted:
        raise ValueError("Incomplete map object")

    # Remove trailing commas outside strings, after comments have become spaces.
    compatible = "".join(chars)
    quoted = escaped = False
    for index, char in enumerate(compatible):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == ",":
            following = index + 1
            while following < len(compatible) and compatible[following].isspace():
                following += 1
            if following < len(compatible) and compatible[following] in "}]":
                chars[index] = " "
    return json.loads("".join(chars)), position


def geojson_blocks(text: str) -> list[tuple[int, int, dict]]:
    decoder = json.JSONDecoder()
    blocks = []
    position = 0
    while match := JSON_START.search(text, position):
        start = match.start()
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            try:
                value, end = decode_commented_object(text, start)
            except (ValueError, json.JSONDecodeError):
                position = start + 1
                continue
        if isinstance(value, dict) and value.get("type") in GEOJSON_TYPES:
            blocks.append((start, end, value))
            position = end
        else:
            position = start + 1
    return blocks


def remove_geojson_text(text: str) -> str:
    parts = []
    position = 0
    for start, end, _ in geojson_blocks(text):
        parts.append(text[position:start])
        parts.append("\n\n")
        position = end
    parts.append(text[position:])
    return clean_text("".join(parts))


def clean_chunks(chunks_path: Path, articles_path: Path, output_directory: Path) -> dict:
    source_hash = get_sha256(chunks_path)
    candidate = ensure_parent(output_directory / "chunks.cleaned.unverified.jsonl")
    geometry_path = output_directory / "map_geometry.jsonl"
    changes_path = output_directory / "changed_chunks.jsonl"
    summary_path = output_directory / "cleanup.summary.json"
    if {chunks_path.resolve(), articles_path.resolve()} & {candidate.resolve(), geometry_path.resolve(), changes_path.resolve(), summary_path.resolve()}:
        raise ValueError("Cleanup outputs must be separate from the source chunks.")

    # Match split fragments to the complete JSON objects in source articles.
    map_articles = {}
    map_count = 0
    with geometry_path.open("w", encoding="utf-8") as geometry:
        for article in read_jsonl(articles_path):
            text = clean_text(article.get("text", article.get("content", "")))
            if not any(marker in text for marker in ('"coordinates"', '"FeatureCollection"', '"GeometryCollection"')):
                continue
            blocks = geojson_blocks(text)
            if not blocks:
                continue
            page_id = article.get("page_id", article.get("id"))
            map_articles[page_id] = (text, blocks)
            for index, (start, end, value) in enumerate(blocks):
                write_jsonl_record(geometry, {
                    "page_id": page_id, "title": article.get("title"), "map_index": index,
                    "geojson": value, "raw_geojson": text[start:end],
                })
                map_count += 1

    summary = {
        "source_chunks_sha256": source_hash,
        "source_articles_sha256": get_sha256(articles_path),
        "input_records": 0, "output_records": 0, "changed_records": 0,
        "removed_map_only_records": 0, "removed_characters": 0,
        "map_articles": len(map_articles), "saved_maps": map_count,
        "unresolved_coordinate_records": [],
    }
    with candidate.open("w", encoding="utf-8") as output, changes_path.open("w", encoding="utf-8") as changes:
        for record in read_jsonl(chunks_path):
            summary["input_records"] += 1
            original = record["text"]
            text = original
            source = map_articles.get(record.get("page_id"))
            if source is not None:
                article_text, blocks = source
                location = article_text.find(original)
                if location >= 0:
                    limit = location + len(original)
                    parts = []
                    position = 0
                    for start, end, _ in blocks:
                        left, right = max(start, location), min(end, limit)
                        if left >= right:
                            continue
                        left, right = left - location, right - location
                        parts.extend((original[position:left], "\n\n"))
                        position = right
                    if position:
                        parts.append(original[position:])
                        text = clean_text("".join(parts))
                else:
                    # Complete objects can be removed even after a manual prose edit.
                    text = remove_geojson_text(original)
            elif '"coordinates"' in original:
                text = remove_geojson_text(original)

            if '"coordinates"' in text or text.count('],[') >= 20:
                summary["unresolved_coordinate_records"].append(record["chunk_id"])
            if text != original:
                summary["changed_records"] += 1
                summary["removed_characters"] += len(original) - len(text)
                write_jsonl_record(changes, {
                    "chunk_id": record["chunk_id"], "page_id": record.get("page_id"),
                    "title": record["title"], "section": record["section"],
                    "original_text": original, "cleaned_text": text,
                    "removed_from_embedding": not bool(text),
                })
                if not text:
                    summary["removed_map_only_records"] += 1
                    continue
                record = {
                    **record, "text": text,
                    "embedding_text": f"Destination: {record['title']}\nSection: {record['section']}\n\n{text}",
                }
            write_jsonl_record(output, record)
            summary["output_records"] += 1

    if get_sha256(chunks_path) != source_hash:
        raise RuntimeError("Source chunks changed during cleanup; rerun on the latest file.")
    summary["candidate_sha256"] = get_sha256(candidate)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return summary


if __name__ == "__main__":
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", default=config["wikivoyage_chunks_path"])
    parser.add_argument("--articles", default=config["wikivoyage_articles_path"])
    parser.add_argument("--output-directory", default=config["coordinate_cleanup_directory"])
    args = parser.parse_args()
    clean_chunks(resolve_path(args.chunks), resolve_path(args.articles), resolve_path(args.output_directory))
