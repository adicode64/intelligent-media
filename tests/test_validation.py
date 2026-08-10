"""Indian registration-number validation and OCR normalisation."""

from __future__ import annotations

import pytest

from app.models.enums import CheckStatus
from app.services.ocr_service import apply_ocr_corrections
from app.services.validation_service import (
    build_vehicle_number_check,
    normalize_plate,
    validate_plate,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ka 01 ab 1234", "KA01AB1234"),
        ("KA-01-AB-1234", "KA01AB1234"),
        ("  ka01ab1234  ", "KA01AB1234"),
        ("KA01#AB@1234", "KA01AB1234"),
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_plate(raw, expected):
    assert normalize_plate(raw) == expected


@pytest.mark.parametrize(
    "plate,fmt",
    [
        ("KA01AB1234", "standard"),
        ("MH12DE1433", "standard"),
        ("DL8CAF5030", "standard"),
        ("TS09EA1234", "standard"),
        ("KA011234", "short_legacy"),
        ("22BH1234AA", "bh_series"),
    ],
)
def test_valid_formats(plate, fmt):
    result = validate_plate(plate, ocr_confidence=0.9)
    assert result.validity == "valid"
    assert result.matched_format == fmt
    assert "heuristic" in result.reason.lower()


def test_unknown_state_code_is_uncertain_not_invalid():
    result = validate_plate("ZZ01AB1234", ocr_confidence=0.9)
    assert result.validity == "uncertain"
    assert "state" in result.reason.lower()


@pytest.mark.parametrize("plate", ["ABCDEFGH", "1234567890", "KA01AB12345678"])
def test_invalid_formats(plate):
    assert validate_plate(plate).validity == "invalid"


def test_empty_candidate_is_uncertain():
    result = validate_plate(None)
    assert result.validity == "uncertain"
    assert result.confidence == 0.0


def test_short_candidate_is_uncertain_not_invalid():
    result = validate_plate("KA01")
    assert result.validity == "uncertain"
    assert "too short" in result.reason.lower()


def test_confidence_is_bounded_by_ocr_confidence():
    weak = validate_plate("KA01AB1234", ocr_confidence=0.2)
    strong = validate_plate("KA01AB1234", ocr_confidence=0.95)
    assert weak.confidence <= strong.confidence
    assert strong.confidence <= 0.95


def test_valid_format_with_weak_ocr_is_downgraded_to_uncertain():
    validation = validate_plate("KA01AB1234", ocr_confidence=0.2)
    check = build_vehicle_number_check(validation, ocr_confidence=0.2)
    assert check.status is CheckStatus.UNCERTAIN


def test_check_reports_pass_for_confident_valid_plate():
    validation = validate_plate("KA01AB1234", ocr_confidence=0.9)
    check = build_vehicle_number_check(validation, ocr_confidence=0.9)
    assert check.status is CheckStatus.PASS
    assert check.value == "KA01AB1234"
    assert "does not guarantee" in check.details["disclaimer"].lower()


def test_check_reports_uncertain_on_ocr_error():
    validation = validate_plate(None)
    check = build_vehicle_number_check(validation, 0.0, ocr_error="OCR engine failed")
    assert check.status is CheckStatus.UNCERTAIN
    assert check.confidence == 0.0
    assert check.value is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("KAO1AB1Z34", "KA01AB1234"),  # O->0 in RTO block, Z->2 in serial
        ("K4O1AB1234", "KA01AB1234"),  # 4->A in state block
        ("KA01AB1234", "KA01AB1234"),  # already clean
    ],
)
def test_ocr_positional_corrections(raw, expected):
    assert apply_ocr_corrections(raw) == expected


def test_ocr_corrections_leave_unusual_lengths_alone():
    assert apply_ocr_corrections("KA01") == "KA01"
