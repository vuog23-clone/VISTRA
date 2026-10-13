"""Bounded Google place facts for RAG, without photo URLs or local paths."""
from math import isfinite


def google_place_context(result, max_chars=1800):
    place = result.metadata.get("place") or result.payload
    def text(value):
        return value[:max_chars] if isinstance(value, str) else None
    def number(value):
        return value if type(value) in (int, float) and isfinite(value) else None
    facts = {"address": text(result.address), "province": text(result.province),
             "category": text(result.category), "rating": number(place.get("rating")),
             "review_count": number(place.get("review_count"))}
    price = place.get("price_range")
    facts["price_range"] = ({"min": number(price.get("min")), "max": number(price.get("max")),
                             "currency": text(price.get("currency"))} if isinstance(price, dict) else None)
    hours = place.get("opening_hours")
    if isinstance(hours, dict):
        clean = {"timezone": text(hours.get("timezone"))}
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
            intervals = hours.get(day)
            clean[day] = ([{"open": text(row.get("open")), "close": text(row.get("close"))}
                           for row in intervals[:8] if isinstance(row, dict)]
                          if isinstance(intervals, list) else None)
        facts["opening_hours"] = clean
    else:
        facts["opening_hours"] = None
    return facts
