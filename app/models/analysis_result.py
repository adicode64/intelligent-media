"""`analysis_results` table: the structured verdict for one image."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import GUID, Base, JSONType, TimestampTZ, utcnow


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    # One current result per image; re-processing replaces it.
    image_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("images.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    overall_status: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    vehicle_number: Mapped[str | None] = mapped_column(String(32), index=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow
    )

    image: Mapped["Image"] = relationship(back_populates="analysis")  # noqa: F821

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AnalysisResult image={self.image_id} status={self.overall_status}>"
