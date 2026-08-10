"""
Analysis result contract.

Design note: every check returns the *same* shape, and every uncertain outcome is
expressed explicitly (`status="uncertain"` + a confidence below 1.0) instead of
being coerced into a pass/fail. Consumers can therefore treat this pipeline as a
set of advisory signals, not an oracle.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import CheckStatus, ProcessingStatus


class CheckResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    status: CheckStatus
    message: str
    score: float | None = None
    value: Any | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    heuristic: bool = Field(
        default=True,
        description="True when the verdict comes from a threshold heuristic rather "
        "than a deterministic measurement.",
    )
    details: dict[str, Any] = Field(default_factory=dict)


class OCRResult(BaseModel):
    raw_text: str = ""
    normalized_text: str | None = None
    confidence: float = 0.0
    engine: str = "none"
    candidates: list[str] = Field(default_factory=list)
    error: str | None = None


class PlateValidation(BaseModel):
    extracted_number: str | None = None
    validity: str = "uncertain"  # valid | invalid | uncertain
    reason: str = ""
    matched_format: str | None = None
    confidence: float = 0.0


class AnalysisSummary(BaseModel):
    overall_status: str  # pass | warning | fail | uncertain
    confidence: float = Field(ge=0.0, le=1.0)
    passed: int = 0
    warnings: int = 0
    failures: int = 0
    uncertain: int = 0
    notes: list[str] = Field(default_factory=list)


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    image_id: uuid.UUID
    status: ProcessingStatus
    summary: AnalysisSummary | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    vehicle_number: str | None = None
    analyzed_at: datetime | None = None
    message: str | None = None


class FailureResponse(BaseModel):
    image_id: uuid.UUID
    status: ProcessingStatus
    failed: bool
    reason: str | None = None
    error_type: str | None = None
    attempts: int = 0
    last_attempt_at: datetime | None = None
    retryable: bool = False
