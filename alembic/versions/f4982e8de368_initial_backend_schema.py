"""initial backend schema

Revision ID: f4982e8de368
Revises: 
Create Date: 2026-08-26 12:14:25.170954
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import uuid


revision: str = 'f4982e8de368'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _upgrade_legacy_create_all_schema() -> bool:
    """Adopt databases created by the pre-Alembic application startup."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        return False

    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "role" not in user_columns:
        with op.batch_alter_table("users") as batch:
            batch.add_column(
                sa.Column(
                    "role",
                    sa.Enum(
                        "admin",
                        "user",
                        name="user_role",
                        native_enum=False,
                        create_constraint=True,
                    ),
                    server_default="user",
                    nullable=False,
                )
            )
            batch.create_index("ix_users_role", ["role"], unique=False)

    if "auth_identities" not in inspector.get_table_names():
        op.create_table(
            "auth_identities",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("provider", sa.String(length=100), nullable=False),
            sa.Column("subject", sa.String(length=320), nullable=False),
            sa.Column("secret_hash", sa.String(length=255), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("provider", "subject", name="uq_auth_identity_subject"),
            sa.UniqueConstraint("user_id", "provider", name="uq_auth_identity_user_provider"),
        )
        op.create_index("ix_auth_identities_provider", "auth_identities", ["provider"])
        op.create_index("ix_auth_identities_user_id", "auth_identities", ["user_id"])

    if "hashed_password" in user_columns:
        identities = sa.table(
            "auth_identities",
            sa.column("id", sa.Uuid()),
            sa.column("user_id", sa.Uuid()),
            sa.column("provider", sa.String()),
            sa.column("subject", sa.String()),
            sa.column("secret_hash", sa.String()),
        )
        rows = bind.execute(
            sa.text("SELECT id, email, hashed_password FROM users")
        ).mappings()
        payload = [
            {
                "id": uuid.uuid4(),
                "user_id": (
                    row["id"]
                    if isinstance(row["id"], uuid.UUID)
                    else uuid.UUID(str(row["id"]))
                ),
                "provider": "local",
                "subject": row["email"].lower(),
                "secret_hash": row["hashed_password"],
            }
            for row in rows
        ]
        if payload:
            op.bulk_insert(identities, payload)
        with op.batch_alter_table("users") as batch:
            batch.drop_column("hashed_password")

    member_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("meeting_members")
        if constraint.get("name")
    }
    with op.batch_alter_table("meeting_members") as batch:
        if "meeting_member_role" in member_checks:
            batch.drop_constraint("meeting_member_role", type_="check")
        batch.create_check_constraint(
            "meeting_member_role",
            "role IN ('owner', 'contributor', 'viewer')",
        )
    op.execute(
        "UPDATE meeting_members SET role = 'owner' "
        "WHERE EXISTS (SELECT 1 FROM meetings "
        "WHERE meetings.id = meeting_members.meeting_id "
        "AND meetings.owner_id = meeting_members.user_id)"
    )
    op.execute(
        "INSERT INTO meeting_members (meeting_id, user_id, role) "
        "SELECT meetings.id, meetings.owner_id, 'owner' FROM meetings "
        "WHERE NOT EXISTS (SELECT 1 FROM meeting_members "
        "WHERE meeting_members.meeting_id = meetings.id "
        "AND meeting_members.user_id = meetings.owner_id)"
    )

    history_columns = {column["name"] for column in inspector.get_columns("history")}
    history_indexes = {
        index["name"] for index in inspector.get_indexes("history") if index.get("name")
    }
    with op.batch_alter_table("history") as batch:
        if "event_type" not in history_columns:
            batch.add_column(sa.Column("event_type", sa.String(length=100), nullable=True))
        if "metadata" not in history_columns:
            batch.add_column(sa.Column("metadata", sa.JSON(), nullable=True))
        if "request_id" not in history_columns:
            batch.add_column(sa.Column("request_id", sa.String(length=100), nullable=True))
        if "ip_address" not in history_columns:
            batch.add_column(sa.Column("ip_address", sa.String(length=64), nullable=True))
    op.execute("UPDATE history SET event_type = 'legacy.event' WHERE event_type IS NULL")
    history_fks = inspector.get_foreign_keys("history")
    actor_fk = next(
        (fk for fk in history_fks if fk.get("constrained_columns") == ["actor_user_id"]),
        None,
    )
    with op.batch_alter_table("history") as batch:
        batch.alter_column("event_type", existing_type=sa.String(length=100), nullable=False)
        if "ix_history_event_type" not in history_indexes:
            batch.create_index("ix_history_event_type", ["event_type"], unique=False)
        if "ix_history_request_id" not in history_indexes:
            batch.create_index("ix_history_request_id", ["request_id"], unique=False)
        if actor_fk and actor_fk.get("name"):
            batch.drop_constraint(actor_fk["name"], type_="foreignkey")
        batch.alter_column("actor_user_id", existing_type=sa.Uuid(), nullable=True)
        if actor_fk and actor_fk.get("name"):
            batch.create_foreign_key(
                actor_fk["name"],
                "users",
                ["actor_user_id"],
                ["id"],
                ondelete="SET NULL",
            )
    return True


