"""
Indian registration-number validation (heuristic).

IMPORTANT: this is a *format* heuristic, not a legal validity check. It cannot
tell you whether a plate is registered, current, or genuine — only whether the
string looks like a plate under the common patterns below.

Supported families:
  1. Modern BH series           : 22 BH 1234 AA   -> ^\\d{2}BH\\d{4}[A-Z]{1,2}$
  2. Standard state series      : KA 01 AB 1234   -> ^[A-Z]{2}\\d{1,2}[A-Z]{1,3}\\d{1,4}$
  3. Older/short series         : KA 01 1234
  4. Military/defence           : 12A 123456 A    (loose match, flagged uncertain)

Known gaps: vanity plates, diplomatic (CD/UN) plates, temporary registrations,
two-line plates OCR'd out of order, and newly created RTO codes. Anything that
does not match a known family returns `invalid` with a reason rather than a
guess, and short/ambiguous strings return `uncertain`.
"""

from __future__ import annotations

import re

from app.models.enums import CheckStatus
from app.schemas.analysis import CheckResult, PlateValidation

# Union-territory + state codes valid at the time of writing.
VALID_STATE_CODES: frozenset[str] = frozenset(
    {
        "AN", "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "DN", "GA", "GJ",
        "HP", "HR", "JH", "JK", "KA", "KL", "LA", "LD", "MH", "ML", "MN", "MP",
        "MZ", "NL", "OD", "OR", "PB", "PY", "RJ", "SK", "TN", "TR", "TS", "UK",
        "UA", "UP", "WB",
    }
)

PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        "bh_series",
        r"^(?P<year>\d{2})BH(?P<serial>\d{4})(?P<suffix>[A-Z]{1,2})$",
        "Bharat (BH) series: YY BH NNNN XX",
    ),
    (
        "standard",
        r"^(?P<state>[A-Z]{2})(?P<rto>\d{1,2})(?P<series>[A-Z]{1,3})(?P<number>\d{1,4})$",
        "State series: SS RR L(LL) NNNN",
    ),
    (
        "short_legacy",
        r"^(?P<state>[A-Z]{2})(?P<rto>\d{1,2})(?P<number>\d{1,4})$",
        "Legacy series without a letter block: SS RR NNNN",
    ),
    (
        "defence",
        r"^\d{2}[A-Z]\d{6}[A-Z]?$",
        "Defence/military format (loose match)",
    ),
)

_STRIP_RE = re.compile(r"[^A-Z0-9]")


def normalize_plate(text: str | None) -> str:
    """Uppercase and drop every non-alphanumeric character."""
    if not text:
        return ""
    return _STRIP_RE.sub("", text.upper())


def validate_plate(candidate: str | None, ocr_confidence: float = 1.0) -> PlateValidation:
    normalized = normalize_plate(candidate)

    if not normalized:
        return PlateValidation(
            extracted_number=None,
            validity="uncertain",
            reason="No candidate registration number was extracted.",
            confidence=0.0,
        )

    if len(normalized) < 6:
        return PlateValidation(
            extracted_number=normalized,
            validity="uncertain",
            reason=(
                f"Candidate '{normalized}' is too short ({len(normalized)} chars) to "
                "match any known Indian plate format; likely a partial OCR read."
            ),
            confidence=round(min(ocr_confidence, 0.3), 3),
        )

    if len(normalized) > 12:
        return PlateValidation(
            extracted_number=normalized,
            validity="invalid",
            reason=(
                f"Candidate '{normalized}' is longer than any valid Indian plate "
                "(max 12 characters); OCR likely merged surrounding text."
            ),
            confidence=round(min(ocr_confidence, 0.4), 3),
        )

    for name, pattern, description in PATTERNS:
        match = re.match(pattern, normalized)
        if not match:
            continue

        groups = match.groupdict()
        state = groups.get("state")

        if state and state not in VALID_STATE_CODES:
            return PlateValidation(
                extracted_number=normalized,
                validity="uncertain",
                reason=(
                    f"Format matches '{description}' but '{state}' is not a recognised "
                    "state/UT code. It may be a new RTO code or an OCR error "
                    "(e.g. O/0 or I/1 confusion)."
                ),
                matched_format=name,
                confidence=round(min(ocr_confidence, 0.5), 3),
            )

        if name == "defence":
            return PlateValidation(
                extracted_number=normalized,
                validity="uncertain",
                reason=(
                    "Matches a defence-style pattern, which overlaps with common OCR "
                    "noise. Treated as uncertain by design."
                ),
                matched_format=name,
                confidence=round(min(ocr_confidence, 0.45), 3),
            )

        # Format-valid. Confidence is bounded by the OCR read that produced it.
        return PlateValidation(
            extracted_number=normalized,
            validity="valid",
            reason=(
                f"Matches the expected Indian format '{description}'. This is a format "
                "heuristic only and does not confirm legal registration."
            ),
            matched_format=name,
            confidence=round(min(0.95, max(ocr_confidence, 0.5)), 3),
        )

    return PlateValidation(
        extracted_number=normalized,
        validity="invalid",
        reason=(
            f"Candidate '{normalized}' does not match any supported Indian "
            "registration format (BH series, state series, or legacy series)."
        ),
        confidence=round(min(ocr_confidence, 0.4), 3),
    )


def build_vehicle_number_check(
    validation: PlateValidation, ocr_confidence: float, ocr_error: str | None = None
) -> CheckResult:
    """Convert a PlateValidation into the uniform CheckResult envelope."""
    if ocr_error:
        return CheckResult(
            name="vehicle_number",
            status=CheckStatus.UNCERTAIN,
            message=f"Vehicle number could not be determined: {ocr_error}",
            confidence=0.0,
            details={"validity": "uncertain", "ocr_error": ocr_error},
        )

    status_map = {
        "valid": CheckStatus.PASS,
        "invalid": CheckStatus.FAIL,
        "uncertain": CheckStatus.UNCERTAIN,
    }
    status = status_map.get(validation.validity, CheckStatus.UNCERTAIN)

    # A format match built on a weak OCR read must not be reported as a pass.
    if status is CheckStatus.PASS and ocr_confidence < 0.5:
        status = CheckStatus.UNCERTAIN

    return CheckResult(
        name="vehicle_number",
        status=status,
        value=validation.extracted_number,
        message=validation.reason,
        confidence=validation.confidence,
        details={
            "validity": validation.validity,
            "matched_format": validation.matched_format,
            "ocr_confidence": round(ocr_confidence, 3),
            "disclaimer": "Regex/format heuristic; does not guarantee legal validity.",
        },
    )
