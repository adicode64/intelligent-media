"""Image lifecycle endpoints: upload, status, results, failure."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.orm import Session

from app.config import settings
from app.core.database import get_db
from app.core.exceptions import PayloadTooLargeError, UnsupportedMediaTypeError
from app.core.logging import get_logger
from app.models.enums import ProcessingStatus
from app.schemas.analysis import AnalysisResponse, FailureResponse
from app.schemas.image import ErrorResponse, StatusResponse, UploadResponse
from app.services import analysis_service, image_service

logger = get_logger(__name__)

router = APIRouter(prefix="/images", tags=["images"])

COMMON_ERRORS = {
    404: {"model": ErrorResponse, "description": "Image not found"},
    500: {"model": ErrorResponse, "description": "Internal error"},
}


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload an image for asynchronous analysis",
    responses={
        413: {"model": ErrorResponse, "description": "File too large"},
        415: {"model": ErrorResponse, "description": "Unsupported file type"},
        422: {"model": ErrorResponse, "description": "Corrupt image"},
        503: {"model": ErrorResponse, "description": "Queue unavailable"},
    },
)
async def upload_image(
    file: UploadFile = File(..., description="Vehicle image (JPEG/PNG/WEBP/BMP)"),
    db: Session = Depends(get_db),
) -> UploadResponse:
    """Validate, store and enqueue. Returns immediately — analysis runs in Celery."""
    if file is None or not file.filename:
        raise UnsupportedMediaTypeError("No file was provided in the request.")

    # Streamed read with a hard stop so an oversized body is never fully buffered.
    limit = settings.max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 256):
        total += len(chunk)
        if total > limit:
            await file.close()
            raise PayloadTooLargeError(
                f"File exceeds the {round(limit / 1_048_576, 2)} MB upload limit."
            )
        chunks.append(chunk)
    await file.close()
    data = b"".join(chunks)

    image, job = image_service.create_image_record(
        db, data, file.content_type, file.filename
    )
    image_service.enqueue_processing(db, image, job)

    return UploadResponse(
        id=image.id, status=ProcessingStatus(image.status), message="Image uploaded successfully"
    )


@router.get(
    "/{image_id}/status",
    response_model=StatusResponse,
    summary="Current processing status",
    responses=COMMON_ERRORS,
)
def get_status(image_id: uuid.UUID, db: Session = Depends(get_db)) -> StatusResponse:
    image = image_service.get_image(db, image_id)
    job = image_service.latest_job(db, image_id)

    try:
        current = ProcessingStatus(image.status)
    except ValueError:
        # Defensive: an unknown status is surfaced honestly rather than guessed.
        logger.error(
            "unknown_image_status",
            extra={"image_id": str(image_id), "status": image.status},
        )
        return StatusResponse(
            id=image.id,
            status=ProcessingStatus.FAILED,
            attempts=job.attempts if job else 0,
            created_at=image.created_at,
            updated_at=image.updated_at,
            failure_reason=f"Unknown processing status '{image.status}' recorded for this image.",
        )

    return StatusResponse(
        id=image.id,
        status=current,
        attempts=job.attempts if job else 0,
        created_at=image.created_at,
        updated_at=image.updated_at,
        failure_reason=image.failure_reason if current is ProcessingStatus.FAILED else None,
    )


@router.get(
    "/{image_id}/results",
    response_model=AnalysisResponse,
    summary="Structured analysis results",
    responses=COMMON_ERRORS,
)
def get_results(image_id: uuid.UUID, db: Session = Depends(get_db)) -> AnalysisResponse:
    """Returns 200 with `status` even while pending, so clients can poll one URL."""
    image = image_service.get_image_with_relations(db, image_id)
    return analysis_service.build_response(image, image.analysis)


@router.get(
    "/{image_id}/failure",
    response_model=FailureResponse,
    summary="Failure information for a failed job",
    responses=COMMON_ERRORS,
)
def get_failure(image_id: uuid.UUID, db: Session = Depends(get_db)) -> FailureResponse:
    image = image_service.get_image(db, image_id)
    job = image_service.latest_job(db, image_id)
    failed = image.status == ProcessingStatus.FAILED

    return FailureResponse(
        image_id=image.id,
        status=ProcessingStatus(image.status)
        if image.status in set(ProcessingStatus)
        else ProcessingStatus.FAILED,
        failed=failed,
        reason=(image.failure_reason or (job.error_message if job else None)) if failed else None,
        error_type=job.error_type if (job and failed) else None,
        attempts=job.attempts if job else 0,
        last_attempt_at=(job.completed_at or job.started_at) if job else None,
        retryable=bool(failed and job and job.attempts < settings.task_max_retries),
    )
