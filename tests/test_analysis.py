"""Analysis checks: blur, brightness, dimensions, duplicates, OCR failure, summary."""

from __future__ import annotations

import io
import uuid

import numpy as np
import pytest
from PIL import Image as PILImage

from app.config import settings
from app.models.enums import CheckStatus
from app.models.image import Image
from app.schemas.analysis import CheckResult, OCRResult
from app.services import analysis_service, duplicate_service, ocr_service, quality_service
from tests.conftest import make_image_bytes


def _gray(kind: str, **kwargs) -> np.ndarray:
    data = make_image_bytes(kind=kind, **kwargs)
    with PILImage.open(io.BytesIO(data)) as img:
        return np.asarray(img.convert("L"), dtype=np.uint8)


# ----------------------------------------------------------------------- blur
def test_blur_detects_sharp_image():
    result = quality_service.check_blur(_gray("sharp"))
    assert result.status is CheckStatus.PASS
    assert result.score > settings.blur_warn_threshold
    assert result.details["metric"] == "laplacian_variance"


def test_blur_detects_blurry_image():
    result = quality_service.check_blur(_gray("blurry"))
    assert result.status is CheckStatus.FAIL
    assert result.score < settings.blur_fail_threshold


def test_blur_threshold_is_configurable(monkeypatch):
    sharp = _gray("sharp")
    monkeypatch.setattr(settings, "blur_fail_threshold", 10_000_000)
    monkeypatch.setattr(settings, "blur_warn_threshold", 20_000_000)
    assert quality_service.check_blur(sharp).status is CheckStatus.FAIL


def test_blur_handles_bad_input_gracefully():
    result = quality_service.check_blur(np.array([]))
    assert result.status in (CheckStatus.ERROR, CheckStatus.FAIL)


# ----------------------------------------------------------------- brightness
def test_brightness_flags_dark_image():
    result = quality_service.check_brightness(_gray("dark"))
    assert result.status is CheckStatus.FAIL
    assert result.value == "too_dark"
    assert result.score < settings.brightness_dark_threshold


def test_brightness_flags_bright_image():
    result = quality_service.check_brightness(_gray("bright"))
    assert result.value == "too_bright"
    assert result.status is CheckStatus.FAIL


def test_brightness_accepts_midtone_image():
    result = quality_service.check_brightness(_gray("flat"))
    assert result.value == "acceptable"
    assert result.status is CheckStatus.PASS
    assert "std_dev" in result.details


# ----------------------------------------------------------------- dimensions
def test_dimensions_pass_for_good_resolution():
    result = quality_service.check_dimensions(1920, 1080)
    assert result.status is CheckStatus.PASS
    assert result.details["aspect_ratio"] == pytest.approx(1.778, abs=0.01)
    assert result.heuristic is False


def test_dimensions_fail_below_minimum():
    result = quality_service.check_dimensions(120, 90)
    assert result.status is CheckStatus.FAIL
    assert result.details["warnings"]


def test_dimensions_warn_on_low_but_valid_resolution():
    result = quality_service.check_dimensions(400, 400)
    assert result.status is CheckStatus.WARNING


def test_dimensions_warn_on_extreme_aspect_ratio():
    result = quality_service.check_dimensions(4000, 400)
    assert result.status in (CheckStatus.WARNING, CheckStatus.FAIL)
    assert any("aspect" in w.lower() for w in result.details["warnings"])


# ------------------------------------------------------------------ duplicate
def _store(db, data: bytes, image_id: uuid.UUID | None = None) -> Image:
    image_id = image_id or uuid.uuid4()
    with PILImage.open(io.BytesIO(data)) as img:
        phash = duplicate_service.compute_phash(img)
        width, height = img.size
    row = Image(
        id=image_id,
        original_filename="x.jpg",
        stored_filename=f"{image_id}.jpg",
        storage_path=f"/tmp/{image_id}.jpg",
        content_type="image/jpeg",
        file_size=len(data),
        width=width,
        height=height,
        image_hash=phash,
        status="completed",
    )
    db.add(row)
    db.commit()
    return row


def test_duplicate_detects_identical_image(db):
    data = make_image_bytes(kind="blurry", seed=3)
    original = _store(db, data)
    result = duplicate_service.check_duplicate(db, original.image_hash, image_id=uuid.uuid4())

    assert result.status is CheckStatus.FAIL
    assert result.value == "duplicate"
    assert result.details["matched_image_id"] == str(original.id)
    assert result.details["hamming_distance"] <= settings.duplicate_exact_distance


def test_duplicate_does_not_flag_unrelated_image(db):
    _store(db, make_image_bytes(kind="dark"))
    with PILImage.open(io.BytesIO(make_image_bytes(kind="sharp"))) as img:
        other_hash = duplicate_service.compute_phash(img)

    result = duplicate_service.check_duplicate(db, other_hash)
    assert result.value == "not_duplicate"
    assert result.status is CheckStatus.PASS


def test_duplicate_similar_band_is_warning_not_duplicate(db, monkeypatch):
    """A near-match must not be reported as an exact duplicate."""
    original = _store(db, make_image_bytes(kind="blurry", seed=11))
    # Force the nearest-neighbour distance into the "similar" band.
    monkeypatch.setattr(
        duplicate_service, "find_nearest", lambda *a, **k: (original, 5)
    )
    result = duplicate_service.check_duplicate(db, original.image_hash)
    assert result.status is CheckStatus.WARNING
    assert result.value == "similar"


def test_duplicate_excludes_the_image_itself(db):
    original = _store(db, make_image_bytes(kind="blurry", seed=5))
    result = duplicate_service.check_duplicate(db, original.image_hash, image_id=original.id)
    assert result.value == "not_duplicate"


