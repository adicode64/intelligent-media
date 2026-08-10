"""Shared enumerations. Stored as short strings for readability in SQL."""

from __future__ import annotations

from enum import StrEnum


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# Jobs share the image lifecycle vocabulary plus an explicit retry state.
class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"


class CheckStatus(StrEnum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"
    UNCERTAIN = "uncertain"
    SKIPPED = "skipped"
    ERROR = "error"
