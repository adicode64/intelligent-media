"""
Celery task: run analysis for one uploaded image.

Retry policy: only `TransientProcessingError` and unexpected exceptions are
retried (bounded exponential backoff, `task_max_retries` attempts). Permanent
errors — corrupt file, missing file — fail fast, because retrying a deterministic
failure only wastes worker capacity and delays the client's answer.

Worker-crash behaviour: `task_acks_late` + `task_reject_on_worker_lost` means a
task killed mid-flight is redelivered to another worker. `recover_stale_jobs`
exists for the rarer case where the process died after the DB said `processing`
but before the broker redelivered.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from celery import Task
from celery.exceptions import Ignore, MaxRetriesExceededError, SoftTimeLimitExceeded

from app.config import settings
from app.core.celery_app import celery_app
from app.core.database import session_scope
from app.core.exceptions import NotFoundError, ProcessingError, TransientProcessingError
from app.core.logging import get_logger
from app.models.enums import JobStatus, ProcessingStatus
from app.services import analysis_service, image_service

logger = get_logger(__name__)


@celery_app.task(
    bind=True,
    name="app.workers.image_tasks.process_image_task",
    max_retries=settings.task_max_retries,
    acks_late=True,
)
def process_image_task(self: Task, image_id: str, job_id: str | None = None) -> dict:
    log = get_logger(__name__, image_id=image_id, job_id=job_id, attempt=self.request.retries + 1)
    try:
        image_uuid = uuid.UUID(image_id)
        job_uuid = uuid.UUID(job_id) if job_id else None
    except (TypeError, ValueError):
        logger.error("invalid_task_arguments", extra={"image_id": image_id, "job_id": job_id})
        raise Ignore()  # malformed message: never retry

    try:
        with session_scope() as db:
            image = image_service.get_image(db, image_uuid)
            if image.status == ProcessingStatus.COMPLETED:
                # Idempotency guard against broker redelivery.
                log.info("task_skipped_already_completed")
                return {"image_id": image_id, "status": image.status, "skipped": True}
            image_service.mark_processing(db, image_uuid, job_uuid)
            storage_path = image.storage_path

        log.info("analysis_started")
        with session_scope() as db:
            payload = analysis_service.analyze_image(db, storage_path, image_id=image_uuid)

        with session_scope() as db:
            result = image_service.mark_completed(db, image_uuid, job_uuid, payload)
            summary = payload.get("summary", {})
            log.info(
                "task_completed",
                extra={
                    "overall_status": result.overall_status,
                    "confidence": result.confidence,
                },
            )
            return {
                "image_id": image_id,
                "status": ProcessingStatus.COMPLETED,
                "overall_status": summary.get("overall_status"),
                "confidence": summary.get("confidence"),
            }

    except NotFoundError as exc:
        # The image row is gone; nothing to retry or record.
        log.error("task_image_missing", extra={"error": str(exc)})
        raise Ignore()

    except ProcessingError as exc:
        retryable = getattr(exc, "retryable", False)
        return _handle_failure(self, log, image_uuid, job_uuid, exc, retryable=retryable)

    except SoftTimeLimitExceeded as exc:
        return _handle_failure(
            self,
            log,
            image_uuid,
            job_uuid,
            exc,
            retryable=True,
            reason="Analysis exceeded the time limit.",
        )

    except Exception as exc:  # unknown errors are assumed transient once
        log.exception("task_unexpected_error")
        return _handle_failure(self, log, image_uuid, job_uuid, exc, retryable=True)


def _handle_failure(
    task: Task,
    log,
    image_uuid: uuid.UUID,
    job_uuid: uuid.UUID | None,
    exc: Exception,
    retryable: bool,
    reason: str | None = None,
) -> dict:
    """Persist the failure, then retry if the error class allows it."""
    message = reason or _public_reason(exc)
    attempts_used = task.request.retries + 1
    will_retry = retryable and attempts_used <= settings.task_max_retries

    try:
        with session_scope() as db:
            image_service.mark_failed(
                db,
                image_uuid,
                reason=message,
                error_type=type(exc).__name__,
                job_id=job_uuid,
                retrying=will_retry,
            )
    except Exception:  # pragma: no cover - DB down during failure bookkeeping
        log.exception("failure_persist_error")

    log.error(
        "task_failed",
        extra={
            "error_type": type(exc).__name__,
            "reason": message,
            "will_retry": will_retry,
            "attempt": attempts_used,
        },
    )

    if will_retry:
        countdown = settings.task_retry_backoff_seconds * (2 ** task.request.retries)
        try:
            raise task.retry(exc=exc, countdown=min(countdown, 300))
        except MaxRetriesExceededError:
            with session_scope() as db:
                image_service.mark_failed(
                    db,
                    image_uuid,
                    reason=f"{message} (retries exhausted)",
                    error_type=type(exc).__name__,
                    job_id=job_uuid,
                )

    return {"image_id": str(image_uuid), "status": ProcessingStatus.FAILED, "reason": message}


def _public_reason(exc: Exception) -> str:
    """Client-safe failure text: no paths, no stack traces."""
    if isinstance(exc, ProcessingError):
        return exc.detail
    return f"Processing failed due to an internal error ({type(exc).__name__})."


@celery_app.task(name="app.workers.image_tasks.recover_stale_jobs")
def recover_stale_jobs(stale_after_minutes: int = 15) -> dict:
    """Safety net for jobs stuck in `processing` after a hard worker crash.

    Intended to run on a Celery beat schedule in production. Kept as an explicit,
    invocable task here rather than wiring a scheduler for a take-home.
    """
    from sqlalchemy import select

    from app.models.image import Image
    from app.models.processing_job import ProcessingJob

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
    requeued = 0
    with session_scope() as db:
        stale = db.execute(
            select(ProcessingJob)
            .join(Image, Image.id == ProcessingJob.image_id)
            .where(
                ProcessingJob.status == JobStatus.PROCESSING,
                ProcessingJob.started_at < cutoff,
            )
        ).scalars()
        for job in stale:
            if job.attempts >= settings.task_max_retries:
                job.status = JobStatus.FAILED
                job.error_message = "Worker lost while processing; retry budget exhausted."
                job.image.status = ProcessingStatus.FAILED
                job.image.failure_reason = job.error_message
                continue
            job.status = JobStatus.PENDING
            job.image.status = ProcessingStatus.PENDING
            process_image_task.delay(str(job.image_id), str(job.id))
            requeued += 1
    logger.info("stale_jobs_recovered", extra={"requeued": requeued})
    return {"requeued": requeued}
