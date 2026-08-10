"""
Image intake and lifecycle persistence.

The upload path is intentionally strict and ordered:
  size guard -> magic-byte sniff -> Pillow decode -> DB row -> disk write ->
  enqueue. The DB row is created first so an orphan file can never exist without
  a record; if the enqueue fails we mark the row `failed` with a clear reason
  instead of leaving a job stuck at `pending` forever.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.core.exceptions import (
    DatabaseError,
    NotFoundError,
    PayloadTooLargeError,
    QueueError,
    StorageError,
    UnsupportedMediaTypeError,
)
from app.core.logging import get_logger
from app.models.analysis_result import AnalysisResult
from app.models.enums import JobStatus, ProcessingStatus
from app.models.image import Image
from app.models.processing_job import ProcessingJob
from app.utils.image_utils import (
    build_stored_filename,
    inspect_image_bytes,
    resolve_storage_path,
    sanitize_filename,
    sniff_format,
)

logger = get_logger(__name__)


def validate_upload(
    data: bytes, declared_content_type: str | None, filename: str | None
) -> dict:
    """Validate size and true image content. Raises a typed AppError on failure."""
    size = len(data)
    if size == 0:
        raise UnsupportedMediaTypeError("Uploaded file is empty.")
    if size > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            f"File is {round(size / 1_048_576, 2)} MB; the limit is "
            f"{round(settings.max_upload_bytes / 1_048_576, 2)} MB."
        )

    if declared_content_type and declared_content_type not in settings.allowed_content_types:
        raise UnsupportedMediaTypeError(
            f"Content type '{declared_content_type}' is not allowed. Allowed: "
            f"{', '.join(settings.allowed_content_types)}."
        )

    # Content, not extension, is authoritative.
    if sniff_format(data) is None:
        raise UnsupportedMediaTypeError(
            "File content does not match any supported image format "
            "(JPEG, PNG, WEBP, BMP)."
        )

    info = inspect_image_bytes(data)  # raises CorruptImageError
    if info["format"] not in settings.allowed_image_formats:
        raise UnsupportedMediaTypeError(
            f"Decoded image format '{info['format']}' is not supported."
        )

    info["file_size"] = size
    info["safe_filename"] = sanitize_filename(filename)
    return info


def create_image_record(
    db: Session, data: bytes, declared_content_type: str | None, filename: str | None
) -> tuple[Image, ProcessingJob]:
    """Persist metadata + file and create a pending job (not yet enqueued)."""
    info = validate_upload(data, declared_content_type, filename)

    image_id = uuid.uuid4()
    stored_filename = build_stored_filename(image_id, info["format"])
    target = resolve_storage_path(stored_filename)

    image = Image(
        id=image_id,
        original_filename=info["safe_filename"],
        stored_filename=stored_filename,
        storage_path=str(target),
        content_type=declared_content_type or f"image/{info['format'].lower()}",
        file_size=info["file_size"],
        width=info["width"],
        height=info["height"],
        status=ProcessingStatus.PENDING,
    )
    job = ProcessingJob(image_id=image_id, status=JobStatus.PENDING, attempts=0)

    try:
        db.add(image)
        db.add(job)
        db.flush()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("image_record_insert_failed", extra={"error": str(exc)})
        raise DatabaseError("Could not record the upload.") from exc

    try:
        # 'xb' refuses to clobber an existing file; the name is a fresh UUID.
        with open(target, "xb") as fh:
            fh.write(data)
    except OSError as exc:
        db.rollback()
        logger.error("image_write_failed", extra={"error": str(exc), "path": str(target)})
        raise StorageError("Could not persist the uploaded image.") from exc

    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        target.unlink(missing_ok=True)  # no orphan file without a row
        raise DatabaseError("Could not commit the upload.") from exc

    db.refresh(image)
    db.refresh(job)
    logger.info(
        "image_uploaded",
        extra={"image_id": str(image.id), "size": image.file_size, "job_id": str(job.id)},
    )
    return image, job


def enqueue_processing(db: Session, image: Image, job: ProcessingJob) -> str | None:
    """Publish the task to Redis. Marks the row failed if the broker is down."""
    from app.workers.image_tasks import process_image_task

    try:
        async_result = process_image_task.delay(str(image.id), str(job.id))
    except Exception as exc:
        logger.error("enqueue_failed", extra={"image_id": str(image.id), "error": str(exc)})
        mark_failed(db, image.id, "Processing could not be queued.", type(exc).__name__)
        raise QueueError("Processing queue is unavailable; please retry.") from exc

    task_id = getattr(async_result, "id", None)
    if task_id:
        job.celery_task_id = str(task_id)
        db.commit()
    return task_id


# ------------------------------------------------------------------- lookups
def get_image(db: Session, image_id: uuid.UUID) -> Image:
    image = db.get(Image, image_id)
    if image is None:
        raise NotFoundError(f"No image found with id {image_id}.")
    return image


def get_image_with_relations(db: Session, image_id: uuid.UUID) -> Image:
    image = db.execute(
        select(Image)
        .options(selectinload(Image.jobs), selectinload(Image.analysis))
        .where(Image.id == image_id)
    ).scalar_one_or_none()
    if image is None:
        raise NotFoundError(f"No image found with id {image_id}.")
    return image


def latest_job(db: Session, image_id: uuid.UUID) -> ProcessingJob | None:
    return db.execute(
        select(ProcessingJob)
        .where(ProcessingJob.image_id == image_id)
        .order_by(ProcessingJob.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


# --------------------------------------------------------------- transitions
def mark_processing(db: Session, image_id: uuid.UUID, job_id: uuid.UUID | None) -> None:
    image = get_image(db, image_id)
    image.status = ProcessingStatus.PROCESSING
    image.failure_reason = None
    if job_id:
        job = db.get(ProcessingJob, job_id)
        if job:
            job.status = JobStatus.PROCESSING
            job.attempts += 1
            job.started_at = job.started_at or datetime.now(timezone.utc)
            job.error_message = None
    db.commit()


def mark_completed(
    db: Session, image_id: uuid.UUID, job_id: uuid.UUID | None, payload: dict
) -> AnalysisResult:
    image = get_image(db, image_id)

    existing = db.execute(
        select(AnalysisResult).where(AnalysisResult.image_id == image_id)
    ).scalar_one_or_none()
    if existing:
        db.delete(existing)
        db.flush()

    summary = payload.get("summary") or {}
    result = AnalysisResult(
        image_id=image_id,
        overall_status=summary.get("overall_status", "uncertain"),
        confidence=float(summary.get("confidence", 0.0)),
        vehicle_number=payload.get("vehicle_number"),
        result=payload,
    )
    db.add(result)

    image.status = ProcessingStatus.COMPLETED
    image.failure_reason = None
    image_hash = (payload.get("signals") or {}).get("image_hash")
    if image_hash:
        image.image_hash = image_hash

    if job_id:
        job = db.get(ProcessingJob, job_id)
        if job:
            job.status = JobStatus.COMPLETED
            job.completed_at = datetime.now(timezone.utc)
            job.error_message = None

    db.commit()
    db.refresh(result)
    return result


def mark_failed(
    db: Session,
    image_id: uuid.UUID,
    reason: str,
    error_type: str | None = None,
    job_id: uuid.UUID | None = None,
    retrying: bool = False,
) -> None:
    """Record a failure. Reasons are human-readable and free of stack traces."""
    try:
        image = db.get(Image, image_id)
        if image is None:
            return
        if not retrying:
            image.status = ProcessingStatus.FAILED
            image.failure_reason = reason

        job = db.get(ProcessingJob, job_id) if job_id else latest_job(db, image_id)
        if job:
            job.status = JobStatus.RETRYING if retrying else JobStatus.FAILED
            job.error_message = reason
            job.error_type = error_type
            if not retrying:
                job.completed_at = datetime.now(timezone.utc)
        db.commit()
    except SQLAlchemyError as exc:  # never let bookkeeping mask the real error
        db.rollback()
        logger.error("mark_failed_persist_error", extra={"error": str(exc)})
