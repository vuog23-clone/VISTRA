"""Execution constraints for explicit evidence preferences and scenic discovery.

Jev handles general semantics. These narrow rules prevent a model prediction
from suppressing images for an imperative to find beautiful scenery, while
respecting explicit requests for text and retaining substantive facts.
"""

import re
import unicodedata


_DISCOVER = re.compile(
    r"^(?:(?:hello|hi|hey|xin chao|chao ban)[,!. ]+)?"
    r"(?:(?:please|can you|could you|would you|help me|i want you to|"
    r"i would like you to|vui long|lam on|ban co the|hay)\s+)*"
    r"(?:find|search(?: for)?|discover|locate|explore|tim|tim kiem)\b")
_AESTHETIC = re.compile(r"\b(?:beautiful|pretty|picturesque|scenic|stunning|gorgeous|"
                        r"breathtaking|spectacular|dep|thomong|tho mong|hung vi)\b")
_SCENERY = re.compile(r"\b(?:beach(?:es)?|mountains?|waterfalls?|landscapes?|scenery|"
                      r"sunsets?|lakes?|coastlines?|views?|bai bien|bien|nui|thac|"
                      r"phong canh|hoang hon|ho)\b")
_TEXT_ONLY = re.compile(
    r"\b(?:no|without|exclude)\s+(?:photos?|pictures?|images?)\b|"
    r"\b(?:do not|don't)\s+(?:show|include|retrieve).{0,25}\b(?:photos?|pictures?|images?)\b|"
    r"\b(?:text only|written list|only (?:names|text)|just (?:names|text))\b|"
    r"\bkhong\s+(?:(?:can|muon|lay|xem)\s+)?(?:anh|hinh)\b|"
    r"\bchi\s+(?:can\s+)?(?:ten|van ban)\b")
_NEGATED_FACTS = re.compile(r"\b(?:no|without|skip)\s+(?:history|directions?|guide facts?|explanations?)\b")
_DISCOVERY_METHOD = re.compile(r"^(?:(?:please|can you|could you)\s+)?"
                               r"(?:(?:tell me|explain|show me)\s+)?"
                               r"how\s+(?:to|do i|can i)\s+(?:find|search for|discover)\b")
_ADDITIONAL_VISUALS = re.compile(r"\b(?:and\s+(?:show|include)|include|with)\s+(?:some\s+)?"
                                r"(?:photos?|pictures?|images?)\b")
_WRITTEN_LIST = re.compile(r"\b(?:show|give|get|provide)\b.{0,20}\blist\b|\bdanh sach\b")
_PHOTO_WORDS = re.compile(r"\b(?:photos?|pictures?|images?|anh|hinh)\b")
_DETAILS = re.compile(
    r"\b(?:history|opening hours|hours|directions?|transport|itinerar(?:y|ies)|"
    r"budget|weather|safe|safety|swimming|activities|explain|costs?|prices?|fees?|"
    r"accessibility|accessible|crowded|crowds|best time|when to visit|"
    r"how (?:to|do|can|much)|nearby (?:restaurants?|hotels?)|"
    r"(?:hotel|restaurant) recommendations|lich su|gio mo cua|duong di|"
    r"lich trinh|chi phi|gia|an toan|boi|hoat dong|cach (?:di|den))\b")


def scenic_evidence_policy(query):
    """Return (required intent, reason), or (None, None) for Jev-only semantics."""
    normalized = unicodedata.normalize("NFKD", (query or "").casefold()).replace("đ", "d")
    normalized = " ".join("".join(c for c in normalized if not unicodedata.combining(c)).split())
    # Explicit preferences override scenic discovery, but not CLI overrides.
    if _TEXT_ONLY.search(normalized):
        return "text_only", "explicit_text_only"
    if _WRITTEN_LIST.search(normalized) and not _PHOTO_WORDS.search(normalized):
        return "text_only", "written_list"
    if _DISCOVERY_METHOD.search(normalized):
        return (("hybrid", "discovery_method_with_visuals") if _ADDITIONAL_VISUALS.search(normalized)
                else ("text_only", "discovery_method"))
    if not (_DISCOVER.search(normalized) and _AESTHETIC.search(normalized) and _SCENERY.search(normalized)):
        return None, None
    if _DETAILS.search(_NEGATED_FACTS.sub("", normalized)):
        return "hybrid", "scenic_discovery_with_facts"
    return "hybrid", "scenic_discovery"
