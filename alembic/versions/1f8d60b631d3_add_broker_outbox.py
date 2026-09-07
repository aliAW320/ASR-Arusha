"""add RabbitMQ transactional outbox

Revision ID: 1f8d60b631d3
Revises: e4e9a07019c0
Create Date: 2026-09-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "1f8d60b631d3"
down_revision: Union[str, Sequence[str], None] = "e4e9a07019c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "broker_outbox_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("queue_name", sa.String(length=150), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("deduplication_key", sa.String(length=255), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "publish_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "publish_attempts >= 0",
            name="ck_broker_outbox_publish_attempts_non_negative",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key"),
    )
    op.create_index(
        op.f("ix_broker_outbox_messages_queue_name"),
        "broker_outbox_messages",
        ["queue_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_broker_outbox_messages_deduplication_key"),
        "broker_outbox_messages",
        ["deduplication_key"],
        unique=True,
    )
    op.create_index(
        op.f("ix_broker_outbox_messages_available_at"),
        "broker_outbox_messages",
        ["available_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_broker_outbox_messages_published_at"),
        "broker_outbox_messages",
        ["published_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("broker_outbox_messages")
