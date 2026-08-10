"""FastAPI application factory."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.errors import register_exception_handlers
from app.api.routes import health, images
from app.config import settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings.storage_path  # ensure the storage directory exists
    logger.info(
        "api_startup",
        extra={"version": __version__, "environment": settings.environment},
    )
    yield
    logger.info("api_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Asynchronous vehicle-image analysis pipeline. All quality verdicts are "
            "heuristics with explicit confidence values, not classifier guarantees."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Permissive in dev; a published deployment should pin explicit origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.debug else settings.allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(images.router, prefix=settings.api_v1_prefix)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"service": settings.app_name, "version": __version__, "docs": "/docs"}

    return app


app = create_app()
