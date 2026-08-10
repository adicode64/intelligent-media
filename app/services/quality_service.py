"""
Quality checks: blur, brightness, dimensions, screenshot/photo-of-photo, EXIF.

All five are threshold heuristics on cheap statistics. They are useful triage
signals and explicitly *not* classifiers: each result carries `heuristic=True`
and a confidence that degrades near the threshold boundary.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image as PILImage

from app.config import settings
from app.core.logging import get_logger
from app.models.enums import CheckStatus
from app.schemas.analysis import CheckResult
from app.utils.image_utils import extract_exif, to_grayscale_array

logger = get_logger(__name__)


def _boundary_confidence(score: float, threshold: float, span: float) -> float:
    """Confidence grows with distance from the decision boundary.

    A Laplacian variance of 61 against a threshold of 60 is a coin flip; 400 is
    not. This keeps us from reporting false certainty.
    """
    if span <= 0:
        return 0.6
    ratio = min(abs(score - threshold) / span, 1.0)
    return round(0.55 + 0.4 * ratio, 3)


# ---------------------------------------------------------------------- blur
def check_blur(gray: np.ndarray) -> CheckResult:
    """Variance of the Laplacian: low variance => few sharp edges => blurry.

    Caveat: intentionally shallow depth-of-field or a flat background lowers the
    score too, so this measures "edge energy", not photographic intent.
    """
    try:
        import cv2

        score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    except ImportError:  # pragma: no cover - OpenCV is a hard dependency
        # Pure-numpy fallback keeps the pipeline alive if OpenCV is unavailable.
        kernel_out = (
            -4.0 * gray[1:-1, 1:-1].astype(np.float64)
            + gray[:-2, 1:-1]
            + gray[2:, 1:-1]
            + gray[1:-1, :-2]
            + gray[1:-1, 2:]
        )
        score = float(np.var(kernel_out))
    except Exception as exc:
        logger.warning("blur_check_failed", extra={"error": str(exc)})
        return CheckResult(
            name="blur",
            status=CheckStatus.ERROR,
            message="Blur detection could not run on this image.",
            confidence=0.0,
            details={"error": type(exc).__name__},
        )

    score = round(score, 2)
    if score < settings.blur_fail_threshold:
        status, msg = CheckStatus.FAIL, "Image appears blurry; details may be unreadable."
        conf = _boundary_confidence(score, settings.blur_fail_threshold, 60.0)
    elif score < settings.blur_warn_threshold:
        status, msg = CheckStatus.WARNING, "Image is borderline sharp; some detail loss likely."
        conf = 0.6
    else:
        status, msg = CheckStatus.PASS, "Image appears sufficiently sharp."
        conf = _boundary_confidence(score, settings.blur_warn_threshold, 200.0)

    return CheckResult(
        name="blur",
        status=status,
        score=score,
        message=msg,
        confidence=conf,
        details={
            "metric": "laplacian_variance",
            "fail_threshold": settings.blur_fail_threshold,
            "warn_threshold": settings.blur_warn_threshold,
        },
    )


# ---------------------------------------------------------------- brightness
def check_brightness(gray: np.ndarray) -> CheckResult:
    """Mean grayscale intensity, with std-dev reported for context."""
    try:
        score = round(float(np.mean(gray)), 2)
        spread = round(float(np.std(gray)), 2)
    except Exception as exc:
        return CheckResult(
            name="brightness",
            status=CheckStatus.ERROR,
            message="Brightness could not be measured.",
            confidence=0.0,
            details={"error": type(exc).__name__},
        )

    if score < settings.brightness_dark_threshold:
        classification, status = "too_dark", CheckStatus.FAIL
        msg = "Image is too dark; low-light capture likely hides detail."
    elif score < settings.brightness_warn_dark_threshold:
        classification, status = "acceptable", CheckStatus.WARNING
        msg = "Image may be slightly dark."
    elif score > settings.brightness_bright_threshold:
        classification, status = "too_bright", CheckStatus.FAIL
        msg = "Image is overexposed; highlights are likely clipped."
    elif score > settings.brightness_warn_bright_threshold:
        classification, status = "acceptable", CheckStatus.WARNING
        msg = "Image may be slightly overexposed."
    else:
        classification, status = "acceptable", CheckStatus.PASS
        msg = "Image brightness is within the acceptable range."

    return CheckResult(
        name="brightness",
        status=status,
        score=score,
        value=classification,
        message=msg,
        confidence=0.8 if status is CheckStatus.PASS else 0.7,
        details={
            "metric": "mean_grayscale_intensity",
            "scale": "0-255",
            "std_dev": spread,
            "classification": classification,
        },
    )


# ---------------------------------------------------------------- dimensions
def check_dimensions(width: int, height: int) -> CheckResult:
    """Deterministic resolution / aspect-ratio gate (not a heuristic)."""
    warnings: list[str] = []
    status = CheckStatus.PASS
    aspect = round(width / height, 3) if height else 0.0

    if width < settings.min_image_width or height < settings.min_image_height:
        status = CheckStatus.FAIL
        warnings.append(
            f"Resolution {width}x{height} is below the minimum "
            f"{settings.min_image_width}x{settings.min_image_height}."
        )
    elif width < settings.warn_image_width or height < settings.warn_image_height:
        status = CheckStatus.WARNING
        warnings.append(
            f"Resolution {width}x{height} is low; OCR accuracy may suffer."
        )

    if aspect and not (settings.min_aspect_ratio <= aspect <= settings.max_aspect_ratio):
        if status is not CheckStatus.FAIL:
            status = CheckStatus.WARNING
        warnings.append(
            f"Aspect ratio {aspect} is outside the expected range "
            f"{settings.min_aspect_ratio}-{settings.max_aspect_ratio}; "
            "the image may be cropped or stretched."
        )

    return CheckResult(
        name="dimensions",
        status=status,
        score=float(width * height),
        value=f"{width}x{height}",
        message="; ".join(warnings) or "Image dimensions are acceptable.",
        confidence=1.0,
        heuristic=False,
        details={
            "width": width,
            "height": height,
            "aspect_ratio": aspect,
            "megapixels": round(width * height / 1_000_000, 2),
            "warnings": warnings,
        },
    )


# ------------------------------------------------- screenshot / photo-of-photo
def check_screenshot_like(gray: np.ndarray, exif: dict[str, Any]) -> CheckResult:
    """Weak-signal heuristic. Deliberately reports `uncertain`, never `fail`.

    Signals combined:
      * no EXIF camera fields (screenshots are re-encoded without them)
      * long runs of perfectly uniform rows (UI chrome / letterboxing)
      * a flat, low-variance histogram typical of synthetic UI content
      * a strong rectangular border (screen bezel in a photo-of-photo)

    None of these is conclusive on its own: an exported/edited camera photo can
    trip all of them. Treat the output as a review prompt.
    """
    try:
        signals: list[str] = []
        score = 0.0
        h, w = gray.shape[:2]

        has_camera_exif = any(k in exif for k in ("Make", "Model", "LensModel", "FNumber"))
        if not has_camera_exif:
            score += 0.25
            signals.append("no_camera_exif")

        software = str(exif.get("Software", "")).lower()
        if any(tok in software for tok in ("screenshot", "screencapture", "snip")):
            score += 0.35
            signals.append("screenshot_software_tag")

        # Uniform rows: identical pixel values across an entire row.
        row_min = gray.min(axis=1)
        row_max = gray.max(axis=1)
        uniform_rows = float(np.mean((row_max - row_min) <= 2))
        if uniform_rows >= settings.screenshot_uniform_row_ratio:
            score += 0.2
            signals.append(f"uniform_rows={round(uniform_rows, 3)}")

        # Synthetic UI content concentrates mass in few intensity levels.
        hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
        hist /= max(hist.sum(), 1.0)
        top_mass = float(np.sort(hist)[-8:].sum())
        if top_mass > 0.6:
            score += 0.2
            signals.append(f"flat_histogram_top8={round(top_mass, 3)}")

        # Border darker/brighter than the interior => possible bezel or frame.
        border = np.concatenate(
            [gray[:4, :].ravel(), gray[-4:, :].ravel(), gray[:, :4].ravel(), gray[:, -4:].ravel()]
        )
        interior = gray[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]
        if interior.size and abs(float(border.mean()) - float(interior.mean())) > 60:
            score += 0.2
            signals.append("strong_border_contrast")

        score = round(min(score, 1.0), 3)
        if score >= 0.6:
            status = CheckStatus.WARNING
            msg = (
                "Heuristic signals suggest this may be a screenshot or a photo of a "
                "screen/printout. Manual review recommended — this is not a classifier."
            )
        elif score >= 0.35:
            status = CheckStatus.UNCERTAIN
            msg = (
                "Some screenshot-like signals present, but the evidence is weak and "
                "inconclusive."
            )
        else:
            status = CheckStatus.PASS
            msg = "No strong screenshot or photo-of-photo signals detected."

        return CheckResult(
            name="screenshot_heuristic",
            status=status,
            score=score,
            message=msg,
            # Cap confidence: this check is never trustworthy on its own.
            confidence=round(min(0.5 + score / 3, 0.7), 3),
            details={"signals": signals, "note": "Weak-signal heuristic, not an AI classifier."},
        )
    except Exception as exc:
        logger.warning("screenshot_check_failed", extra={"error": str(exc)})
        return CheckResult(
            name="screenshot_heuristic",
            status=CheckStatus.ERROR,
            message="Screenshot heuristic could not run.",
            confidence=0.0,
            details={"error": type(exc).__name__},
        )


# ------------------------------------------------------------------ metadata
def check_metadata(image: PILImage.Image) -> CheckResult:
    """Report what EXIF is available. Missing EXIF is informational only.

    Stripping metadata is the default for most messaging apps and web pipelines,
    so absence carries almost no evidential weight.
    """
    try:
        exif = extract_exif(image)
    except Exception as exc:
        return CheckResult(
            name="metadata",
            status=CheckStatus.ERROR,
            message="EXIF metadata could not be read.",
            confidence=0.0,
            details={"error": type(exc).__name__},
        )

    camera = " ".join(
        str(exif[k]) for k in ("Make", "Model") if exif.get(k)
    ).strip() or None
    timestamp = exif.get("DateTimeOriginal") or exif.get("DateTime")
    gps_available = bool(exif.get("GPSInfo"))
    software = exif.get("Software")

    present = [
        label
        for label, ok in (
            ("camera", bool(camera)),
            ("timestamp", bool(timestamp)),
            ("gps", gps_available),
            ("software", bool(software)),
        )
        if ok
    ]

    if not exif:
        status = CheckStatus.SKIPPED
        msg = (
            "No EXIF metadata present. This is common for edited, exported or "
            "messaging-app images and is not treated as suspicious."
        )
    else:
        status = CheckStatus.PASS
        msg = f"EXIF metadata available: {', '.join(present) or 'basic fields only'}."

    return CheckResult(
        name="metadata",
        status=status,
        message=msg,
        value=camera,
        confidence=1.0,
        heuristic=False,
        details={
            "camera": camera,
            "timestamp": str(timestamp) if timestamp else None,
            "gps_available": gps_available,
            "software": str(software) if software else None,
            "editing_software_detected": bool(
                software and any(t in str(software).lower() for t in ("photoshop", "gimp", "lightroom"))
            ),
            "exif_tag_count": len(exif),
            "fields_present": present,
        },
    )
