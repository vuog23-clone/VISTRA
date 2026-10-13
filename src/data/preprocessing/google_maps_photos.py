"""Resolve public place cover photos from google_maps_url without a Google key.

Only the visible cover photo of the requested place is selected. Map tiles,
avatars, review photos and nearby-place suggestions are never substitutes.
Photo pixels are fetched into memory; source JSON and local photo folders are
not used or modified. Google may change its DOM or make a photo unavailable.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from time import monotonic, sleep
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from src.data.google_payload import place_payload
from src.utils.utils import load_config, resolve_path, stable_uuid
from src.security.guardrails import validate_text, UnsafeInput
from src.security.images import decode_image_bytes, MAX_IMAGE_BYTES


def checked_maps_url(value):
    value = validate_text(value, "Maps URL", max_chars=4096, check_injection=False, reject_secrets=False)
    if not isinstance(value, str):
        raise ValueError("google_maps_url must be a Google Maps place-page URL.")
    parts = urlsplit(value)
    if (parts.scheme != "https" or parts.hostname not in {"www.google.com", "google.com", "maps.google.com"}
            or parts.username or parts.password or parts.port not in (None, 443)
            or "\\" in value or any(c.isspace() for c in value)
            or not parts.path.startswith("/maps/place/")):
        raise ValueError("Use a full HTTPS Google Maps /maps/place/ URL.")
    return value


def checked_photo_url(value):
    """Allow actual Google-hosted place photos, not arbitrary fetch destinations."""
    value = validate_text(value, "photo URL", max_chars=4096, check_injection=False, reject_secrets=False)
    if not isinstance(value, str):
        raise ValueError("The place photo must have a direct HTTPS image URL.")
    parts = urlsplit(value)
    hosted_photo = (re.fullmatch(r"lh\d+\.(?:googleusercontent\.com|ggpht\.com)", parts.hostname or "")
        and parts.path.startswith(("/p/", "/gps-cs-s/", "/gps-proxy/", "/grass-cs/", "/grass-proxy/")))
    street_photo = (parts.hostname == "streetviewpixels-pa.googleapis.com"
        and parts.path == "/v1/thumbnail" and dict(parse_qsl(parts.query)).get("panoid"))
    if (parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443)
            or "\\" in value or any(c.isspace() for c in value) or not (hosted_photo or street_photo)):
        raise ValueError("Not a public Google Maps place-photo URL.")
    return value


def photo_identity(url):
    """Ignore display-size variants when computing an image's stable point ID."""
    parts = urlsplit(checked_photo_url(url))
    if parts.hostname == "streetviewpixels-pa.googleapis.com":
        query = {key: value for key, value in parse_qsl(parts.query)
                 if key in {"panoid", "yaw", "pitch", "thumbfov"}}
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(query.items())), ""))
    path = re.sub(r"=(?:w|s|h)\d.*$", "", parts.path)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


@dataclass(frozen=True)
class MapsPhoto:
    point_id: str
    image_url: str
    metadata: dict


