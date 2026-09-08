"""make diarization optional per voice

Revision ID: c3f81a2d4b67
Revises: 9c431a5f72e8
Create Date: 2026-09-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3f81a2d4b67"
down_revision: Union[str, Sequence[str], None] = "9c431a5f72e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "service_heartbeats",
        sa.Column("service_name", sa.String(length=64), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("service_name"),
    )

    decision = sa.Enum(
        "pending",
        "enabled",
        "skipped",
        "unavailable",
        name="diarization_decision",
        native_enum=False,
        create_constraint=True,
    )
    # The legacy create_all adoption path in f4982e8de368 returns early and
    # never builds the processing tables, so `voices` is not guaranteed here.
    if "voices" in sa.inspect(op.get_bind()).get_table_names():
        # Existing voices were all processed back when diarization was
        # mandatory, so they backfill to "enabled" rather than to the
        # "pending" default a fresh upload gets.
        op.add_column(
            "voices",
            sa.Column(
                "diarization_decision",
                decision,
                server_default="enabled",
                nullable=False,
            ),
        )


def downgrade() -> None:
    if "voices" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_column("voices", "diarization_decision")
    op.drop_table("service_heartbeats")
