"""
OCR / vehicle-number extraction.

Engine: Tesseract via pytesseract, chosen over EasyOCR because it needs no model
download at build time and runs on CPU in a small container. The engine is
resolved lazily and any absence degrades to an explicit `uncertain` result — the
pipeline never fabricates a number, and the test-suite never needs the binary.

Pre-processing is deliberately simple (grayscale -> upscale -> CLAHE ->
Otsu threshold). Plate *localisation* is not attempted; we OCR the whole frame
and pick the best plate-shaped candidate.
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
from PIL import Image as PILImage

from app.config import settings
from app.core.logging import get_logger
from app.schemas.analysis import OCRResult
from app.services.validation_service import normalize_plate, validate_plate

logger = get_logger(__name__)

# Common OCR confusions, applied only in plate context where the position of a
# character tells us whether a letter or a digit is expected.
LETTER_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"}
DIGIT_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "8": "B"}

_PLATE_LIKE_RE = re.compile(r"[A-Z0-9]{6,12}")


class OCRUnavailableError(RuntimeError):
    """Raised when no OCR engine can be loaded."""


def _preprocess(image: PILImage.Image) -> np.ndarray:
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    try:
        import cv2

        h, w = gray.shape[:2]
        # Upscale small images: Tesseract needs ~30px character height.
        if max(h, w) < 1000:
            scale = min(2.5, 1000 / max(h, w, 1))
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        gray = cv2.bilateralFilter(gray, 7, 55, 55)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return binary
    except Exception as exc:  # pragma: no cover - OpenCV should be present
        logger.debug("ocr_preprocess_fallback", extra={"error": str(exc)})
        return gray


def _run_tesseract(array: np.ndarray) -> tuple[str, float, list[tuple[str, float]]]:
    import pytesseract
    from pytesseract import Output

    config = "--oem 3 --psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    data = pytesseract.image_to_data(
        PILImage.fromarray(array),
        lang=settings.ocr_languages,
        config=config,
        output_type=Output.DICT,
        timeout=settings.ocr_timeout_seconds,
    )

    words: list[tuple[str, float]] = []
    for text, conf in zip(data.get("text", []), data.get("conf", [])):
        token = (text or "").strip()
        try:
            confidence = float(conf)
        except (TypeError, ValueError):
            confidence = -1.0
        if token and confidence >= 0:
            words.append((token, confidence / 100.0))

    raw_text = " ".join(t for t, _ in words)
    mean_conf = sum(c for _, c in words) / len(words) if words else 0.0
    return raw_text, mean_conf, words


def apply_ocr_corrections(candidate: str) -> str:
    """Positional character repair for the standard `SS RR LLL NNNN` layout.

    Only applied where the expected character class is unambiguous, so we do not
    invent structure that OCR never saw.
    """
    text = normalize_plate(candidate)
    if not 8 <= len(text) <= 10:
        return text

    chars = list(text)
    # Positions 0-1 must be letters (state code).
    for i in (0, 1):
        if chars[i].isdigit():
            chars[i] = DIGIT_TO_LETTER.get(chars[i], chars[i])
    # Positions 2-3 must be digits (RTO code).
    for i in (2, 3):
        if chars[i].isalpha():
            chars[i] = LETTER_TO_DIGIT.get(chars[i], chars[i])
    # Trailing 4 characters are the serial number: digits.
    for i in range(len(chars) - 4, len(chars)):
        if chars[i].isalpha():
            chars[i] = LETTER_TO_DIGIT.get(chars[i], chars[i])
    return "".join(chars)


def _rank_candidates(raw_text: str, words: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Collect plate-shaped substrings, scored by OCR confidence + format match."""
    seen: dict[str, float] = {}

    def consider(token: str, confidence: float) -> None:
        for match in _PLATE_LIKE_RE.findall(normalize_plate(token)):
            for variant in (match, apply_ocr_corrections(match)):
                if not variant:
                    continue
                verdict = validate_plate(variant, confidence)
                bonus = {"valid": 0.35, "uncertain": 0.1, "invalid": 0.0}[verdict.validity]
                score = min(confidence + bonus, 1.0)
                if score > seen.get(variant, -1.0):
                    seen[variant] = score

    for token, confidence in words:
        consider(token, confidence)
    # Adjacent words: plates are frequently split across OCR tokens.
    for i in range(len(words) - 1):
        merged = words[i][0] + words[i + 1][0]
        consider(merged, min(words[i][1], words[i + 1][1]))
    consider(raw_text.replace(" ", ""), max((c for _, c in words), default=0.0) * 0.8)

    return sorted(seen.items(), key=lambda kv: kv[1], reverse=True)


def extract_vehicle_number(image: PILImage.Image) -> OCRResult:
    """Best-effort plate extraction. Returns an explicit low-confidence result
    rather than raising, so a missing OCR engine never fails the whole pipeline.
    """
    if not settings.ocr_enabled:
        return OCRResult(engine="disabled", error="OCR is disabled by configuration.")

    try:
        array = _preprocess(image)
    except Exception as exc:
        logger.warning("ocr_preprocess_failed", extra={"error": str(exc)})
        return OCRResult(engine="none", error="Image could not be prepared for OCR.")

    try:
        raw_text, mean_conf, words = _run_tesseract(array)
        engine = "tesseract"
    except ImportError:
        return OCRResult(
            engine="unavailable",
            error="No OCR engine is installed; vehicle number could not be read.",
        )
    except Exception as exc:
        # Missing binary, timeout, or a Tesseract crash all land here.
        logger.warning(
    "ocr_failed", extra={"error": str(exc), "type": type(exc).__name__}, exc_info=True
)
        return OCRResult(
            engine="tesseract",
            error=f"OCR engine failed: {type(exc).__name__}",
        )

    if not raw_text.strip():
        return OCRResult(
            raw_text="",
            engine=engine,
            confidence=0.0,
            error="OCR produced no readable text.",
        )

    ranked = _rank_candidates(raw_text, words)
    if not ranked:
        return OCRResult(
            raw_text=raw_text[:500],
            engine=engine,
            confidence=round(mean_conf, 3),
            error="OCR text contained no plate-shaped candidate.",
        )

    best, best_score = ranked[0]
    # Report the raw OCR confidence, not the format-bonus-inflated ranking score.
    reported = round(min(mean_conf if mean_conf > 0 else best_score, best_score), 3)
    return OCRResult(
        raw_text=raw_text[:500],
        normalized_text=best,
        confidence=reported,
        engine=engine,
        candidates=[c for c, _ in ranked[:5]],
        error=None
        if reported >= settings.ocr_min_confidence
        else "OCR confidence is below the configured threshold; treat as uncertain.",
    )


def ocr_result_to_dict(result: OCRResult) -> dict[str, Any]:
    return result.model_dump()
