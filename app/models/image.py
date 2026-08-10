"""`images` table: one row per uploaded file, plus its lifecycle status."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import GUID, Base, TimestampTZ, utcnow
from app.models.enums import ProcessingStatus


class Image(Base):
    __tablename__ = "images"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)

    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    # 16 hex chars for a 64-bit perceptual hash.
    image_hash: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProcessingStatus.PENDING, index=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, onupdate=utcnow
    )

    jobs: Mapped[list["ProcessingJob"]] = relationship(  # noqa: F821
        back_populates="image", cascade="all, delete-orphan", order_by="ProcessingJob.created_at"
    )
    analysis: Mapped["AnalysisResult | None"] = relationship(  # noqa: F821
        back_populates="image", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        # Duplicate detection scans recent hashes: composite index serves it.
        Index("ix_images_hash_created_at", "image_hash", "created_at"),
        Index("ix_images_status_created_at", "status", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Image {self.id} status={self.status}>"
