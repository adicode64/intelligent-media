"""
Domain exceptions.

Split into client errors (mapped to 4xx by the API layer) and processing errors
(raised inside the worker). `TransientProcessingError` is the only class the
worker retries — everything else is treated as permanent to avoid burning the
queue on deterministic failures.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for all application errors."""

    status_code = 500
    error_code = "internal_error"
    public_message = "An unexpected error occurred."

    def __init__(self, detail: str | None = None):
        self.detail = detail or self.public_message
        super().__init__(self.detail)


# ------------------------------------------------------------------ client 4xx
class ValidationError(AppError):
    status_code = 400
    error_code = "validation_error"
    public_message = "The request was invalid."


class UnsupportedMediaTypeError(AppError):
    status_code = 415
    error_code = "unsupported_media_type"
    public_message = "Unsupported file type."


class PayloadTooLargeError(AppError):
    status_code = 413
    error_code = "payload_too_large"
    public_message = "Uploaded file is too large."


class CorruptImageError(AppError):
    status_code = 422
    error_code = "corrupt_image"
    public_message = "The uploaded file is not a readable image."


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"
    public_message = "Resource not found."


class StorageError(AppError):
    status_code = 500
    error_code = "storage_error"
    public_message = "Could not store the uploaded file."


class QueueError(AppError):
    status_code = 503
    error_code = "queue_unavailable"
    public_message = "Processing queue is unavailable. Please retry."


class DatabaseError(AppError):
    status_code = 503
    error_code = "database_error"
    public_message = "Database is currently unavailable."


# -------------------------------------------------------------- processing
class ProcessingError(AppError):
    """Permanent processing failure: retrying will not help."""

    error_code = "processing_error"
    public_message = "Image processing failed."
    retryable = False


class TransientProcessingError(ProcessingError):
    """Temporary failure (I/O hiccup, DB blip). Safe to retry."""

    error_code = "transient_processing_error"
    public_message = "Image processing failed temporarily."
    retryable = True
