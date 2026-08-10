"""
Low-level image helpers: safe filenames, content sniffing, decoding, EXIF.

Security posture: the *bytes* decide whether something is an image, never the
extension or the client-supplied content type. Stored names are UUID-based, so a
hostile filename can never influence the path we write to.
"""

from __future__ import annotations

import io
import os
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from PIL import ExifTags, Image as PILImage, UnidentifiedImageError

from app.config import settings
from app.core.exceptions import CorruptImageError, ProcessingError

# Decompression-bomb guard: Pillow raises above this pixel count.
PILImage.MAX_IMAGE_PIXELS = 80_000_000

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Magic-byte prefixes we accept; keys are Pillow format names.
_MAGIC = {
    b"\xff\xd8\xff": "JPEG",
    b"\x89PNG\r\n\x1a\n": "PNG",
    b"BM": "BMP",
}

EXT_BY_FORMAT = {
    "JPEG": ".jpg",
    "MPO": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "BMP": ".bmp",
}


def sanitize_filename(filename: str | None, fallback: str = "upload") -> str:
    """Strip directories, control chars and unicode tricks from a client name.

    Only used for display/audit; it never forms part of the storage path.
    """
    if not filename:
        return fallback
    # Defeat both POSIX and Windows separators plus traversal segments.
    name = filename.replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = _SAFE_NAME_RE.sub("_", name).strip("._")
    name = name[:255]
    return name or fallback


def build_stored_filename(image_id: uuid.UUID, image_format: str) -> str:
    """UUID-based storage name; extension derived from the sniffed format."""
    return f"{image_id}{EXT_BY_FORMAT.get(image_format.upper(), '.bin')}"


def resolve_storage_path(stored_filename: str) -> Path:
    """Join into the storage root and refuse anything that escapes it."""
    root = settings.storage_path
    candidate = (root / os.path.basename(stored_filename)).resolve()
    if not str(candidate).startswith(str(root) + os.sep):
        raise ProcessingError("Resolved storage path escapes the storage root.")
    return candidate


def sniff_format(data: bytes) -> str | None:
    """Cheap magic-byte check before handing bytes to Pillow."""
    for magic, fmt in _MAGIC.items():
        if data.startswith(magic):
            return fmt
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def inspect_image_bytes(data: bytes) -> dict[str, Any]:
    """Verify the payload really is a decodable image and return its basics.

    Two passes are required: `verify()` invalidates the file object, so a second
    open is needed for anything else.
    """
    if not data:
        raise CorruptImageError("Uploaded file is empty.")
    try:
        with PILImage.open(io.BytesIO(data)) as probe:
            probe.verify()
        with PILImage.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            width, height = img.size
            mode = img.mode
    except UnidentifiedImageError as exc:
        raise CorruptImageError("File could not be identified as an image.") from exc
    except PILImage.DecompressionBombError as exc:
        raise CorruptImageError("Image resolution exceeds the safety limit.") from exc
    except Exception as exc:  # truncated / malformed payloads land here
        raise CorruptImageError("Image data is corrupted or unreadable.") from exc

    if width <= 0 or height <= 0:
        raise CorruptImageError("Image reports zero width or height.")

    return {"format": fmt, "width": width, "height": height, "mode": mode}


def load_pil_image(path: str | Path) -> PILImage.Image:
    try:
        img = PILImage.open(path)
        img.load()
        return img
    except FileNotFoundError as exc:
        raise ProcessingError("Stored image file is missing.") from exc
    except Exception as exc:
        raise ProcessingError("Stored image could not be decoded.") from exc


def to_grayscale_array(image: PILImage.Image) -> np.ndarray:
    """Grayscale uint8 array. Used by blur/brightness/screenshot heuristics."""
    return np.asarray(image.convert("L"), dtype=np.uint8)


def extract_exif(image: PILImage.Image) -> dict[str, Any]:
    """Best-effort EXIF extraction; absence of EXIF is normal, not suspicious."""
    out: dict[str, Any] = {}
    try:
        raw = image.getexif()
    except Exception:
        return out
    if not raw:
        return out

    for tag_id, value in raw.items():
        tag = ExifTags.TAGS.get(tag_id, str(tag_id))
        out[tag] = _coerce_exif_value(value)

    try:
        gps_ifd = raw.get_ifd(ExifTags.IFD.GPSInfo)
        if gps_ifd:
            out["GPSInfo"] = {
                ExifTags.GPSTAGS.get(k, str(k)): _coerce_exif_value(v)
                for k, v in gps_ifd.items()
            }
    except Exception:
        pass
    return out


def _coerce_exif_value(value: Any) -> Any:
    """EXIF values include bytes and IFDRational; make them JSON-safe."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[:256]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    if isinstance(value, tuple):
        return [_coerce_exif_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce_exif_value(v) for k, v in value.items()}
    return str(value)[:256]


def hamming_distance(hex_a: str, hex_b: str) -> int:
    """Hamming distance between two equal-length hex hash strings."""
    if not hex_a or not hex_b or len(hex_a) != len(hex_b):
        raise ValueError("Hash strings must be non-empty and the same length.")
    return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")
