"""Remove embedded GeoJSON from Wikivoyage prose before tokenization.

This helper is part of normal chunk preparation and writes no audit artifacts.
"""

import json
import re

from src.utils.utils import clean_text


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