def load_places(dataset_path=None):
    config = load_config()
    root = resolve_path(dataset_path, config["google_dataset_path"])
    paths = sorted(root.glob("*/metadata/*.json"))
    if not paths:
        raise FileNotFoundError(f"No Google metadata JSON files found in {root}")
    places = {}
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"Expected a place object: {path}")
        data = place_payload(data)
        place_id = data.get("place_id")
        if not isinstance(place_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", place_id):
            raise ValueError(f"Invalid place_id: {path}")
        if place_id in places:
            raise ValueError(f"Duplicate Google place_id: {place_id}")
        if not isinstance(data.get("place_name"), str) or not data["place_name"].strip():
            raise ValueError(f"Missing place_name: {path}")
        checked_maps_url(data.get("google_maps_url"))
        json.dumps(data, allow_nan=False)
        places[place_id] = data
    # Reuse database validation; this never connects to PostgreSQL.
    from src.database.postgres_google_metadata_store import prepare_rows
    prepare_rows(list(places.values()), [])
    return places


class GoogleMapsPhotoResolver:
    """Render a public Maps place page and read its visible cover-photo src."""

    def __init__(self, *, context=None, timeout_ms=30000):
        self.context = context
        self.timeout_ms = timeout_ms
        self._playwright = self._browser = None

    def _context(self):
        if self.context is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as error:
                raise RuntimeError("Install optional indexing dependencies: pip install 'playwright>=1.55,<2'; "
                                   "then python -m playwright install chromium.") from error
            self._playwright = sync_playwright().start()
            try:
                edge = Path(os.getenv("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
                # Use the installed Edge engine on Windows; otherwise use the
                # Chromium binary installed by `playwright install chromium`.
                channel = "msedge" if os.name == "nt" and edge.is_file() else None
                self._browser = self._playwright.chromium.launch(headless=True, channel=channel)
                self.context = self._browser.new_context(locale="en-US", accept_downloads=False)
            except Exception:
                self.close()
                raise
        return self.context

    def resolve(self, place):
        url = checked_maps_url(place.get("google_maps_url"))
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query["hl"] = "en"
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        page = self._context().new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            # Stop on a challenge/consent/error page; do not substitute its imagery.
            checked_maps_url(page.url)
            heading = page.get_by_role("heading", level=1).first
            heading.wait_for(state="visible", timeout=self.timeout_ms)
            name = heading.inner_text().strip()
            if not name:
                raise ValueError("The public place page has no place heading.")
            # Google may have renamed the place; scope to its own displayed name,
            # never to arbitrary images elsewhere on the page.
            image = page.get_by_role("button", name=f"Photo of {name}", exact=True).locator("img")
            if "\ufffd" in name:
                # Some Maps headings contain a replacement character while the
                # accessible cover label retains the actual accented character.
                label = re.compile("^Photo of " + re.escape(name).replace("\ufffd", ".") + "$")
                image = page.get_by_role("button", name=label).locator("img")
            image.wait_for(state="visible", timeout=self.timeout_ms)
            # The visible <img> initially has a placeholder or no src. Wait for
            # this exact cover element to receive the real remote photo URL.
            deadline = monotonic() + self.timeout_ms / 1000
            while True:
                try:
                    image_url = checked_photo_url(image.get_attribute("src"))
                    break
                except ValueError:
                    if monotonic() >= deadline:
                        raise ValueError("The place cover did not load a public photo URL.")
                    sleep(0.1)
            point_id = stable_uuid(f"google-maps-cover:{place['place_id']}")
            return MapsPhoto(point_id, image_url, place_payload(place))
        finally:
            page.close()

    def close(self):
        if self._browser is not None:
            self._browser.close()
            self._browser = None
            self.context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None


def fetch_photo(url, *, client=None, max_bytes=8 * 1024 * 1024):
    """Fetch a bounded public image and decode RGB pixels without writing a file."""
    url = checked_photo_url(url)
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_IMAGE_BYTES:
        raise UnsafeInput("image_size", "The remote photo byte limit must be at most 8 MB.")
    deadline = monotonic() + 45
    owns_client = client is None
    client = httpx.Client(timeout=30, follow_redirects=False, trust_env=False) if client is None else client
    try:
        # Validate each redirect before requesting it. Never forward credentials.
        for _ in range(4):
            # Public Street View thumbnails reject HTTPX's default agent even
            # when the same URL renders on Maps. Identify our actual client.
            with client.stream("GET", url, follow_redirects=False,
                               headers={"Accept-Encoding": "identity", "User-Agent": "VISTRA/1.0"}) as response:
                if response.is_redirect:
                    url = checked_photo_url(str(response.url.join(response.headers["location"])))
                    continue
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise UnsafeInput("photo_encoding", "Compressed photo responses are not allowed.")
                if not response.headers.get("content-type", "").lower().startswith("image/"):
                    raise ValueError("The resolved photo URL returned a page, not an image.")
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(content) + len(chunk) > max_bytes:
                        raise ValueError("The remote photo exceeds the 8 MB limit.")
                    if monotonic() > deadline:
                        raise UnsafeInput("photo_timeout", "Photo retrieval exceeded its time budget.")
                    content.extend(chunk)
                return decode_image_bytes(content)
        raise ValueError("The photo URL redirected too many times.")
    finally:
        if owns_client:
            client.close()
