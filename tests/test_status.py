"""Status / results / failure endpoints and the worker task's state transitions."""

from __future__ import annotations

import uuid

import pytest

from app.models.analysis_result import AnalysisResult
from app.models.enums import JobStatus, ProcessingStatus
from app.models.image import Image
from app.models.processing_job import ProcessingJob
from app.services import analysis_service, image_service
from tests.conftest import make_image_bytes


def _upload(client) -> str:
    files = {"file": ("v.jpg", make_image_bytes(), "image/jpeg")}
    return client.post("/api/v1/images/upload", files=files).json()["id"]


# ------------------------------------------------------------------- health
def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("ok", "degraded")
    assert body["dependencies"]["database"] == "ok"
    assert "version" in body


# ------------------------------------------------------------------- status
def test_status_pending_after_upload(client):
    image_id = _upload(client)
    body = client.get(f"/api/v1/images/{image_id}/status").json()
    assert body["id"] == image_id
    assert body["status"] == "pending"
    assert body["failure_reason"] is None


def test_status_unknown_id_is_404(client):
    response = client.get(f"/api/v1/images/{uuid.uuid4()}/status")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_status_malformed_id_is_422(client):
    assert client.get("/api/v1/images/not-a-uuid/status").status_code == 422


def test_status_reflects_processing_transition(client, db):
    image_id = uuid.UUID(_upload(client))
    job = db.query(ProcessingJob).filter_by(image_id=image_id).one()
    image_service.mark_processing(db, image_id, job.id)

    body = client.get(f"/api/v1/images/{image_id}/status").json()
    assert body["status"] == "processing"
    assert body["attempts"] == 1


def test_status_handles_unknown_stored_status(client, db):
    """A corrupt status value is surfaced honestly, not guessed."""
    image_id = uuid.UUID(_upload(client))
    db.get(Image, image_id).status = "weird_state"
    db.commit()

    body = client.get(f"/api/v1/images/{image_id}/status").json()
    assert body["status"] == "failed"
    assert "unknown processing status" in body["failure_reason"].lower()


# ------------------------------------------------------------------ results
def test_results_before_completion_reports_pending(client):
    image_id = _upload(client)
    body = client.get(f"/api/v1/images/{image_id}/results").json()
    assert body["status"] == "pending"
    assert body["checks"] == []
    assert "not available yet" in body["message"]


def test_results_after_successful_processing(client, db, tmp_path, monkeypatch):
    from app.services import ocr_service

    monkeypatch.setattr(
        ocr_service, "_run_tesseract", lambda _a: ("KA01AB1234", 0.88, [("KA01AB1234", 0.88)])
    )
    image_id = uuid.UUID(_upload(client))
    image = db.get(Image, image_id)
    job = db.query(ProcessingJob).filter_by(image_id=image_id).one()

    payload = analysis_service.analyze_image(db, image.storage_path, image_id=image_id)
    image_service.mark_completed(db, image_id, job.id, payload)

    body = client.get(f"/api/v1/images/{image_id}/results").json()
    assert body["status"] == "completed"
    assert body["summary"]["overall_status"] in ("pass", "warning", "fail", "uncertain")
    assert 0.0 <= body["summary"]["confidence"] <= 1.0
    assert {c["name"] for c in body["checks"]} >= {"blur", "brightness", "duplicate"}
    assert body["vehicle_number"] == "KA01AB1234"
    assert body["analyzed_at"]

    # Persistence: the image row is updated and the result stored exactly once.
    db.expire_all()
    assert db.get(Image, image_id).status == ProcessingStatus.COMPLETED
    assert db.get(Image, image_id).image_hash
    assert db.query(AnalysisResult).filter_by(image_id=image_id).count() == 1
    assert db.query(ProcessingJob).filter_by(image_id=image_id).one().status == JobStatus.COMPLETED


def test_reprocessing_replaces_the_previous_result(client, db, monkeypatch):
    from app.services import ocr_service

    monkeypatch.setattr(ocr_service, "_run_tesseract", lambda _a: ("", 0.0, []))
    image_id = uuid.UUID(_upload(client))
    image = db.get(Image, image_id)
    job = db.query(ProcessingJob).filter_by(image_id=image_id).one()

    for _ in range(2):
        payload = analysis_service.analyze_image(db, image.storage_path, image_id=image_id)
        image_service.mark_completed(db, image_id, job.id, payload)

    assert db.query(AnalysisResult).filter_by(image_id=image_id).count() == 1


# ------------------------------------------------------------------ failure
def test_failure_endpoint_when_nothing_failed(client):
    image_id = _upload(client)
    body = client.get(f"/api/v1/images/{image_id}/failure").json()
    assert body["failed"] is False
    assert body["reason"] is None


