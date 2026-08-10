"""
Analysis orchestration: runs every check and aggregates one verdict.

Contract: `analyze_image` never raises for an individual check failure. A check
that blows up is recorded with `status="error"` so partial results are still
useful; only unrecoverable problems (missing/undecodable file) raise, because
those mean the whole job is meaningless.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.core.exceptions import ProcessingError
from app.core.logging import get_logger
from app.models.enums import CheckStatus
from app.schemas.analysis import AnalysisResponse, AnalysisSummary, CheckResult
from app.services import duplicate_service, ocr_service, quality_service, validation_service
from app.utils.image_utils import extract_exif, load_pil_image, to_grayscale_array

logger = get_logger(__name__)

# Weights reflect how much each signal should move the aggregate verdict.
CHECK_WEIGHTS: dict[str, float] = {
    "blur": 1.0,
    "brightness": 0.8,
    "dimensions": 0.9,
    "duplicate": 1.0,
    "vehicle_number": 1.2,
    "screenshot_heuristic": 0.5,
    "metadata": 0.2,
}


def run_checks(
    db: Session, storage_path: str, image_id: uuid.UUID | None = None
) -> tuple[list[CheckResult], str | None, dict[str, Any]]:
    """Execute all checks. Returns (checks, vehicle_number, raw_signals)."""
    pil_image = load_pil_image(storage_path)  # raises ProcessingError if unusable
    try:
        width, height = pil_image.size
        gray = to_grayscale_array(pil_image)
        exif = extract_exif(pil_image)

        checks: list[CheckResult] = [
            quality_service.check_blur(gray),
            quality_service.check_brightness(gray),
            quality_service.check_dimensions(width, height),
        ]

        # --- duplicate -------------------------------------------------------
        image_hash = duplicate_service.compute_phash(pil_image)
        checks.append(duplicate_service.check_duplicate(db, image_hash, image_id=image_id))

        # --- OCR + plate validation -----------------------------------------
        ocr = ocr_service.extract_vehicle_number(pil_image)
        validation = validation_service.validate_plate(ocr.normalized_text, ocr.confidence)
        ocr_blocking_error = ocr.error if ocr.normalized_text is None else None
        plate_check = validation_service.build_vehicle_number_check(
            validation, ocr.confidence, ocr_blocking_error
        )
        plate_check.details["ocr"] = ocr.model_dump()
        checks.append(plate_check)

        # --- weak-signal heuristics + metadata ------------------------------
        checks.append(quality_service.check_screenshot_like(gray, exif))
        checks.append(quality_service.check_metadata(pil_image))

        vehicle_number = (
            validation.extracted_number
            if plate_check.status in (CheckStatus.PASS, CheckStatus.WARNING)
            else None
        )
        signals = {"image_hash": image_hash, "width": width, "height": height}
        return checks, vehicle_number, signals
    finally:
        try:
            pil_image.close()
        except Exception:  # pragma: no cover
            pass


def summarize(checks: list[CheckResult]) -> AnalysisSummary:
    """Aggregate check statuses into an overall verdict + confidence.

    Confidence is a weighted mean of per-check confidences, so a run where most
    signals were uncertain cannot present itself as a confident verdict.
    """
    counts = {status: 0 for status in CheckStatus}
    notes: list[str] = []
    weighted_conf = 0.0
    total_weight = 0.0

    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
        weight = CHECK_WEIGHTS.get(check.name, 0.5)
        if check.status in (CheckStatus.SKIPPED, CheckStatus.ERROR):
            notes.append(f"{check.name}: {check.message}")
            continue
        weighted_conf += check.confidence * weight
        total_weight += weight
        if check.status in (CheckStatus.FAIL, CheckStatus.WARNING, CheckStatus.UNCERTAIN):
            notes.append(f"{check.name}: {check.message}")

    failures = counts[CheckStatus.FAIL]
    warnings = counts[CheckStatus.WARNING]
    uncertain = counts[CheckStatus.UNCERTAIN]
    errors = counts[CheckStatus.ERROR]

    if failures:
        overall = "fail"
    elif warnings:
        overall = "warning"
    elif uncertain or errors:
        overall = "uncertain"
    else:
        overall = "pass"

    confidence = round(weighted_conf / total_weight, 3) if total_weight else 0.0
    if errors:
        # Missing signals mean we know less; reflect that instead of hiding it.
        confidence = round(confidence * max(0.5, 1 - 0.15 * errors), 3)

    return AnalysisSummary(
        overall_status=overall,
        confidence=min(max(confidence, 0.0), 1.0),
        passed=counts[CheckStatus.PASS],
        warnings=warnings,
        failures=failures,
        uncertain=uncertain + errors,
        notes=notes[:12],
    )


def analyze_image(
    db: Session, storage_path: str, image_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Full analysis. Returns a JSON-serialisable payload for persistence."""
    log = get_logger(__name__, image_id=str(image_id) if image_id else None)
    try:
        checks, vehicle_number, signals = run_checks(db, storage_path, image_id=image_id)
    except ProcessingError:
        raise
    except Exception as exc:  # defensive: unexpected library error
        log.error("analysis_unexpected_error", extra={"error": str(exc)})
        raise ProcessingError(f"Analysis aborted: {type(exc).__name__}") from exc

    summary = summarize(checks)
    log.info(
        "analysis_completed",
        extra={
            "overall_status": summary.overall_status,
            "confidence": summary.confidence,
            "vehicle_number": vehicle_number,
        },
    )
    return {
        "summary": summary.model_dump(),
        "checks": [c.model_dump() for c in checks],
        "vehicle_number": vehicle_number,
        "signals": signals,
        "config_snapshot": {
            "blur_fail_threshold": settings.blur_fail_threshold,
            "blur_warn_threshold": settings.blur_warn_threshold,
            "brightness_dark_threshold": settings.brightness_dark_threshold,
            "brightness_bright_threshold": settings.brightness_bright_threshold,
            "duplicate_exact_distance": settings.duplicate_exact_distance,
            "duplicate_similar_distance": settings.duplicate_similar_distance,
            "min_resolution": f"{settings.min_image_width}x{settings.min_image_height}",
        },
    }


def build_response(image, analysis_row) -> AnalysisResponse:
    """Map DB rows onto the public analysis contract."""
    if analysis_row is None:
        return AnalysisResponse(
            image_id=image.id,
            status=image.status,
            message=(
                "Analysis is not available yet."
                if image.status in ("pending", "processing")
                else "No analysis result was produced for this image."
            ),
        )

    payload = analysis_row.result or {}
    return AnalysisResponse(
        image_id=image.id,
        status=image.status,
        summary=AnalysisSummary(**payload["summary"])
        if payload.get("summary")
        else AnalysisSummary(
            overall_status=analysis_row.overall_status, confidence=analysis_row.confidence
        ),
        checks=[CheckResult(**c) for c in payload.get("checks", [])],
        vehicle_number=analysis_row.vehicle_number,
        analyzed_at=analysis_row.created_at,
    )
