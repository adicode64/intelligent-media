"""
SQLAlchemy engine / session wiring.

A synchronous engine is used deliberately: the Celery worker is synchronous, and
the API's DB calls are short metadata reads/writes. This keeps one session model
across API and worker instead of maintaining both sync and async stacks.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models.base import Base  # re-exported for Alembic convenience

__all__ = ["Base", "engine", "SessionLocal", "get_db", "session_scope"]


def _engine_kwargs() -> dict:
    url = settings.database_url
    if url.startswith("sqlite"):
        # Used by the test-suite; a single shared in-file/in-memory DB.
        return {"connect_args": {"check_same_thread": False}}
    return {
        "pool_pre_ping": True,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
    }


engine = create_engine(settings.database_url, future=True, **_engine_kwargs())
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Transactional scope for worker code."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
