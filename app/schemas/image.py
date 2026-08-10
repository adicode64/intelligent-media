"""Request/response models for the image lifecycle endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ProcessingStatus


class UploadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: ProcessingStatus = ProcessingStatus.PENDING
    message: str = "Image uploaded successfully"


class StatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: ProcessingStatus
    attempts: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Populated only when status == failed, so clients get a hint without a
    # second round-trip to /failure.
    failure_reason: str | None = None


class ImageMetadata(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    content_type: str
    file_size: int
    width: int | None = None
    height: int | None = None
    image_hash: str | None = None
    status: ProcessingStatus
    created_at: datetime


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    environment: str
    dependencies: dict[str, str] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Client-safe error envelope. Never carries stack traces."""

    error: str
    detail: str
    request_id: str | None = None
