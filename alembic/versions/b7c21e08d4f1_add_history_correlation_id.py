"""add history correlation id

Revision ID: b7c21e08d4f1
Revises: f4982e8de368
Create Date: 2026-08-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7c21e08d4f1"
down_revision: Union[str, Sequence[str], None] = "f4982e8de368"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "history",
        sa.Column("correlation_id", sa.String(length=100), nullable=True),
    )
    op.create_index(
        op.f("ix_history_correlation_id"),
        "history",
        ["correlation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_history_correlation_id"), table_name="history")
    op.drop_column("history", "correlation_id")
