"""
Central application configuration.

Every tunable value (thresholds, limits, connection strings) lives here and is
sourced from environment variables so that no behaviour-defining constant is
hard-coded inside business logic. See `.env.example` for the full list.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "Intelligent Media Processing Pipeline"
    api_v1_prefix: str = "/api/v1"
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    # Comma-separated browser origins allowed to call the API (React dev server
    # defaults included). Set to "*" to allow any origin.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000"

    # ------------------------------------------------------------- database
    database_url: str = "postgresql+psycopg2://media:media@localhost:5432/media"
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ---------------------------------------------------------------- redis
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    celery_task_always_eager: bool = False

    # -------------------------------------------------------------- storage
    storage_dir: Path = Path("storage")
    max_upload_bytes: int = 10 * 1024 * 1024  # 10 MB
    allowed_content_types: tuple[str, ...] = (
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/bmp",
    )
    # Pillow format names that we accept after sniffing real file content.
    allowed_image_formats: tuple[str, ...] = ("JPEG", "PNG", "WEBP", "BMP", "MPO")

    # ---------------------------------------------------- analysis: thresholds
    # Laplacian variance below this => blurry. Above blur_warn => sharp.
    blur_fail_threshold: float = 60.0
    blur_warn_threshold: float = 120.0

    # Mean grayscale brightness (0-255).
    brightness_dark_threshold: float = 55.0
    brightness_warn_dark_threshold: float = 80.0
    brightness_warn_bright_threshold: float = 195.0
    brightness_bright_threshold: float = 220.0

    # Perceptual-hash Hamming distance (0-64 for a 64-bit phash).
    duplicate_exact_distance: int = 2
    duplicate_similar_distance: int = 8
    duplicate_scan_limit: int = 2000

    # Dimension validation.
    min_image_width: int = 320
    min_image_height: int = 320
    warn_image_width: int = 640
    warn_image_height: int = 480
    min_aspect_ratio: float = 0.4
    max_aspect_ratio: float = 3.0

    # OCR.
    ocr_enabled: bool = True
    ocr_min_confidence: float = 0.45
    ocr_languages: str = "eng"
    ocr_timeout_seconds: int = 20

    # Screenshot / photo-of-photo heuristic.
    screenshot_edge_ratio_threshold: float = 0.55
    screenshot_uniform_row_ratio: float = 0.35

    # ------------------------------------------------------------- worker
    task_max_retries: int = 3
    task_retry_backoff_seconds: int = 5

    @property
    def allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    @property
    def storage_path(self) -> Path:
        path = Path(self.storage_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor (import-safe, cheap to call anywhere)."""
    return Settings()


settings = get_settings()