def upgrade() -> None:
    if _upgrade_legacy_create_all_schema():
        return
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('external_integrations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('version', sa.String(length=100), nullable=False),
    sa.Column('kind', sa.Enum('mcp', 'http', 'grpc', 'other', name='external_integration_kind', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('endpoint', sa.String(length=2048), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name', 'version', name='uq_external_integration_name_version')
    )
    op.create_table('model_runtimes',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('kind', sa.Enum('local', 'remote', name='model_runtime_kind', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('full_name', sa.String(length=150), nullable=True),
    sa.Column('role', sa.Enum('admin', 'user', name='user_role', native_enum=False, create_constraint=True), server_default='user', nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_role'), 'users', ['role'], unique=False)
    op.create_table('auth_identities',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('provider', sa.String(length=100), nullable=False),
    sa.Column('subject', sa.String(length=320), nullable=False),
    sa.Column('secret_hash', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'subject', name='uq_auth_identity_subject'),
    sa.UniqueConstraint('user_id', 'provider', name='uq_auth_identity_user_provider')
    )
    op.create_index(op.f('ix_auth_identities_provider'), 'auth_identities', ['provider'], unique=False)
    op.create_index(op.f('ix_auth_identities_user_id'), 'auth_identities', ['user_id'], unique=False)
    op.create_table('history',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('event_type', sa.String(length=100), nullable=False),
    sa.Column('action_description', sa.Text(), nullable=False),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.Column('request_id', sa.String(length=100), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('actor_user_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_history_actor_user_id'), 'history', ['actor_user_id'], unique=False)
    op.create_index(op.f('ix_history_created_at'), 'history', ['created_at'], unique=False)
    op.create_index(op.f('ix_history_event_type'), 'history', ['event_type'], unique=False)
    op.create_index(op.f('ix_history_request_id'), 'history', ['request_id'], unique=False)
    op.create_table('meetings',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('meeting_date', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('owner_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_meetings_owner_id'), 'meetings', ['owner_id'], unique=False)
    op.create_table('model_definitions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('runtime_id', sa.Uuid(), nullable=False),
    sa.Column('task_type', sa.Enum('transcription', 'diarization', 'cleaning', 'embedding', 'minutes_generation', 'other', name='model_task_type', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('version', sa.String(length=255), nullable=False),
    sa.Column('source_uri', sa.String(length=2048), nullable=True),
    sa.Column('checksum_sha256', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['runtime_id'], ['model_runtimes.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('runtime_id', 'task_type', 'name', 'version', name='uq_model_definition_identity')
    )
    op.create_index(op.f('ix_model_definitions_runtime_id'), 'model_definitions', ['runtime_id'], unique=False)
    op.create_index(op.f('ix_model_definitions_task_type'), 'model_definitions', ['task_type'], unique=False)
    op.create_table('history_affected_meetings',
    sa.Column('history_id', sa.Uuid(), nullable=False),
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['history_id'], ['history.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('history_id', 'meeting_id')
    )
    op.create_table('history_affected_users',
    sa.Column('history_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['history_id'], ['history.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('history_id', 'user_id')
    )
    op.create_table('meeting_members',
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.Enum('owner', 'contributor', 'viewer', name='meeting_member_role', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('meeting_id', 'user_id')
    )
    op.create_table('meeting_results',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_meeting_results_generated_at'), 'meeting_results', ['generated_at'], unique=False)
    op.create_index(op.f('ix_meeting_results_meeting_id'), 'meeting_results', ['meeting_id'], unique=False)
    op.create_table('meeting_speakers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('display_name', sa.String(length=150), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_meeting_speakers_meeting_id'), 'meeting_speakers', ['meeting_id'], unique=False)
    op.create_table('voices',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('minio_bucket', sa.String(length=63), nullable=False),
    sa.Column('minio_key', sa.String(length=1024), nullable=False),
    sa.Column('original_filename', sa.String(length=512), nullable=True),
    sa.Column('content_type', sa.String(length=255), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=True),
    sa.Column('checksum_sha256', sa.String(length=64), nullable=True),
    sa.Column('duration_ms', sa.BigInteger(), nullable=True),
    sa.Column('codec', sa.String(length=64), nullable=True),
    sa.Column('sample_rate_hz', sa.Integer(), nullable=True),
    sa.Column('channels', sa.Integer(), nullable=True),
    sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sequence_number', sa.Integer(), nullable=True),
    sa.Column('status', sa.Enum('waiting', 'pending', 'finished', 'error', name='voice_status', native_enum=False, create_constraint=True), server_default='waiting', nullable=False),
    sa.Column('meeting_id', sa.Uuid(), nullable=True),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('uploaded_by_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('channels IS NULL OR channels > 0', name='ck_voice_channels_positive'),
    sa.CheckConstraint('duration_ms IS NULL OR duration_ms >= 0', name='ck_voice_duration_non_negative'),
    sa.CheckConstraint('sample_rate_hz IS NULL OR sample_rate_hz > 0', name='ck_voice_sample_rate_positive'),
    sa.CheckConstraint('sequence_number IS NULL OR sequence_number >= 0', name='ck_voice_sequence_non_negative'),
    sa.CheckConstraint('size_bytes IS NULL OR size_bytes >= 0', name='ck_voice_size_non_negative'),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['uploaded_by_id'], ['users.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('meeting_id', 'sequence_number', name='uq_voice_meeting_sequence'),
    sa.UniqueConstraint('minio_bucket', 'minio_key', name='uq_voice_minio_object')
    )
    op.create_index(op.f('ix_voices_checksum_sha256'), 'voices', ['checksum_sha256'], unique=False)
    op.create_index(op.f('ix_voices_meeting_id'), 'voices', ['meeting_id'], unique=False)
    op.create_index(op.f('ix_voices_status'), 'voices', ['status'], unique=False)
    op.create_index(op.f('ix_voices_uploaded_by_id'), 'voices', ['uploaded_by_id'], unique=False)
    op.create_table('history_affected_meeting_results',
    sa.Column('history_id', sa.Uuid(), nullable=False),
    sa.Column('meeting_result_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['history_id'], ['history.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['meeting_result_id'], ['meeting_results.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('history_id', 'meeting_result_id')
    )
    op.create_table('history_affected_meeting_speakers',
    sa.Column('history_id', sa.Uuid(), nullable=False),
    sa.Column('meeting_speaker_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['history_id'], ['history.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['meeting_speaker_id'], ['meeting_speakers.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('history_id', 'meeting_speaker_id')
    )
    op.create_table('history_affected_voices',
    sa.Column('history_id', sa.Uuid(), nullable=False),
    sa.Column('voice_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['history_id'], ['history.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['voice_id'], ['voices.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('history_id', 'voice_id')
    )
    op.create_table('results',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('voice_id', sa.Uuid(), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['voice_id'], ['voices.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_results_generated_at'), 'results', ['generated_at'], unique=False)
    op.create_index(op.f('ix_results_voice_id'), 'results', ['voice_id'], unique=False)
    op.create_table('diarization_speakers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('result_id', sa.Uuid(), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=False),
    sa.Column('meeting_speaker_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['meeting_speaker_id'], ['meeting_speakers.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['result_id'], ['results.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('result_id', 'label', name='uq_diarization_speaker_result_label')
    )
    op.create_index(op.f('ix_diarization_speakers_meeting_speaker_id'), 'diarization_speakers', ['meeting_speaker_id'], unique=False)
    op.create_index(op.f('ix_diarization_speakers_result_id'), 'diarization_speakers', ['result_id'], unique=False)
    op.create_table('meeting_result_sources',
    sa.Column('meeting_result_id', sa.Uuid(), nullable=False),
    sa.Column('result_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['meeting_result_id'], ['meeting_results.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['result_id'], ['results.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('meeting_result_id', 'result_id')
    )
    op.create_table('processing_jobs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('result_id', sa.Uuid(), nullable=True),
    sa.Column('meeting_result_id', sa.Uuid(), nullable=True),
    sa.Column('stage', sa.Enum('preprocess', 'transcription', 'diarization', 'alignment', 'meeting_compose', 'cleaning', 'minutes_generation', name='processing_stage', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('model_id', sa.Uuid(), nullable=True),
    sa.Column('integration_id', sa.Uuid(), nullable=True),
    sa.Column('priority', sa.Integer(), server_default='100', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint('(result_id IS NOT NULL AND meeting_result_id IS NULL) OR (result_id IS NULL AND meeting_result_id IS NOT NULL)', name='ck_processing_job_exactly_one_target'),
    sa.CheckConstraint('priority >= 0', name='ck_processing_job_priority_non_negative'),
    sa.ForeignKeyConstraint(['integration_id'], ['external_integrations.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['meeting_result_id'], ['meeting_results.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['model_id'], ['model_definitions.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['result_id'], ['results.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_processing_jobs_created_at'), 'processing_jobs', ['created_at'], unique=False)
    op.create_index(op.f('ix_processing_jobs_integration_id'), 'processing_jobs', ['integration_id'], unique=False)
    op.create_index(op.f('ix_processing_jobs_meeting_result_id'), 'processing_jobs', ['meeting_result_id'], unique=False)
    op.create_index(op.f('ix_processing_jobs_model_id'), 'processing_jobs', ['model_id'], unique=False)
    op.create_index(op.f('ix_processing_jobs_result_id'), 'processing_jobs', ['result_id'], unique=False)
    op.create_index(op.f('ix_processing_jobs_stage'), 'processing_jobs', ['stage'], unique=False)
    op.create_table('meeting_result_artifacts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('meeting_result_id', sa.Uuid(), nullable=False),
    sa.Column('artifact_type', sa.Enum('combined_transcript_json', 'cleaned_transcript', 'minutes', 'summary', 'key_points', 'action_items', 'mcp_response', 'other', name='meeting_artifact_type', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('minio_bucket', sa.String(length=63), nullable=False),
    sa.Column('minio_key', sa.String(length=1024), nullable=False),
    sa.Column('content_type', sa.String(length=255), nullable=True),
    sa.Column('checksum_sha256', sa.String(length=64), nullable=True),
    sa.Column('producer_job_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['meeting_result_id'], ['meeting_results.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['producer_job_id'], ['processing_jobs.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('minio_bucket', 'minio_key', name='uq_meeting_result_artifact_minio_object')
    )
    op.create_index(op.f('ix_meeting_result_artifacts_artifact_type'), 'meeting_result_artifacts', ['artifact_type'], unique=False)
    op.create_index(op.f('ix_meeting_result_artifacts_meeting_result_id'), 'meeting_result_artifacts', ['meeting_result_id'], unique=False)
    op.create_index(op.f('ix_meeting_result_artifacts_producer_job_id'), 'meeting_result_artifacts', ['producer_job_id'], unique=False)
    op.create_table('processing_attempts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('attempt_number', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('queued', 'running', 'succeeded', 'failed', 'cancelled', name='processing_attempt_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('queue_task_id', sa.String(length=255), nullable=True),
    sa.Column('worker_name', sa.String(length=255), nullable=True),
    sa.Column('external_request_id', sa.String(length=255), nullable=True),
    sa.Column('error_code', sa.String(length=100), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('queued_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('attempt_number >= 1', name='ck_processing_attempt_number_positive'),
    sa.ForeignKeyConstraint(['job_id'], ['processing_jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id', 'attempt_number', name='uq_processing_attempt_number')
    )
    op.create_index(op.f('ix_processing_attempts_job_id'), 'processing_attempts', ['job_id'], unique=False)
    op.create_index(op.f('ix_processing_attempts_queue_task_id'), 'processing_attempts', ['queue_task_id'], unique=True)
    op.create_index(op.f('ix_processing_attempts_queued_at'), 'processing_attempts', ['queued_at'], unique=False)
    op.create_index(op.f('ix_processing_attempts_status'), 'processing_attempts', ['status'], unique=False)
    op.create_table('processing_job_dependencies',
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('depends_on_job_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('job_id <> depends_on_job_id', name='ck_processing_job_no_self_dependency'),
    sa.ForeignKeyConstraint(['depends_on_job_id'], ['processing_jobs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['job_id'], ['processing_jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('job_id', 'depends_on_job_id')
    )
    op.create_table('processing_job_parameters',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('value_type', sa.Enum('string', 'integer', 'float', 'boolean', 'json', name='processing_parameter_type', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['processing_jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id', 'name', name='uq_processing_job_parameter_name')
    )
    op.create_index(op.f('ix_processing_job_parameters_job_id'), 'processing_job_parameters', ['job_id'], unique=False)
    op.create_table('result_artifacts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('result_id', sa.Uuid(), nullable=False),
    sa.Column('artifact_type', sa.Enum('transcript_json', 'metadata', 'cleaned_text', 'other', 'normalized_audio', 'raw_text', 'word_timestamps_json', 'diarization_json', 'aligned_transcript_json', name='result_artifact_type', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('minio_bucket', sa.String(length=63), nullable=False),
    sa.Column('minio_key', sa.String(length=1024), nullable=False),
    sa.Column('content_type', sa.String(length=255), nullable=True),
    sa.Column('checksum_sha256', sa.String(length=64), nullable=True),
    sa.Column('producer_job_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['producer_job_id'], ['processing_jobs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['result_id'], ['results.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('minio_bucket', 'minio_key', name='uq_result_artifact_minio_object')
    )
    op.create_index(op.f('ix_result_artifacts_artifact_type'), 'result_artifacts', ['artifact_type'], unique=False)
    op.create_index(op.f('ix_result_artifacts_producer_job_id'), 'result_artifacts', ['producer_job_id'], unique=False)
    op.create_index(op.f('ix_result_artifacts_result_id'), 'result_artifacts', ['result_id'], unique=False)
    op.create_table('history_affected_meeting_result_artifacts',
    sa.Column('history_id', sa.Uuid(), nullable=False),
    sa.Column('meeting_result_artifact_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['history_id'], ['history.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['meeting_result_artifact_id'], ['meeting_result_artifacts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('history_id', 'meeting_result_artifact_id')
    )
    op.create_table('history_affected_result_artifacts',
    sa.Column('history_id', sa.Uuid(), nullable=False),
    sa.Column('result_artifact_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['history_id'], ['history.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['result_artifact_id'], ['result_artifacts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('history_id', 'result_artifact_id')
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('history_affected_result_artifacts')
    op.drop_table('history_affected_meeting_result_artifacts')
    op.drop_index(op.f('ix_result_artifacts_result_id'), table_name='result_artifacts')
    op.drop_index(op.f('ix_result_artifacts_producer_job_id'), table_name='result_artifacts')
    op.drop_index(op.f('ix_result_artifacts_artifact_type'), table_name='result_artifacts')
    op.drop_table('result_artifacts')
    op.drop_index(op.f('ix_processing_job_parameters_job_id'), table_name='processing_job_parameters')
    op.drop_table('processing_job_parameters')
    op.drop_table('processing_job_dependencies')
    op.drop_index(op.f('ix_processing_attempts_status'), table_name='processing_attempts')
    op.drop_index(op.f('ix_processing_attempts_queued_at'), table_name='processing_attempts')
    op.drop_index(op.f('ix_processing_attempts_queue_task_id'), table_name='processing_attempts')
    op.drop_index(op.f('ix_processing_attempts_job_id'), table_name='processing_attempts')
    op.drop_table('processing_attempts')
    op.drop_index(op.f('ix_meeting_result_artifacts_producer_job_id'), table_name='meeting_result_artifacts')
    op.drop_index(op.f('ix_meeting_result_artifacts_meeting_result_id'), table_name='meeting_result_artifacts')
    op.drop_index(op.f('ix_meeting_result_artifacts_artifact_type'), table_name='meeting_result_artifacts')
    op.drop_table('meeting_result_artifacts')
    op.drop_index(op.f('ix_processing_jobs_stage'), table_name='processing_jobs')
    op.drop_index(op.f('ix_processing_jobs_result_id'), table_name='processing_jobs')
    op.drop_index(op.f('ix_processing_jobs_model_id'), table_name='processing_jobs')
    op.drop_index(op.f('ix_processing_jobs_meeting_result_id'), table_name='processing_jobs')
    op.drop_index(op.f('ix_processing_jobs_integration_id'), table_name='processing_jobs')
    op.drop_index(op.f('ix_processing_jobs_created_at'), table_name='processing_jobs')
    op.drop_table('processing_jobs')
    op.drop_table('meeting_result_sources')
    op.drop_index(op.f('ix_diarization_speakers_result_id'), table_name='diarization_speakers')
    op.drop_index(op.f('ix_diarization_speakers_meeting_speaker_id'), table_name='diarization_speakers')
    op.drop_table('diarization_speakers')
    op.drop_index(op.f('ix_results_voice_id'), table_name='results')
    op.drop_index(op.f('ix_results_generated_at'), table_name='results')
    op.drop_table('results')
    op.drop_table('history_affected_voices')
    op.drop_table('history_affected_meeting_speakers')
    op.drop_table('history_affected_meeting_results')
    op.drop_index(op.f('ix_voices_uploaded_by_id'), table_name='voices')
    op.drop_index(op.f('ix_voices_status'), table_name='voices')
    op.drop_index(op.f('ix_voices_meeting_id'), table_name='voices')
    op.drop_index(op.f('ix_voices_checksum_sha256'), table_name='voices')
    op.drop_table('voices')
    op.drop_index(op.f('ix_meeting_speakers_meeting_id'), table_name='meeting_speakers')
    op.drop_table('meeting_speakers')
    op.drop_index(op.f('ix_meeting_results_meeting_id'), table_name='meeting_results')
    op.drop_index(op.f('ix_meeting_results_generated_at'), table_name='meeting_results')
    op.drop_table('meeting_results')
    op.drop_table('meeting_members')
    op.drop_table('history_affected_users')
    op.drop_table('history_affected_meetings')
    op.drop_index(op.f('ix_model_definitions_task_type'), table_name='model_definitions')
    op.drop_index(op.f('ix_model_definitions_runtime_id'), table_name='model_definitions')
    op.drop_table('model_definitions')
    op.drop_index(op.f('ix_meetings_owner_id'), table_name='meetings')
    op.drop_table('meetings')
    op.drop_index(op.f('ix_history_request_id'), table_name='history')
    op.drop_index(op.f('ix_history_event_type'), table_name='history')
    op.drop_index(op.f('ix_history_created_at'), table_name='history')
    op.drop_index(op.f('ix_history_actor_user_id'), table_name='history')
    op.drop_table('history')
    op.drop_index(op.f('ix_auth_identities_user_id'), table_name='auth_identities')
    op.drop_index(op.f('ix_auth_identities_provider'), table_name='auth_identities')
    op.drop_table('auth_identities')
    op.drop_index(op.f('ix_users_role'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
    op.drop_table('model_runtimes')
    op.drop_table('external_integrations')
    # ### end Alembic commands ###
