"""Bounded local raster decoding; no URLs, devices, UNC paths, or metadata."""

from io import BytesIO
from pathlib import Path
import re
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from src.security.guardrails import UnsafeInput


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_EDGE = 1280
FORMATS = {"JPEG", "PNG", "WEBP"}
SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_DEVICE = re.compile(r"^(?:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\.|$)", re.I)


def checked_local_path(value, *, must_exist=True):
    from src.utils.utils import PROJECT_ROOT
    if not isinstance(value, (str, Path)):
        raise UnsafeInput("invalid_path", "Provide a local workspace path.")
    raw = str(value)
    if (not raw or len(raw) > 4096 or raw.startswith(("\\\\", "//"))
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
        raise UnsafeInput("unsafe_path", "Network, device, or control-character paths are not allowed.")
    path = Path(raw)
    if (":" in raw.replace(path.drive, "", 1) or any(
            _DEVICE.match(part) or part.endswith((" ", ".")) for part in path.parts if part != path.anchor)):
        raise UnsafeInput("unsafe_path", "Device names and alternate data streams are not allowed.")
    try:
        path = (path if path.is_absolute() else PROJECT_ROOT / path).resolve(strict=must_exist)
        path.relative_to(PROJECT_ROOT)
    except (OSError, RuntimeError, ValueError):
        raise UnsafeInput("unsafe_path", "The path must resolve inside the workspace.") from None
    if any(part.casefold().startswith(".") for part in path.relative_to(PROJECT_ROOT).parts):
        raise UnsafeInput("protected_path", "Hidden project paths cannot be used as input or output.")
    if must_exist and not path.is_file():
        raise UnsafeInput("invalid_file", "The input must be a regular file.")
    return path


def _check_image(image):
    if (image.width < 1 or image.height < 1 or image.width * image.height > MAX_IMAGE_PIXELS
            or max(image.size) > MAX_IMAGE_PIXELS):
        raise UnsafeInput("image_dimensions", "Image dimensions exceed the safety limit.")
    if getattr(image, "n_frames", 1) != 1:
        raise UnsafeInput("animated_image", "Use a single-frame image.")


def decode_image_bytes(content):
    if not isinstance(content, (bytes, bytearray)) or len(content) > MAX_IMAGE_BYTES:
        raise UnsafeInput("image_size", "Image files must be at most 8 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content), formats=list(FORMATS)) as image:
                _check_image(image)
                image.verify()
            with Image.open(BytesIO(content), formats=list(FORMATS)) as image:
                return _clean_image(image)
    except UnsafeInput:
        raise
    except (OSError, SyntaxError, ValueError, UnidentifiedImageError,
            Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise UnsafeInput("invalid_image", "The file is not a valid supported raster image.") from None


def _clean_image(image):
    _check_image(image)
    image.load()
    with ImageOps.exif_transpose(image) as oriented:
        clean = oriented.convert("RGB")
    clean.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    # Copy pixels into a fresh object: EXIF, comments, ICC and ancillary data
    # never reach embedding models or downstream consumers.
    result = Image.new("RGB", clean.size)
    result.paste(clean)
    clean.close()
    return result


def load_safe_image(value):
    if isinstance(value, Image.Image):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                return _clean_image(value)
        except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise UnsafeInput("invalid_image", "The supplied image cannot be safely decoded.") from None
    path = checked_local_path(value)
    if path.suffix.lower() not in SUFFIXES:
        raise UnsafeInput("image_format", "Use a JPEG, PNG, or WebP image.")
    # Read at most limit + 1, even if the file grows after stat().
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise UnsafeInput("image_size", "Image files must be at most 8 MB.")
    with path.open("rb") as file:
        content = file.read(MAX_IMAGE_BYTES + 1)
    return decode_image_bytes(content)
