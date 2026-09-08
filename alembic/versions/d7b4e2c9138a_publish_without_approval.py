"""publish to the knowledge base without an approval step

Revision ID: d7b4e2c9138a
Revises: c3f81a2d4b67
Create Date: 2026-09-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7b4e2c9138a"
down_revision: Union[str, Sequence[str], None] = "c3f81a2d4b67"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_STATUSES = ("awaiting_approval", "queued", "running", "published", "failed")
_NEW_STATUSES = ("pending", "queued", "running", "published", "failed")


def _status(values: Sequence[str]) -> sa.Enum:
    return sa.Enum(
        *values,
        name="meeting_publication_status",
        native_enum=False,
        create_constraint=True,
    )


def upgrade() -> None:
    # SQLite batch mode rebuilds the table by reflecting it *and* its
    # foreign-key targets. The legacy create_all adoption path in
    # f4982e8de368 returns early and never builds those, so there is nothing
    # here to rewrite.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if not {"meeting_publications", "meeting_results", "processing_jobs"} <= tables:
        return

    # Drop the index first: batch mode rebuilds the table and would otherwise
    # try to recreate this index over a column that is no longer there.
    op.drop_index(
        "ix_meeting_publications_approved_by_id", table_name="meeting_publications"
    )

    # "awaiting_approval" meant "composed, waiting for a human"; nothing waits
    # for a human any more, so those rows become plain "pending" -- composed,
    # not yet handed to the publication worker.
    with op.batch_alter_table("meeting_publications") as batch:
        batch.alter_column(
            "status",
            existing_type=_status(_OLD_STATUSES),
            type_=sa.String(length=32),
            existing_nullable=False,
            server_default=None,
        )
        batch.drop_column("approved_by_id")
        batch.drop_column("approved_at")
    op.execute(
        "UPDATE meeting_publications SET status = 'pending' "
        "WHERE status = 'awaiting_approval'"
    )
    with op.batch_alter_table("meeting_publications") as batch:
        batch.alter_column(
            "status",
            existing_type=sa.String(length=32),
            type_=_status(_NEW_STATUSES),
            existing_nullable=False,
            server_default="pending",
        )


def downgrade() -> None:
    # SQLite batch mode rebuilds the table by reflecting it *and* its
    # foreign-key targets. The legacy create_all adoption path in
    # f4982e8de368 returns early and never builds those, so there is nothing
    # here to rewrite.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if not {"meeting_publications", "meeting_results", "processing_jobs"} <= tables:
        return

    with op.batch_alter_table("meeting_publications") as batch:
        batch.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("approved_by_id", sa.Uuid(), nullable=True))
        batch.alter_column(
            "status",
            existing_type=_status(_NEW_STATUSES),
            type_=sa.String(length=32),
            existing_nullable=False,
            server_default=None,
        )
    op.execute(
        "UPDATE meeting_publications SET status = 'awaiting_approval' "
        "WHERE status = 'pending'"
    )
    with op.batch_alter_table("meeting_publications") as batch:
        batch.alter_column(
            "status",
            existing_type=sa.String(length=32),
            type_=_status(_OLD_STATUSES),
            existing_nullable=False,
            server_default="awaiting_approval",
        )
    op.create_index(
        "ix_meeting_publications_approved_by_id",
        "meeting_publications",
        ["approved_by_id"],
    )
