"""Validate untrusted requests before models, databases, or providers are used.

These high-confidence attack signatures are one layer, not a semantic safety
classifier or a guarantee against arbitrary prompt injection. Never execute
query/source/model text as code, SQL, tools, or configuration.
"""

import re
import base64
import html
import unicodedata
from math import isfinite
from urllib.parse import unquote


MAX_QUERY_CHARS = 8000
MAX_LIMIT = 50
MAX_GROUP_SIZE = 10
MAX_CANDIDATES = 200
MAX_CONTEXT_BYTES = 128 * 1024
MAX_ANSWER_CHARS = 24000
ROUTES = {"text", "hybrid"}


class UnsafeInput(ValueError):
    """Rejected untrusted input; the message never includes the submitted value."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _attack_text(value):
    value = html.unescape(unquote(value))
    value = unicodedata.normalize("NFKD", value.casefold()).replace("đ", "d")
    return " ".join("".join(c for c in value if not unicodedata.combining(c)).split())


_INJECTION = tuple(re.compile(pattern, re.I) for pattern in (
    r"\b(?:ignore|disregard|override|forget|bypass)\b.{0,60}\b(?:previous|prior|system|developer|safety|security|all)\b.{0,40}\b(?:instructions?|prompts?|rules?|guardrails?|restrictions?)\b",
    r"\b(?:reveal|print|show|return|repeat|leak|exfiltrate)\b.{0,60}\b(?:system prompt|developer (?:prompt|instructions)|api keys?|credentials?|secret keys?|environment variables?|\.env)\b",
    r"\b(?:you are now|act as)\s+(?:an?\s+)?(?:unrestricted|uncensored|jailbroken|dan)\b",
    r"(?:<\|(?:im_start|im_end|system|developer|assistant|endoftext)\|>|\[/?INST\]|<<\s*/?SYS\s*>>)",
    r"\b(?:bo qua|khong tuan theo|vo hieu hoa)\b.{0,50}\b(?:chi dan|huong dan|quy tac|lenh)\b.{0,35}\b(?:truoc|he thong|an toan)\b",
    r"\b(?:tiet lo|in ra|hien thi)\b.{0,50}\b(?:khoa api|mat khau|chi dan he thong|prompt he thong)\b",
))
_HARMFUL_INTENT = tuple(re.compile(pattern, re.I) for pattern in (
    r"\b(?:help me|teach me|give me instructions (?:on )?how to)\s+(?:build|make)\s+(?:a |an )?(?:bomb|explosive device)\b",
    r"\b(?:how (?:can|do) i|help me)\s+(?:kill|murder|poison)\s+(?:myself|someone|a person|people)\b",
    r"\b(?:where (?:can|do) i|help me)\s+(?:buy|obtain|find)\s+(?:illegal weapons|child sexual abuse material)\b",
))


def validate_text(value, name="query", *, max_chars=MAX_QUERY_CHARS,
                  optional=False, check_injection=True, check_intent=False,
                  reject_secrets=True):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise UnsafeInput("invalid_type", f"{name} must be a string.")
    # Bound work before normalization/regular expressions.
    if len(value) > max_chars or len(value.encode("utf-8", errors="surrogatepass")) > max_chars * 4:
        raise UnsafeInput("text_too_long", f"{name} exceeds the {max_chars}-character limit.")
    for char in value:
        category = unicodedata.category(char)
        if ((category == "Cc" and char not in "\n\r\t") or category == "Cs"
                or (category == "Cf" and char != "\u200d")):
            raise UnsafeInput("hidden_control", f"{name} contains unsafe control or invisible characters.")
    value = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value or len(value) > max_chars:
        raise UnsafeInput("invalid_text", f"{name} must be nonblank and within its length limit.")
    normalized = _attack_text(value)
    variants = [normalized]
    if check_injection and re.search(r"\b(?:base64|decode)\b", normalized):
        for token in re.findall(r"[A-Za-z0-9+/]{20,4096}={0,2}", value)[:8]:
            try:
                variants.append(_attack_text(base64.b64decode(token, validate=True).decode("utf-8")))
            except (ValueError, UnicodeError):
                pass
    if check_injection and any(pattern.search(variant) for variant in variants for pattern in _INJECTION):
        raise UnsafeInput("prompt_injection", f"{name} contains an instruction-override or secret-extraction attempt.")
    if check_intent and any(pattern.search(normalized) for pattern in _HARMFUL_INTENT):
        raise UnsafeInput("unsafe_intent", "The request asks for harmful instructions.")
    if reject_secrets:
        from src.utils.redaction import CredentialRedactor
        if CredentialRedactor().text(value) != value:
            raise UnsafeInput("credential_input", f"Remove credentials from {name} before submitting it.")
    return value


def bounded_int(value, name, maximum, *, optional=False):
    if value is None and optional:
        return None
    if type(value) is not int or not 1 <= value <= maximum:
        raise UnsafeInput("invalid_limit", f"{name} must be an integer between 1 and {maximum}.")
    return value


def validate_threshold(value):
    if value is not None and (type(value) not in (int, float) or not isfinite(value)
                              or not -1 <= value <= 1):
        raise UnsafeInput("invalid_threshold", "score_threshold must be finite and between -1 and 1.")
    return value


def validate_ids(values, name):
    if values is None:
        return None
    if (not isinstance(values, (list, tuple)) or len(values) > 10000
            or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value)
                   for value in values)):
        raise UnsafeInput("invalid_scope", f"{name} must be a bounded list of dataset IDs.")
    return values


def validate_request(query, *, image=None, route=None, location=None,
                     visual_query=None, categories=None, limit=None,
                     group_size=None, score_threshold=None, **flags):
    query = validate_text(query, check_intent=True)
    if route is not None and (not isinstance(route, str) or route not in ROUTES | {"auto"}):
        raise UnsafeInput("invalid_route", "Unknown retrieval route.")
    location = validate_text(location, "location", max_chars=160, optional=True)
    visual_query = validate_text(visual_query, "visual_query", optional=True, check_intent=True)
    bounded_int(limit, "limit", MAX_LIMIT, optional=True)
    bounded_int(group_size, "group_size", MAX_GROUP_SIZE, optional=True)
    validate_threshold(score_threshold)
    for name, value in flags.items():
        if type(value) is not bool:
            raise UnsafeInput("invalid_flag", f"{name} must be a boolean.")
    from src.retrieval.category_filters import checked_categories
    categories = checked_categories(categories) if categories is not None else None
    return query, location, visual_query, categories


def validate_output_path(value):
    """CLI exports are new JSON files in the workspace, outside protected trees."""
    from src.utils.utils import PROJECT_ROOT
    from src.security.images import checked_local_path
    path = checked_local_path(value, must_exist=False)
    relative = path.relative_to(PROJECT_ROOT)
    protected = {"src", "datasets", "databases", ".git", ".codex", ".agents", ".aws", ".venv", ".cache", ".vscode"}
    if (path.suffix.lower() != ".json" or any(part.casefold() in protected for part in relative.parts)
            or path.exists()):
        raise UnsafeInput("unsafe_output", "Export to a new .json file outside protected project directories.")
    return path
