"""meeting composer sources

Revision ID: e4e9a07019c0
Revises: b7c21e08d4f1
Create Date: 2026-09-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4e9a07019c0"
down_revision: Union[str, Sequence[str], None] = "b7c21e08d4f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_legacy_meeting_result_sources(bind) -> None:
    """Give any pre-existing meeting_result_sources row a deterministic
    position/offset/duration/artifact snapshot.

    No code path has ever inserted into this table before this migration, so
    in every real deployment this is a no-op. It stays correct on principle:
    a row whose Result has no surviving artifact cannot be given real lineage,
    so it is dropped instead of invented.
    """
    rows = bind.execute(
        sa.text(
            "SELECT meeting_result_id, result_id FROM meeting_result_sources "
            "ORDER BY meeting_result_id, result_id"
        )
    ).all()
    positions: dict[str, int] = {}
    for meeting_result_id, result_id in rows:
        key = str(meeting_result_id)
        position = positions.get(key, 0)
        positions[key] = position + 1
        artifact_id = bind.execute(
            sa.text(
                "SELECT id FROM result_artifacts WHERE result_id = :result_id "
                "ORDER BY created_at LIMIT 1"
            ),
            {"result_id": result_id},
        ).scalar()
        if artifact_id is None:
            bind.execute(
                sa.text(
                    "DELETE FROM meeting_result_sources "
                    "WHERE meeting_result_id = :meeting_result_id "
                    "AND result_id = :result_id"
                ),
                {"meeting_result_id": meeting_result_id, "result_id": result_id},
            )
            continue
        bind.execute(
            sa.text(
                "UPDATE meeting_result_sources SET "
                "position = :position, source_offset_ms = 0, "
                "source_duration_ms = 1, source_artifact_id = :artifact_id "
                "WHERE meeting_result_id = :meeting_result_id "
                "AND result_id = :result_id"
            ),
            {
                "position": position,
                "artifact_id": artifact_id,
                "meeting_result_id": meeting_result_id,
                "result_id": result_id,
            },
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "meeting_results" not in existing_tables:
        # A database adopted from the pre-Alembic legacy schema (see
        # f4982e8de368) never created meeting_results / meeting_result_sources
        # at all -- there is nothing to alter. It will pick up the current
        # shape from a future migration that (re)introduces those tables.
        return

    with op.batch_alter_table("meeting_results") as batch:
        batch.add_column(
            sa.Column(
                "schema_version",
                sa.String(length=50),
                nullable=False,
                server_default="meeting-transcript/v1",
            )
        )
        batch.add_column(
            sa.Column("source_fingerprint", sa.String(length=64), nullable=True)
        )

    op.execute(
        "UPDATE meeting_results SET source_fingerprint = 'legacy:' || id "
        "WHERE source_fingerprint IS NULL"
    )

    with op.batch_alter_table("meeting_results") as batch:
        batch.alter_column(
            "source_fingerprint",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch.create_unique_constraint(
            "uq_meeting_result_meeting_fingerprint",
            ["meeting_id", "source_fingerprint"],
        )

    with op.batch_alter_table("meeting_result_sources") as batch:
        batch.add_column(sa.Column("position", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("voice_sequence_snapshot", sa.Integer(), nullable=True)
        )
        batch.add_column(sa.Column("source_offset_ms", sa.BigInteger(), nullable=True))
        batch.add_column(
            sa.Column("source_duration_ms", sa.BigInteger(), nullable=True)
        )
        batch.add_column(sa.Column("source_artifact_id", sa.Uuid(), nullable=True))

    _backfill_legacy_meeting_result_sources(bind)

    with op.batch_alter_table("meeting_result_sources") as batch:
        batch.alter_column("position", existing_type=sa.Integer(), nullable=False)
        batch.alter_column(
            "source_offset_ms", existing_type=sa.BigInteger(), nullable=False
        )
        batch.alter_column(
            "source_duration_ms", existing_type=sa.BigInteger(), nullable=False
        )
        batch.alter_column(
            "source_artifact_id", existing_type=sa.Uuid(), nullable=False
        )
        batch.create_check_constraint(
            "ck_meeting_result_source_position_non_negative", "position >= 0"
        )
        batch.create_check_constraint(
            "ck_meeting_result_source_voice_sequence_non_negative",
            "voice_sequence_snapshot IS NULL OR voice_sequence_snapshot >= 0",
        )
        batch.create_check_constraint(
            "ck_meeting_result_source_offset_non_negative", "source_offset_ms >= 0"
        )
        batch.create_check_constraint(
            "ck_meeting_result_source_duration_positive", "source_duration_ms > 0"
        )
        batch.create_unique_constraint(
            "uq_meeting_result_source_position", ["meeting_result_id", "position"]
        )
        batch.create_foreign_key(
            "fk_meeting_result_source_artifact",
            "result_artifacts",
            ["source_artifact_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            op.f("ix_meeting_result_sources_source_artifact_id"),
            ["source_artifact_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "meeting_results" not in set(inspector.get_table_names()):
        return

    with op.batch_alter_table("meeting_result_sources") as batch:
        batch.drop_index(op.f("ix_meeting_result_sources_source_artifact_id"))
        batch.drop_constraint("fk_meeting_result_source_artifact", type_="foreignkey")
        batch.drop_constraint("uq_meeting_result_source_position", type_="unique")
        batch.drop_constraint(
            "ck_meeting_result_source_duration_positive", type_="check"
        )
        batch.drop_constraint(
            "ck_meeting_result_source_offset_non_negative", type_="check"
        )
        batch.drop_constraint(
            "ck_meeting_result_source_voice_sequence_non_negative", type_="check"
        )
        batch.drop_constraint(
            "ck_meeting_result_source_position_non_negative", type_="check"
        )
        batch.drop_column("source_artifact_id")
        batch.drop_column("source_duration_ms")
        batch.drop_column("source_offset_ms")
        batch.drop_column("voice_sequence_snapshot")
        batch.drop_column("position")

    with op.batch_alter_table("meeting_results") as batch:
        batch.drop_constraint(
            "uq_meeting_result_meeting_fingerprint", type_="unique"
        )
        batch.drop_column("source_fingerprint")
        batch.drop_column("schema_version")
