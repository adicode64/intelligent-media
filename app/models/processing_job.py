"""`processing_jobs` table: an auditable attempt log for async processing."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import GUID, Base, TimestampTZ, utcnow
from app.models.enums import JobStatus


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    image_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("images.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JobStatus.PENDING, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(String(128))
    celery_task_id: Mapped[str | None] = mapped_column(String(255))

    started_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    completed_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow
    )

    image: Mapped["Image"] = relationship(back_populates="jobs")  # noqa: F821

    __table_args__ = (Index("ix_jobs_image_created", "image_id", "created_at"),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ProcessingJob {self.id} image={self.image_id} status={self.status}>"
