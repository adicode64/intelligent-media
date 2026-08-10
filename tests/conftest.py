"""
Shared test fixtures.

The suite runs entirely offline: SQLite instead of Postgres, an in-process fake
for the Celery publish call, and no dependency on the Tesseract binary (OCR-
dependent assertions are written against mocked engine behaviour).
"""

from __future__ import annotations

import os
import tempfile
import uuid

# Must be set before app.config is imported anywhere.
_TMP = tempfile.mkdtemp(prefix="imp-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("STORAGE_DIR", f"{_TMP}/storage")
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "true")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

import io  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image as PILImage  # noqa: E402

from app.core.database import Base, SessionLocal, engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import image_service  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db(_create_schema):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _clean_tables(_create_schema):
    """Isolate tests: truncate in FK-safe order before each test."""
    session = SessionLocal()
    try:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    finally:
        session.close()
    yield


@pytest.fixture()
def enqueued(monkeypatch):
    """Capture enqueue calls instead of publishing to Redis."""
    calls: list[tuple[str, str]] = []

    def fake_enqueue(db, image, job):
        calls.append((str(image.id), str(job.id)))
        return f"task-{uuid.uuid4()}"

    monkeypatch.setattr(image_service, "enqueue_processing", fake_enqueue)
    return calls


@pytest.fixture()
def client(enqueued):
    def override_get_db():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ----------------------------------------------------------------- factories
def make_image_bytes(
    width: int = 800,
    height: int = 600,
    fmt: str = "JPEG",
    kind: str = "sharp",
    seed: int = 7,
) -> bytes:
    """Deterministic synthetic images with controllable sharpness/brightness."""
    rng = np.random.default_rng(seed)
    if kind == "sharp":
        # High-frequency checkerboard: large Laplacian variance.
        arr = np.zeros((height, width, 3), dtype=np.uint8)
        arr[::2, ::2] = 255
        arr[1::2, 1::2] = 255
    elif kind == "blurry":
        base = rng.integers(100, 140, size=(height // 40 + 1, width // 40 + 1, 3), dtype=np.uint8)
        img = PILImage.fromarray(base).resize((width, height), PILImage.BILINEAR)
        arr = np.asarray(img)
    elif kind == "dark":
        arr = np.full((height, width, 3), 15, dtype=np.uint8)
    elif kind == "bright":
        arr = np.full((height, width, 3), 250, dtype=np.uint8)
    else:  # "flat" mid-grey
        arr = np.full((height, width, 3), 128, dtype=np.uint8)

    buf = io.BytesIO()
    PILImage.fromarray(arr).save(buf, format=fmt, quality=95)
    return buf.getvalue()


@pytest.fixture()
def image_bytes():
    return make_image_bytes()


@pytest.fixture()
def upload_file_tuple():
    def _make(name: str = "vehicle.jpg", **kwargs):
        return {"file": (name, make_image_bytes(**kwargs), "image/jpeg")}

    return _make
