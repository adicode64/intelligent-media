"""Upload endpoint: happy path, validation failures, and security behaviour."""

from __future__ import annotations

import uuid

import pytest

from app.core.exceptions import PayloadTooLargeError, UnsupportedMediaTypeError
from app.models.enums import JobStatus, ProcessingStatus
from app.models.image import Image
from app.models.processing_job import ProcessingJob
from app.services import image_service
from tests.conftest import make_image_bytes


def test_upload_returns_pending_immediately(client, upload_file_tuple, enqueued):
    response = client.post("/api/v1/images/upload", files=upload_file_tuple())

    assert response.status_code == 202
    body = response.json()
    uuid.UUID(body["id"])  # valid UUID
    assert body["status"] == "pending"
    assert body["message"] == "Image uploaded successfully"
    # The API must not block on analysis; it only publishes the job.
    assert len(enqueued) == 1


def test_upload_persists_metadata_and_job(client, upload_file_tuple, db):
    image_id = client.post("/api/v1/images/upload", files=upload_file_tuple()).json()["id"]

    image = db.get(Image, uuid.UUID(image_id))
    assert image is not None
    assert image.width == 800 and image.height == 600
    assert image.file_size > 0
    assert image.status == ProcessingStatus.PENDING
    assert image.stored_filename.startswith(image_id)  # UUID-based storage name

    job = db.query(ProcessingJob).filter_by(image_id=uuid.UUID(image_id)).one()
    assert job.status == JobStatus.PENDING and job.attempts == 0


def test_uploaded_file_is_written_to_disk(client, upload_file_tuple, db):
    from pathlib import Path

    image_id = client.post("/api/v1/images/upload", files=upload_file_tuple()).json()["id"]
    image = db.get(Image, uuid.UUID(image_id))
    assert Path(image.storage_path).is_file()


def test_reject_non_image_content(client):
    files = {"file": ("payload.jpg", b"#!/bin/sh\necho hacked\n", "image/jpeg")}
    response = client.post("/api/v1/images/upload", files=files)
    # Extension and content type both claim JPEG; the bytes do not.
    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_media_type"


def test_reject_disallowed_content_type(client):
    files = {"file": ("notes.pdf", b"%PDF-1.7 fake", "application/pdf")}
    assert client.post("/api/v1/images/upload", files=files).status_code == 415


def test_reject_truncated_image(client):
    """PNG has per-chunk CRCs, so truncation is reliably detectable."""
    full = make_image_bytes(fmt="PNG")
    data = full[: len(full) // 3]
    response = client.post("/api/v1/images/upload", files={"file": ("t.png", data, "image/png")})
    assert response.status_code in (415, 422)


def test_reject_empty_file(client):
    response = client.post("/api/v1/images/upload", files={"file": ("e.jpg", b"", "image/jpeg")})
    assert response.status_code == 415


def test_reject_oversized_file(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_upload_bytes", 1024)
    response = client.post(
        "/api/v1/images/upload", files={"file": ("big.jpg", make_image_bytes(), "image/jpeg")}
    )
    assert response.status_code == 413
    assert "limit" in response.json()["detail"].lower()


def test_missing_file_field_is_422(client):
    assert client.post("/api/v1/images/upload").status_code == 422


def test_path_traversal_filename_is_neutralised(client, upload_file_tuple, db):
    files = {"file": ("../../../../etc/passwd.jpg", make_image_bytes(), "image/jpeg")}
    image_id = client.post("/api/v1/images/upload", files=files).json()["id"]

    image = db.get(Image, uuid.UUID(image_id))
    assert ".." not in image.original_filename
    assert "/" not in image.original_filename
    # Storage path stays inside the storage root and uses the UUID name.
    assert image.stored_filename == f"{image_id}.jpg"


def test_validate_upload_raises_typed_errors():
    with pytest.raises(UnsupportedMediaTypeError):
        image_service.validate_upload(b"not-an-image", "image/jpeg", "a.jpg")
    info = image_service.validate_upload(make_image_bytes(), "image/jpeg", "a.jpg")
    assert info["width"] == 800 and info["format"] == "JPEG"


def test_validate_upload_enforces_size_limit(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_upload_bytes", 512)
    with pytest.raises(PayloadTooLargeError):
        image_service.validate_upload(make_image_bytes(), "image/jpeg", "a.jpg")
