"""Liveness/readiness endpoint. Reports dependency reachability without failing."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.config import settings
from app.core.database import get_db
from app.schemas.image import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Service health")
def health(db: Session = Depends(get_db)) -> HealthResponse:
    deps: dict[str, str] = {}

    try:
        db.execute(text("SELECT 1"))
        deps["database"] = "ok"
    except Exception:
        deps["database"] = "unavailable"

    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
        client.ping()
        deps["redis"] = "ok"
    except Exception:
        deps["redis"] = "unavailable"

    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        deps["ocr"] = "ok"
    except Exception:
        # OCR is optional: the pipeline degrades to uncertain results without it.
        deps["ocr"] = "unavailable"

    overall = "ok" if deps.get("database") == "ok" else "degraded"
    return HealthResponse(
        status=overall,
        version=__version__,
        environment=settings.environment,
        dependencies=deps,
    )
