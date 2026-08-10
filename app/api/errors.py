"""
Exception handlers.

Single rule: clients get an error code plus a short, curated message. Stack
traces and internal detail go to the logs with a request id so support can
correlate the two.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or str(uuid.uuid4())


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = _request_id(request)
        log = logger.error if exc.status_code >= 500 else logger.warning
        log(
            "app_error",
            extra={
                "request_id": request_id,
                "path": request.url.path,
                "error_code": exc.error_code,
                "status": exc.status_code,
                "detail": exc.detail,
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.error_code,
                "detail": exc.detail,
                "request_id": request_id,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = _request_id(request)
        fields = [
            {"field": ".".join(str(p) for p in e.get("loc", [])), "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "detail": "Request validation failed.",
                "fields": fields,
                "request_id": request_id,
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        request_id = _request_id(request)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": "http_error",
                "detail": str(exc.detail),
                "request_id": request_id,
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def handle_db(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        request_id = _request_id(request)
        logger.error(
            "database_error",
            extra={"request_id": request_id, "path": request.url.path, "type": type(exc).__name__},
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": "database_error",
                "detail": "Database is currently unavailable. Please retry.",
                "request_id": request_id,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        # exception() records the traceback in logs only; never in the response.
        logger.exception(
            "unhandled_exception",
            extra={"request_id": request_id, "path": request.url.path, "type": type(exc).__name__},
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "detail": "An unexpected error occurred. Reference the request id when reporting.",
                "request_id": request_id,
            },
        )
