"""Explicit photo categories narrow the metadata scope before vector ranking."""

import re


CATEGORY_ALIASES = {
    "beach": {"beach"}, "museum": {"museum"}, "park": {"park", "garden"},
    "restaurant": {"restaurant"}, "cafe": {"cafe"},
    "market": {"market", "marketplace"}, "viewpoint": {"viewpoint"},
    "attraction": {"attraction"},
}


def metadata_photo_category(metadata):
    """Normalize dataset category labels for the current photo filters."""
    value = str(metadata.get("category") or "").casefold().strip()
    aliases = {
        "cafe": {"cafe", "coffee shop"},
        "restaurant": {"restaurant", "bistro", "brunch restaurant"},
        "market": {"market", "marketplace"},
        "attraction": {"attraction", "tourist attraction", "historical landmark",
                       "theme park", "amusement park", "zoo"},
    }
    if value.endswith(" restaurant"):
        return "restaurant"
    return next((key for key, values in aliases.items() if value in values), value or None)


def explicit_photo_categories(query):
    """Recognize direct English photo requests; callers can pass categories explicitly."""
    match = re.search(r"\b(?:images?|photos?|pictures?)\s+(?:of|showing)\s+(.+)", query or "", re.I)
    if not match:
        return []
    subject = match.group(1)
    categories = []
    for category in CATEGORY_ALIASES:
        plural = "beaches" if category == "beach" else category + "s"
        for token in re.finditer(r"\b(?:" + category + "|" + plural + r")\b", subject, re.I):
            prefix = subject[max(0, token.start()-25):token.start()]
            if not re.search(r"\b(?:not|no|without|excluding)(?:\s+\w+){0,2}\s*$", prefix, re.I):
                categories.append(category)
                break
    return categories


def checked_categories(values):
    if values is not None and (not isinstance(values, (list, tuple)) or len(values) > len(CATEGORY_ALIASES)
                               or any(not isinstance(value, str) for value in values)):
        from src.security.guardrails import UnsafeInput
        raise UnsafeInput("invalid_categories", "categories must be a short list of supported category names.")
    categories = list(dict.fromkeys(values or []))
    if set(categories) - CATEGORY_ALIASES.keys():
        from src.security.guardrails import UnsafeInput
        raise UnsafeInput("invalid_categories", "Unsupported photo category.")
    return categories


