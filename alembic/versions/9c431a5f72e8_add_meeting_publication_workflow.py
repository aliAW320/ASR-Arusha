"""add meeting attachments and approval-gated publication

Revision ID: 9c431a5f72e8
Revises: 1f8d60b631d3
Create Date: 2026-09-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9c431a5f72e8"
down_revision: Union[str, Sequence[str], None] = "1f8d60b631d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "meeting_attachments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("meeting_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by_id", sa.Uuid(), nullable=False),
        sa.Column("minio_bucket", sa.String(length=63), nullable=False),
        sa.Column("minio_key", sa.String(length=1024), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "size_bytes > 0", name="ck_meeting_attachment_size_positive"
        ),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "minio_bucket", "minio_key", name="uq_meeting_attachment_minio_object"
        ),
    )
    op.create_index(
        op.f("ix_meeting_attachments_meeting_id"),
        "meeting_attachments",
        ["meeting_id"],
    )
    op.create_index(
        op.f("ix_meeting_attachments_uploaded_by_id"),
        "meeting_attachments",
        ["uploaded_by_id"],
    )
    op.create_index(
        op.f("ix_meeting_attachments_checksum_sha256"),
        "meeting_attachments",
        ["checksum_sha256"],
    )

    publication_status = sa.Enum(
        "awaiting_approval",
        "queued",
        "running",
        "published",
        "failed",
        name="meeting_publication_status",
        native_enum=False,
        create_constraint=True,
    )
    vision_status = sa.Enum(
        "not_requested",
        "used",
        "unsupported",
        name="publication_vision_status",
        native_enum=False,
        create_constraint=True,
    )
    op.create_table(
        "meeting_publications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("meeting_id", sa.Uuid(), nullable=False),
        sa.Column("meeting_result_id", sa.Uuid(), nullable=True),
        sa.Column("current_job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            publication_status,
            server_default="awaiting_approval",
            nullable=False,
        ),
        sa.Column(
            "destination_path",
            sa.String(length=1024),
            server_default="پروژه‌های کارآموزی/ASR test",
            nullable=False,
        ),
        sa.Column("approved_by_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attachment_ids", sa.JSON(), nullable=False),
        sa.Column(
            "vision_status",
            vision_status,
            server_default="not_requested",
            nullable=False,
        ),
        sa.Column("outline_parent_document_id", sa.String(length=255), nullable=True),
        sa.Column("outline_summary_document_id", sa.String(length=255), nullable=True),
        sa.Column("outline_transcript_document_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["meeting_result_id"], ["meeting_results.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["current_job_id"], ["processing_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["approved_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("meeting_id", name="uq_meeting_publication_meeting"),
    )
    for column in (
        "meeting_id",
        "meeting_result_id",
        "current_job_id",
        "status",
        "approved_by_id",
    ):
        op.create_index(
            op.f(f"ix_meeting_publications_{column}"),
            "meeting_publications",
            [column],
        )


def downgrade() -> None:
    op.drop_table("meeting_publications")
    op.drop_table("meeting_attachments")