def test_failed_processing_records_useful_reason(client, db):
    image_id = uuid.UUID(_upload(client))
    job = db.query(ProcessingJob).filter_by(image_id=image_id).one()
    image_service.mark_processing(db, image_id, job.id)
    image_service.mark_failed(
        db, image_id, "Stored image could not be decoded.", "ProcessingError", job.id
    )

    status_body = client.get(f"/api/v1/images/{image_id}/status").json()
    assert status_body["status"] == "failed"
    assert status_body["failure_reason"] == "Stored image could not be decoded."

    failure = client.get(f"/api/v1/images/{image_id}/failure").json()
    assert failure["failed"] is True
    assert failure["error_type"] == "ProcessingError"
    assert failure["attempts"] == 1
    assert failure["retryable"] is True
    # No stack trace or filesystem path leaks to the client.
    assert "Traceback" not in failure["reason"] and "/srv" not in failure["reason"]


def test_retrying_keeps_image_out_of_failed_state(client, db):
    image_id = uuid.UUID(_upload(client))
    job = db.query(ProcessingJob).filter_by(image_id=image_id).one()
    image_service.mark_processing(db, image_id, job.id)
    image_service.mark_failed(db, image_id, "Transient blip", "TransientProcessingError", job.id, retrying=True)

    db.expire_all()
    assert db.get(Image, image_id).status == ProcessingStatus.PROCESSING
    assert db.query(ProcessingJob).filter_by(id=job.id).one().status == JobStatus.RETRYING


def test_failure_unknown_id_is_404(client):
    assert client.get(f"/api/v1/images/{uuid.uuid4()}/failure").status_code == 404


# ------------------------------------------------------------------- worker
def test_worker_task_marks_completed(client, db, monkeypatch):
    """End-to-end task body with the real DB and a mocked OCR engine."""
    from app.services import ocr_service
    from app.workers import image_tasks

    monkeypatch.setattr(ocr_service, "_run_tesseract", lambda _a: ("", 0.0, []))
    image_id = _upload(client)
    job = db.query(ProcessingJob).filter_by(image_id=uuid.UUID(image_id)).one()

    result = image_tasks.process_image_task.run(image_id, str(job.id))

    assert result["status"] == ProcessingStatus.COMPLETED
    db.expire_all()
    assert db.get(Image, uuid.UUID(image_id)).status == ProcessingStatus.COMPLETED


def test_worker_task_records_failure_for_missing_file(client, db, monkeypatch):
    from app.workers import image_tasks

    image_id = _upload(client)
    image = db.get(Image, uuid.UUID(image_id))
    job = db.query(ProcessingJob).filter_by(image_id=image.id).one()
    image.storage_path = "/nonexistent/gone.jpg"
    db.commit()

    # A permanent error must not consume retries.
    monkeypatch.setattr(image_tasks.settings, "task_max_retries", 0)
    result = image_tasks.process_image_task.run(image_id, str(job.id))

    assert result["status"] == ProcessingStatus.FAILED
    db.expire_all()
    refreshed = db.get(Image, uuid.UUID(image_id))
    assert refreshed.status == ProcessingStatus.FAILED
    assert refreshed.failure_reason
    assert db.query(ProcessingJob).filter_by(id=job.id).one().error_type == "ProcessingError"


def test_worker_task_is_idempotent_for_completed_images(client, db, monkeypatch):
    from app.services import ocr_service
    from app.workers import image_tasks

    monkeypatch.setattr(ocr_service, "_run_tesseract", lambda _a: ("", 0.0, []))
    image_id = _upload(client)
    job = db.query(ProcessingJob).filter_by(image_id=uuid.UUID(image_id)).one()

    image_tasks.process_image_task.run(image_id, str(job.id))
    second = image_tasks.process_image_task.run(image_id, str(job.id))
    assert second.get("skipped") is True


def test_worker_ignores_malformed_arguments():
    from celery.exceptions import Ignore

    from app.workers import image_tasks

    with pytest.raises(Ignore):
        image_tasks.process_image_task.run("not-a-uuid", "also-bad")


def test_enqueue_failure_marks_image_failed(db, monkeypatch):
    """If Redis is down the client gets 503 and the row does not sit at pending."""
    from app.core.exceptions import QueueError
    from app.workers import image_tasks

    image, job = image_service.create_image_record(
        db, make_image_bytes(), "image/jpeg", "v.jpg"
    )
    monkeypatch.setattr(
        image_tasks.process_image_task,
        "delay",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("redis down")),
    )
    with pytest.raises(QueueError):
        image_service.enqueue_processing(db, image, job)

    db.expire_all()
    assert db.get(Image, image.id).status == ProcessingStatus.FAILED