def test_duplicate_skipped_without_hash(db):
    result = duplicate_service.check_duplicate(db, None)
    assert result.status is CheckStatus.SKIPPED


def test_duplicate_survives_db_error(db, monkeypatch):
    monkeypatch.setattr(
        duplicate_service, "find_nearest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db"))
    )
    result = duplicate_service.check_duplicate(db, "0" * 16)
    assert result.status is CheckStatus.ERROR


# ------------------------------------------------------------------------ OCR
def test_ocr_engine_missing_returns_uncertain(monkeypatch):
    def raise_import(_array):
        raise ImportError("pytesseract missing")

    monkeypatch.setattr(ocr_service, "_run_tesseract", raise_import)
    with PILImage.open(io.BytesIO(make_image_bytes())) as img:
        result = ocr_service.extract_vehicle_number(img)

    assert result.engine == "unavailable"
    assert result.normalized_text is None
    assert result.confidence == 0.0
    assert "No OCR engine" in (result.error or "")


def test_ocr_engine_crash_is_contained(monkeypatch):
    def boom(_array):
        raise RuntimeError("tesseract crashed")

    monkeypatch.setattr(ocr_service, "_run_tesseract", boom)
    with PILImage.open(io.BytesIO(make_image_bytes())) as img:
        result = ocr_service.extract_vehicle_number(img)
    assert result.error and "RuntimeError" in result.error


def test_ocr_no_text_reports_zero_confidence(monkeypatch):
    monkeypatch.setattr(ocr_service, "_run_tesseract", lambda _a: ("", 0.0, []))
    with PILImage.open(io.BytesIO(make_image_bytes())) as img:
        result = ocr_service.extract_vehicle_number(img)
    assert result.confidence == 0.0 and result.normalized_text is None


def test_ocr_picks_plate_shaped_candidate(monkeypatch):
    words = [("KA01", 0.9), ("AB1234", 0.88), ("STICKER", 0.4)]
    monkeypatch.setattr(
        ocr_service, "_run_tesseract", lambda _a: ("KA01 AB1234 STICKER", 0.79, words)
    )
    with PILImage.open(io.BytesIO(make_image_bytes())) as img:
        result = ocr_service.extract_vehicle_number(img)

    assert result.normalized_text == "KA01AB1234"
    assert result.confidence > 0.5


def test_ocr_disabled_by_config(monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", False)
    with PILImage.open(io.BytesIO(make_image_bytes())) as img:
        assert ocr_service.extract_vehicle_number(img).engine == "disabled"


# -------------------------------------------------------- screenshot/metadata
def test_screenshot_heuristic_never_hard_fails():
    result = quality_service.check_screenshot_like(_gray("flat"), {})
    assert result.status is not CheckStatus.FAIL
    assert result.confidence <= 0.7
    assert "heuristic" in result.details["note"].lower()


def test_metadata_missing_exif_is_not_suspicious():
    with PILImage.open(io.BytesIO(make_image_bytes())) as img:
        result = quality_service.check_metadata(img)
    assert result.status in (CheckStatus.SKIPPED, CheckStatus.PASS)
    assert "not treated as suspicious" in result.message or result.details["exif_tag_count"] >= 0


# -------------------------------------------------------------- orchestration
def test_analyze_image_returns_all_checks(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr_service, "_run_tesseract", lambda _a: ("", 0.0, []))
    path = tmp_path / "v.jpg"
    path.write_bytes(make_image_bytes(kind="sharp"))

    payload = analysis_service.analyze_image(db, str(path))
    names = {c["name"] for c in payload["checks"]}
    assert names == {
        "blur",
        "brightness",
        "dimensions",
        "duplicate",
        "vehicle_number",
        "screenshot_heuristic",
        "metadata",
    }
    assert 0.0 <= payload["summary"]["confidence"] <= 1.0
    assert payload["signals"]["image_hash"]
    assert "blur_fail_threshold" in payload["config_snapshot"]


def test_analyze_image_missing_file_raises():
    from app.core.exceptions import ProcessingError

    with pytest.raises(ProcessingError):
        analysis_service.analyze_image(None, "/nonexistent/nope.jpg")


def test_summary_fail_wins_over_warning():
    checks = [
        CheckResult(name="blur", status=CheckStatus.FAIL, message="blurry", confidence=0.9),
        CheckResult(name="brightness", status=CheckStatus.WARNING, message="dark", confidence=0.7),
    ]
    summary = analysis_service.summarize(checks)
    assert summary.overall_status == "fail"
    assert summary.failures == 1 and summary.warnings == 1
    assert summary.notes


def test_summary_uncertain_when_only_uncertain_signals():
    checks = [
        CheckResult(name="blur", status=CheckStatus.PASS, message="ok", confidence=0.9),
        CheckResult(
            name="vehicle_number", status=CheckStatus.UNCERTAIN, message="?", confidence=0.2
        ),
    ]
    summary = analysis_service.summarize(checks)
    assert summary.overall_status == "uncertain"
    # Low-confidence signals must drag the aggregate confidence down.
    assert summary.confidence < 0.9


def test_summary_all_pass():
    checks = [
        CheckResult(name=n, status=CheckStatus.PASS, message="ok", confidence=0.9)
        for n in ("blur", "brightness", "dimensions")
    ]
    assert analysis_service.summarize(checks).overall_status == "pass"


def test_ocr_result_dict_is_json_safe():
    assert set(OCRResult().model_dump()) >= {"raw_text", "normalized_text", "confidence"}
