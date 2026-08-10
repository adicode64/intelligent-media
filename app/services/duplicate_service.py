"""
Perceptual-hash duplicate detection.

Strategy: compute a 64-bit pHash, then compare against previously stored hashes
with Hamming distance. Two bands are reported so we don't collapse "the same
photo" and "a similar-looking photo" into one verdict:

  distance <= duplicate_exact_distance    -> duplicate
  <= duplicate_similar_distance           -> similar (warning, not duplicate)
  otherwise                               -> not_duplicate

Limitation: a linear scan over recent hashes is O(n). Fine for a take-home and
for tens of thousands of rows; production would use a BK-tree or a pg `bit_count`
index over hash bands.
"""

from __future__ import annotations

import uuid
from typing import Any

from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.logging import get_logger
from app.models.enums import CheckStatus
from app.models.image import Image
from app.schemas.analysis import CheckResult
from app.utils.image_utils import hamming_distance

logger = get_logger(__name__)

HASH_BITS = 64


def compute_phash(image: PILImage.Image) -> str | None:
    """Perceptual hash as a hex string, or None if hashing is unavailable."""
    try:
        import imagehash

        return str(imagehash.phash(image))
    except ImportError:  # pragma: no cover - declared dependency
        logger.warning("imagehash_unavailable")
        return None
    except Exception as exc:
        logger.warning("phash_failed", extra={"error": str(exc)})
        return None


def find_nearest(
    db: Session, image_hash: str, exclude_image_id: uuid.UUID | None = None
) -> tuple[Image | None, int]:
    """Closest previously stored image by Hamming distance."""
    stmt = (
        select(Image)
        .where(Image.image_hash.is_not(None))
        .order_by(Image.created_at.desc())
        .limit(settings.duplicate_scan_limit)
    )
    if exclude_image_id is not None:
        stmt = stmt.where(Image.id != exclude_image_id)

    best: Image | None = None
    best_distance = HASH_BITS + 1
    for candidate in db.execute(stmt).scalars():
        try:
            distance = hamming_distance(image_hash, candidate.image_hash or "")
        except ValueError:
            continue  # hash produced by a different algorithm/length; skip
        if distance < best_distance:
            best, best_distance = candidate, distance
            if distance == 0:
                break
    return best, (best_distance if best else HASH_BITS + 1)


def check_duplicate(
    db: Session, image_hash: str | None, image_id: uuid.UUID | None = None
) -> CheckResult:
    if not image_hash:
        return CheckResult(
            name="duplicate",
            status=CheckStatus.SKIPPED,
            message="Perceptual hash unavailable, duplicate detection skipped.",
            confidence=0.0,
            details={"reason": "no_hash"},
        )

    try:
        match, distance = find_nearest(db, image_hash, exclude_image_id=image_id)
    except Exception as exc:
        logger.warning("duplicate_check_failed", extra={"error": str(exc)})
        return CheckResult(
            name="duplicate",
            status=CheckStatus.ERROR,
            message="Duplicate detection could not complete.",
            confidence=0.0,
            details={"error": type(exc).__name__},
        )

    details: dict[str, Any] = {
        "hash": image_hash,
        "algorithm": "phash_64bit",
        "exact_threshold": settings.duplicate_exact_distance,
        "similar_threshold": settings.duplicate_similar_distance,
        "compared_against": settings.duplicate_scan_limit,
    }

    if match is None:
        return CheckResult(
            name="duplicate",
            status=CheckStatus.PASS,
            score=0.0,
            value="not_duplicate",
            message="No previously stored image to compare against.",
            confidence=1.0,
            heuristic=False,
            details=details,
        )

    similarity = round(1 - distance / HASH_BITS, 4)
    details.update(
        {
            "matched_image_id": str(match.id),
            "hamming_distance": distance,
            "similarity": similarity,
        }
    )

    if distance <= settings.duplicate_exact_distance:
        return CheckResult(
            name="duplicate",
            status=CheckStatus.FAIL,
            score=float(distance),
            value="duplicate",
            message=(
                f"Likely duplicate of image {match.id} "
                f"(Hamming distance {distance}, similarity {similarity})."
            ),
            confidence=round(0.95 - 0.1 * distance, 3),
            details=details,
        )

    if distance <= settings.duplicate_similar_distance:
        return CheckResult(
            name="duplicate",
            status=CheckStatus.WARNING,
            score=float(distance),
            value="similar",
            message=(
                f"Visually similar to image {match.id} (distance {distance}) but not "
                "close enough to call a duplicate — could be another angle of the "
                "same vehicle."
            ),
            confidence=0.6,
            details=details,
        )

    return CheckResult(
        name="duplicate",
        status=CheckStatus.PASS,
        score=float(distance),
        value="not_duplicate",
        message=f"No duplicate detected (nearest distance {distance}).",
        confidence=0.9,
        details=details,
    )
