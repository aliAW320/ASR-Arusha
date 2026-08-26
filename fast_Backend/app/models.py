from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class VoiceStatus(str, enum.Enum):
    """
    Aggregate/business status for a voice file.

    Detailed execution state lives in ProcessingJob/ProcessingAttempt.
    This enum is intentionally kept small because it is primarily useful
    for the UI and high-level workflow.
    """

    WAITING = "waiting"
    PENDING = "pending"
    FINISHED = "finished"
    ERROR = "error"


class MeetingMemberRole(str, enum.Enum):
    OWNER = "owner"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class ResultArtifactType(str, enum.Enum):
    # Existing artifact types
    TRANSCRIPT_JSON = "transcript_json"
    METADATA = "metadata"
    CLEANED_TEXT = "cleaned_text"
    OTHER = "other"

    # Pipeline artifacts
    NORMALIZED_AUDIO = "normalized_audio"
    RAW_TEXT = "raw_text"
    WORD_TIMESTAMPS_JSON = "word_timestamps_json"
    DIARIZATION_JSON = "diarization_json"
    ALIGNED_TRANSCRIPT_JSON = "aligned_transcript_json"


class MeetingArtifactType(str, enum.Enum):
    COMBINED_TRANSCRIPT_JSON = "combined_transcript_json"
    CLEANED_TRANSCRIPT = "cleaned_transcript"
    MINUTES = "minutes"
    SUMMARY = "summary"
    KEY_POINTS = "key_points"
    ACTION_ITEMS = "action_items"
    MCP_RESPONSE = "mcp_response"
    OTHER = "other"


class ProcessingStage(str, enum.Enum):
    PREPROCESS = "preprocess"
    TRANSCRIPTION = "transcription"
    DIARIZATION = "diarization"
    ALIGNMENT = "alignment"
    MEETING_COMPOSE = "meeting_compose"
    CLEANING = "cleaning"
    MINUTES_GENERATION = "minutes_generation"


class ProcessingAttemptStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProcessingParameterType(str, enum.Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    JSON = "json"


class ModelRuntimeKind(str, enum.Enum):
    LOCAL = "local"
    REMOTE = "remote"


class ModelTaskType(str, enum.Enum):
    TRANSCRIPTION = "transcription"
    DIARIZATION = "diarization"
    CLEANING = "cleaning"
    EMBEDDING = "embedding"
    MINUTES_GENERATION = "minutes_generation"
    OTHER = "other"


class ExternalIntegrationKind(str, enum.Enum):
    MCP = "mcp"
    HTTP = "http"
    GRPC = "grpc"
    OTHER = "other"


def _enum_type(enum_class: type[enum.Enum], name: str) -> SqlEnum:
    """Create a portable string-backed SQLAlchemy enum with a DB CHECK constraint."""
    return SqlEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda cls: [item.value for item in cls],
    )


# ---------------------------------------------------------------------------
# Association tables
# ---------------------------------------------------------------------------


