"""Bounded responses and a credential-safe TypeSafe endpoint boundary."""

import json
from time import monotonic
from urllib.parse import urlsplit

from src.security.guardrails import UnsafeInput


def checked_provider_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 for c in value):
        raise UnsafeInput("unsafe_endpoint", "Invalid TypeSafe endpoint.")
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == "https" and parts.hostname == "api.typesafe.ai"
                 and parts.port in (None, 443) and not parts.username and not parts.password
                 and not parts.query and not parts.fragment and "\\" not in value)
    except ValueError:
        valid = False
    if not valid:
        raise UnsafeInput("unsafe_endpoint", "TypeSafe credentials require its trusted HTTPS endpoint.")
    return value


def post_provider_json(client, url, *, max_bytes=65536, **kwargs):
    url = checked_provider_url(url)
    if type(max_bytes) is not int or not 1 <= max_bytes <= 65536:
        raise UnsafeInput("provider_response_size", "Provider response limits must be at most 64 KB.")
    deadline = monotonic() + 30
    headers = dict(kwargs.pop("headers", {}))
    headers["Accept-Encoding"] = "identity"
    kwargs["headers"] = headers
    with client.stream("POST", url, follow_redirects=False, **kwargs) as response:
        response.raise_for_status()
        if response.is_redirect:
            raise UnsafeInput("provider_redirect", "Provider redirects are not allowed.")
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if mime != "application/json" and not (mime.startswith("application/") and mime.endswith("+json")):
            raise UnsafeInput("provider_response", "The provider returned a non-JSON response.")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise UnsafeInput("provider_encoding", "Compressed provider responses are not allowed.")
        content = bytearray()
        for chunk in response.iter_bytes(chunk_size=65536):
            if len(content) + len(chunk) > max_bytes:
                raise UnsafeInput("provider_response_size", "The provider response exceeded its size limit.")
            content.extend(chunk)
            if monotonic() > deadline:
                raise UnsafeInput("provider_timeout", "The provider response exceeded its time budget.")
        try:
            value = json.loads(content)
        except (ValueError, RecursionError):
            raise UnsafeInput("provider_response", "The provider returned invalid JSON.") from None
        if not isinstance(value, dict):
            raise UnsafeInput("provider_response", "The provider response must be an object.")
        return value, response.status_code
