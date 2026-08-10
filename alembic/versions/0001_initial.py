"""initial schema: images, processing_jobs, analysis_results

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-08
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "images",
        sa.Column("id", UUID, primary_key=True, nullable=False),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("stored_filename", sa.String(512), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("image_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_images_status", "images", ["status"])
    op.create_index("ix_images_hash_created_at", "images", ["image_hash", "created_at"])
    op.create_index("ix_images_status_created_at", "images", ["status", "created_at"])

    op.create_table(
        "processing_jobs",
        sa.Column("id", UUID, primary_key=True, nullable=False),
        sa.Column("image_id", UUID, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_type", sa.String(128), nullable=True),
        sa.Column("celery_task_id", sa.String(255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["image_id"], ["images.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_processing_jobs_image_id", "processing_jobs", ["image_id"])
    op.create_index("ix_processing_jobs_status", "processing_jobs", ["status"])
    op.create_index("ix_jobs_image_created", "processing_jobs", ["image_id", "created_at"])

    op.create_table(
        "analysis_results",
        sa.Column("id", UUID, primary_key=True, nullable=False),
        sa.Column("image_id", UUID, nullable=False),
        sa.Column("overall_status", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("vehicle_number", sa.String(32), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["image_id"], ["images.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("image_id", name="uq_analysis_results_image_id"),
    )
    op.create_index("ix_analysis_results_image_id", "analysis_results", ["image_id"])
    op.create_index("ix_analysis_results_vehicle_number", "analysis_results", ["vehicle_number"])


def downgrade() -> None:
    op.drop_table("analysis_results")
    op.drop_table("processing_jobs")
    op.drop_index("ix_images_status_created_at", table_name="images")
    op.drop_index("ix_images_hash_created_at", table_name="images")
    op.drop_index("ix_images_status", table_name="images")
    op.drop_table("images")