history_affected_users = Table(
    "history_affected_users",
    Base.metadata,
    Column(
        "history_id",
        Uuid,
        ForeignKey("history.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "user_id",
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


history_affected_meetings = Table(
    "history_affected_meetings",
    Base.metadata,
    Column(
        "history_id",
        Uuid,
        ForeignKey("history.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "meeting_id",
        Uuid,
        ForeignKey("meetings.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


history_affected_voices = Table(
    "history_affected_voices",
    Base.metadata,
    Column(
        "history_id",
        Uuid,
        ForeignKey("history.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "voice_id",
        Uuid,
        ForeignKey("voices.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


history_affected_result_artifacts = Table(
    "history_affected_result_artifacts",
    Base.metadata,
    Column(
        "history_id",
        Uuid,
        ForeignKey("history.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "result_artifact_id",
        Uuid,
        ForeignKey("result_artifacts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


history_affected_meeting_results = Table(
    "history_affected_meeting_results",
    Base.metadata,
    Column(
        "history_id",
        Uuid,
        ForeignKey("history.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "meeting_result_id",
        Uuid,
        ForeignKey("meeting_results.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


history_affected_meeting_result_artifacts = Table(
    "history_affected_meeting_result_artifacts",
    Base.metadata,
    Column(
        "history_id",
        Uuid,
        ForeignKey("history.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "meeting_result_artifact_id",
        Uuid,
        ForeignKey("meeting_result_artifacts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


history_affected_meeting_speakers = Table(
    "history_affected_meeting_speakers",
    Base.metadata,
    Column(
        "history_id",
        Uuid,
        ForeignKey("history.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "meeting_speaker_id",
        Uuid,
        ForeignKey("meeting_speakers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


meeting_result_sources = Table(
    "meeting_result_sources",
    Base.metadata,
    Column(
        "meeting_result_id",
        Uuid,
        ForeignKey("meeting_results.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "result_id",
        Uuid,
        ForeignKey("results.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


processing_job_dependencies = Table(
    "processing_job_dependencies",
    Base.metadata,
    Column(
        "job_id",
        Uuid,
        ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "depends_on_job_id",
        Uuid,
        ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    CheckConstraint(
        "job_id <> depends_on_job_id",
        name="ck_processing_job_no_self_dependency",
    ),
)


# ---------------------------------------------------------------------------
# Users / meetings / memberships
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        _enum_type(UserRole, "user_role"),
        default=UserRole.USER,
        server_default=UserRole.USER.value,
        nullable=False,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    owned_meetings: Mapped[list[Meeting]] = relationship(back_populates="owner")
    meeting_memberships: Mapped[list[MeetingMember]] = relationship(
        back_populates="user",
        passive_deletes=True,
    )
    uploaded_voices: Mapped[list[VoiceFile]] = relationship(
        back_populates="uploaded_by"
    )
    history_events: Mapped[list[History]] = relationship(back_populates="actor")
    affected_history_events: Mapped[list[History]] = relationship(
        secondary=history_affected_users,
        back_populates="affected_users",
    )
    auth_identities: Mapped[list[AuthIdentity]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AuthIdentity(Base):
    """Authentication identity kept separate from the domain user account.

    The local provider stores a password hash. A future central provider can
    map its stable subject to the same User without changing domain relations.
    """

    __tablename__ = "auth_identities"
    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_auth_identity_subject"),
        UniqueConstraint("user_id", "provider", name="uq_auth_identity_user_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(320), nullable=False)
    secret_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="auth_identities")


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    meeting_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    owner: Mapped[User] = relationship(back_populates="owned_meetings")
    memberships: Mapped[list[MeetingMember]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    voices: Mapped[list[VoiceFile]] = relationship(
        back_populates="meeting",
        passive_deletes=True,
        order_by="VoiceFile.sequence_number",
    )

    # Preserved convenience relationship: all voice-level Result rows that
    # belong to voices currently attached to this meeting.
    results: Mapped[list[Result]] = relationship(
        secondary="voices",
        primaryjoin="Meeting.id == VoiceFile.meeting_id",
        secondaryjoin="VoiceFile.id == Result.voice_id",
        viewonly=True,
    )

    meeting_results: Mapped[list[MeetingResult]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="MeetingResult.generated_at",
    )
    speakers: Mapped[list[MeetingSpeaker]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    history_events: Mapped[list[History]] = relationship(
        secondary=history_affected_meetings,
        back_populates="affected_meetings",
    )


class MeetingMember(Base):
    __tablename__ = "meeting_members"

    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[MeetingMemberRole] = mapped_column(
        _enum_type(MeetingMemberRole, "meeting_member_role"),
        nullable=False,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    meeting: Mapped[Meeting] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="meeting_memberships")


# ---------------------------------------------------------------------------
# Voice files
# ---------------------------------------------------------------------------


class VoiceFile(Base):
    __tablename__ = "voices"
    __table_args__ = (
        UniqueConstraint(
            "minio_bucket",
            "minio_key",
            name="uq_voice_minio_object",
        ),
        UniqueConstraint(
            "meeting_id",
            "sequence_number",
            name="uq_voice_meeting_sequence",
        ),
        CheckConstraint(
            "sequence_number IS NULL OR sequence_number >= 0",
            name="ck_voice_sequence_non_negative",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_voice_size_non_negative",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_voice_duration_non_negative",
        ),
        CheckConstraint(
            "sample_rate_hz IS NULL OR sample_rate_hz > 0",
            name="ck_voice_sample_rate_positive",
        ),
        CheckConstraint(
            "channels IS NULL OR channels > 0",
            name="ck_voice_channels_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Object storage location
    minio_bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    minio_key: Mapped[str] = mapped_column(String(1024), nullable=False)

    # Original upload metadata
    original_filename: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )
    content_type: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    size_bytes: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    checksum_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    # Media metadata
    duration_ms: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    codec: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    sample_rate_hz: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    channels: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Ordering inside a meeting; meaningful only while meeting_id is non-null.
    sequence_number: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    status: Mapped[VoiceStatus] = mapped_column(
        _enum_type(VoiceStatus, "voice_status"),
        default=VoiceStatus.WAITING,
        server_default=VoiceStatus.WAITING.value,
        nullable=False,
        index=True,
    )

    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    uploaded_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    meeting: Mapped[Meeting | None] = relationship(back_populates="voices")
    uploaded_by: Mapped[User] = relationship(back_populates="uploaded_voices")

    # A voice may be reprocessed multiple times. Each Result row is one
    # immutable logical processing run/version for that source voice.
    results: Mapped[list[Result]] = relationship(
        back_populates="voice",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Result.generated_at",
    )

    history_events: Mapped[list[History]] = relationship(
        secondary=history_affected_voices,
        back_populates="affected_voices",
    )

    @property
    def result(self) -> Result | None:
        """
        Backwards-compatible read-only alias for the latest Result.

        The database relation is intentionally one-to-many so replacing a
        model or reprocessing a voice does not overwrite historical results.
        """
        return self.results[-1] if self.results else None


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------


class ModelRuntime(Base):
    """
    Execution runtime/backend for a model.

    Examples:
      - faster-whisper
      - pyannote.audio
      - transformers
      - openai-compatible-gateway
    """

    __tablename__ = "model_runtimes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
        unique=True,
    )
    kind: Mapped[ModelRuntimeKind] = mapped_column(
        _enum_type(ModelRuntimeKind, "model_runtime_kind"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    models: Mapped[list[ModelDefinition]] = relationship(
        back_populates="runtime",
        passive_deletes=True,
    )


class ModelDefinition(Base):
    """
    Immutable identity of a model/revision.

    When weights or behavior change, create a new row instead of mutating the
    old one. That keeps old ProcessingJob rows reproducible without copying
    model_name/model_version into every job.
    """

    __tablename__ = "model_definitions"
    __table_args__ = (
        UniqueConstraint(
            "runtime_id",
            "task_type",
            "name",
            "version",
            name="uq_model_definition_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    runtime_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_runtimes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    task_type: Mapped[ModelTaskType] = mapped_column(
        _enum_type(ModelTaskType, "model_task_type"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    source_uri: Mapped[str | None] = mapped_column(
        String(2048),
        nullable=True,
    )
    checksum_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    runtime: Mapped[ModelRuntime] = relationship(back_populates="models")
    processing_jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="model",
        passive_deletes=True,
    )


# ---------------------------------------------------------------------------
# External integrations
# ---------------------------------------------------------------------------


class ExternalIntegration(Base):
    """
    Versioned external integration definition.

    Credentials should not be persisted here. Keep secrets in the deployment
    secret store/environment and resolve them by integration identity.
    """

    __tablename__ = "external_integrations"
    __table_args__ = (
        UniqueConstraint(
            "name",
            "version",
            name="uq_external_integration_name_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[ExternalIntegrationKind] = mapped_column(
        _enum_type(ExternalIntegrationKind, "external_integration_kind"),
        nullable=False,
    )
    endpoint: Mapped[str | None] = mapped_column(
        String(2048),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    processing_jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="integration",
        passive_deletes=True,
    )


# ---------------------------------------------------------------------------
# Per-voice processing results
# ---------------------------------------------------------------------------


class Result(Base):
    """
    One logical processing run/version for one VoiceFile.

    IMPORTANT:
    voice_id is intentionally NOT unique. Reprocessing a voice with a new
    Whisper/Pyannote/cleaner version creates a new Result row instead of
    overwriting or duplicating provenance fields.
    """

    __tablename__ = "results"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    voice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("voices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    voice: Mapped[VoiceFile] = relationship(back_populates="results")

    artifacts: Mapped[list[ResultArtifact]] = relationship(
        back_populates="result",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    processing_jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="result",
        passive_deletes=True,
    )

    diarization_speakers: Mapped[list[DiarizationSpeaker]] = relationship(
        back_populates="result",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    meeting_results: Mapped[list[MeetingResult]] = relationship(
        secondary=meeting_result_sources,
        back_populates="source_results",
        viewonly=True,
    )


class ResultArtifact(Base):
    __tablename__ = "result_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "minio_bucket",
            "minio_key",
            name="uq_result_artifact_minio_object",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[ResultArtifactType] = mapped_column(
        _enum_type(ResultArtifactType, "result_artifact_type"),
        default=ResultArtifactType.TRANSCRIPT_JSON,
        nullable=False,
        index=True,
    )
    minio_bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    minio_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    checksum_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    # Lineage only: which processing job produced this artifact.
    # The job itself already points to the Result, so no model/provider fields
    # are duplicated here.
    producer_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    result: Mapped[Result] = relationship(back_populates="artifacts")
    producer_job: Mapped[ProcessingJob | None] = relationship(
        foreign_keys=[producer_job_id],
        back_populates="result_artifacts",
    )
    history_events: Mapped[list[History]] = relationship(
        secondary=history_affected_result_artifacts,
        back_populates="affected_result_artifacts",
    )


# ---------------------------------------------------------------------------
# Meeting-level results
# ---------------------------------------------------------------------------


class MeetingResult(Base):
    """
    One meeting-level processing/generation run.

    This is separate from Result because Result belongs to a single VoiceFile,
    while combined transcript/minutes/key-points belong to the Meeting.

    source_results records exactly which per-voice Result versions were used.
    """

    __tablename__ = "meeting_results"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    meeting: Mapped[Meeting] = relationship(back_populates="meeting_results")

    source_results: Mapped[list[Result]] = relationship(
        secondary=meeting_result_sources,
        back_populates="meeting_results",
    )

    artifacts: Mapped[list[MeetingResultArtifact]] = relationship(
        back_populates="meeting_result",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    processing_jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="meeting_result",
        passive_deletes=True,
    )

    history_events: Mapped[list[History]] = relationship(
        secondary=history_affected_meeting_results,
        back_populates="affected_meeting_results",
    )


class MeetingResultArtifact(Base):
    __tablename__ = "meeting_result_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "minio_bucket",
            "minio_key",
            name="uq_meeting_result_artifact_minio_object",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    meeting_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[MeetingArtifactType] = mapped_column(
        _enum_type(MeetingArtifactType, "meeting_artifact_type"),
        nullable=False,
        index=True,
    )
    minio_bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    minio_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    checksum_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    producer_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    meeting_result: Mapped[MeetingResult] = relationship(
        back_populates="artifacts"
    )
    producer_job: Mapped[ProcessingJob | None] = relationship(
        foreign_keys=[producer_job_id],
        back_populates="meeting_result_artifacts",
    )
    history_events: Mapped[list[History]] = relationship(
        secondary=history_affected_meeting_result_artifacts,
        back_populates="affected_meeting_result_artifacts",
    )


# ---------------------------------------------------------------------------
# Speaker identity / diarization mapping
# ---------------------------------------------------------------------------


class MeetingSpeaker(Base):
    """
    Meeting-wide speaker identity.

    A speaker does not have to be a registered User. Multiple diarization
    labels from different voice Results can map to the same MeetingSpeaker.
    """

    __tablename__ = "meeting_speakers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    display_name: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    meeting: Mapped[Meeting] = relationship(back_populates="speakers")
    diarization_speakers: Mapped[list[DiarizationSpeaker]] = relationship(
        back_populates="meeting_speaker",
        passive_deletes=True,
    )
    history_events: Mapped[list[History]] = relationship(
        secondary=history_affected_meeting_speakers,
        back_populates="affected_meeting_speakers",
    )


class DiarizationSpeaker(Base):
    """
    Speaker label emitted by diarization for a specific Result.

    Example:
      Result A: SPEAKER_00 -> MeetingSpeaker "Ali"
      Result B: SPEAKER_02 -> MeetingSpeaker "Ali"

    This avoids assuming that SPEAKER_00 means the same person across files
    or across repeated diarization runs.
    """

    __tablename__ = "diarization_speakers"
    __table_args__ = (
        UniqueConstraint(
            "result_id",
            "label",
            name="uq_diarization_speaker_result_label",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    meeting_speaker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meeting_speakers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    result: Mapped[Result] = relationship(
        back_populates="diarization_speakers"
    )
    meeting_speaker: Mapped[MeetingSpeaker | None] = relationship(
        back_populates="diarization_speakers"
    )


# ---------------------------------------------------------------------------
# Processing DAG / queue execution
# ---------------------------------------------------------------------------


class ProcessingJob(Base):
    """
    One logical pipeline stage.

    A job targets exactly one of:
      1) Result        -> voice-level work
      2) MeetingResult -> meeting-level work

    The XOR CHECK avoids duplicating meeting_id/voice_id on this table.
    Model identity and external integration identity are normalized references.
    Execution retries live in ProcessingAttempt rather than overwriting job
    state.
    """

    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint(
            "("
            "result_id IS NOT NULL AND meeting_result_id IS NULL"
            ") OR ("
            "result_id IS NULL AND meeting_result_id IS NOT NULL"
            ")",
            name="ck_processing_job_exactly_one_target",
        ),
        CheckConstraint(
            "priority >= 0",
            name="ck_processing_job_priority_non_negative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    result_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("results.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    meeting_result_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meeting_results.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    stage: Mapped[ProcessingStage] = mapped_column(
        _enum_type(ProcessingStage, "processing_stage"),
        nullable=False,
        index=True,
    )

    # Nullable because some stages (e.g. FFmpeg preprocess, deterministic
    # alignment) do not require a model.
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_definitions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # Nullable because most jobs are internal. MCP/HTTP/gRPC stages can point
    # to a versioned integration definition.
    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("external_integrations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    priority: Mapped[int] = mapped_column(
        Integer,
        default=100,
        server_default="100",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    result: Mapped[Result | None] = relationship(
        back_populates="processing_jobs",
        foreign_keys=[result_id],
    )
    meeting_result: Mapped[MeetingResult | None] = relationship(
        back_populates="processing_jobs",
        foreign_keys=[meeting_result_id],
    )
    model: Mapped[ModelDefinition | None] = relationship(
        back_populates="processing_jobs"
    )
    integration: Mapped[ExternalIntegration | None] = relationship(
        back_populates="processing_jobs"
    )

    parameters: Mapped[list[ProcessingJobParameter]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    attempts: Mapped[list[ProcessingAttempt]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ProcessingAttempt.attempt_number",
    )

    dependencies: Mapped[list[ProcessingJob]] = relationship(
        "ProcessingJob",
        secondary=processing_job_dependencies,
        primaryjoin=id == processing_job_dependencies.c.job_id,
        secondaryjoin=id == processing_job_dependencies.c.depends_on_job_id,
        back_populates="dependents",
    )
    dependents: Mapped[list[ProcessingJob]] = relationship(
        "ProcessingJob",
        secondary=processing_job_dependencies,
        primaryjoin=id == processing_job_dependencies.c.depends_on_job_id,
        secondaryjoin=id == processing_job_dependencies.c.job_id,
        back_populates="dependencies",
    )

    result_artifacts: Mapped[list[ResultArtifact]] = relationship(
        back_populates="producer_job",
        foreign_keys="ResultArtifact.producer_job_id",
    )
    meeting_result_artifacts: Mapped[list[MeetingResultArtifact]] = relationship(
        back_populates="producer_job",
        foreign_keys="MeetingResultArtifact.producer_job_id",
    )

    @property
    def latest_attempt(self) -> ProcessingAttempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def current_status(self) -> ProcessingAttemptStatus | None:
        attempt = self.latest_attempt
        return attempt.status if attempt else None


class ProcessingJobParameter(Base):
    """
    Per-job execution parameter.

    Values are stored atomically as text plus an explicit type discriminator.
    This avoids a JSON blob on ProcessingJob and keeps each parameter
    independently addressable and normalized.
    """

    __tablename__ = "processing_job_parameters"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "name",
            name="uq_processing_job_parameter_name",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )
    value_type: Mapped[ProcessingParameterType] = mapped_column(
        _enum_type(ProcessingParameterType, "processing_parameter_type"),
        nullable=False,
    )
    value: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    job: Mapped[ProcessingJob] = relationship(back_populates="parameters")


class ProcessingAttempt(Base):
    """
    One execution attempt for a ProcessingJob.

    Retry history is append-only: retrying creates a new row rather than
    overwriting the previous failure.
    """

    __tablename__ = "processing_attempts"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "attempt_number",
            name="uq_processing_attempt_number",
        ),
        CheckConstraint(
            "attempt_number >= 1",
            name="ck_processing_attempt_number_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    status: Mapped[ProcessingAttemptStatus] = mapped_column(
        _enum_type(
            ProcessingAttemptStatus,
            "processing_attempt_status",
        ),
        nullable=False,
        index=True,
    )

    queue_task_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
        index=True,
    )
    worker_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    external_request_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    error_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    job: Mapped[ProcessingJob] = relationship(back_populates="attempts")


# ---------------------------------------------------------------------------
# History / audit trail
# ---------------------------------------------------------------------------


class History(Base):
    __tablename__ = "history"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    action_description: Mapped[str] = mapped_column(Text, nullable=False)
    event_data: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    actor: Mapped[User | None] = relationship(back_populates="history_events")

    affected_users: Mapped[list[User]] = relationship(
        secondary=history_affected_users,
        back_populates="affected_history_events",
    )
    affected_meetings: Mapped[list[Meeting]] = relationship(
        secondary=history_affected_meetings,
        back_populates="history_events",
    )
    affected_voices: Mapped[list[VoiceFile]] = relationship(
        secondary=history_affected_voices,
        back_populates="history_events",
    )
    affected_result_artifacts: Mapped[list[ResultArtifact]] = relationship(
        secondary=history_affected_result_artifacts,
        back_populates="history_events",
    )
    affected_meeting_results: Mapped[list[MeetingResult]] = relationship(
        secondary=history_affected_meeting_results,
        back_populates="history_events",
    )
    affected_meeting_result_artifacts: Mapped[
        list[MeetingResultArtifact]
    ] = relationship(
        secondary=history_affected_meeting_result_artifacts,
        back_populates="history_events",
    )
    affected_meeting_speakers: Mapped[list[MeetingSpeaker]] = relationship(
        secondary=history_affected_meeting_speakers,
        back_populates="history_events",
    )
