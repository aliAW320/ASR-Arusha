import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class VoiceStatus(str, enum.Enum):
    WAITING = "waiting"
    PENDING = "pending"
    FINISHED = "finished"
    ERROR = "error"


class MeetingMemberRole(str, enum.Enum):
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


class ResultArtifactType(str, enum.Enum):
    TRANSCRIPT_JSON = "transcript_json"
    METADATA = "metadata"
    CLEANED_TEXT = "cleaned_text"
    OTHER = "other"


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


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    owned_meetings: Mapped[list["Meeting"]] = relationship(back_populates="owner")
    meeting_memberships: Mapped[list["MeetingMember"]] = relationship(
        back_populates="user",
        passive_deletes=True,
    )
    uploaded_voices: Mapped[list["VoiceFile"]] = relationship(
        back_populates="uploaded_by"
    )
    history_events: Mapped[list["History"]] = relationship(back_populates="actor")
    affected_history_events: Mapped[list["History"]] = relationship(
        secondary=history_affected_users,
        back_populates="affected_users",
    )


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    meeting_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )

    owner: Mapped["User"] = relationship(back_populates="owned_meetings")
    memberships: Mapped[list["MeetingMember"]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    voices: Mapped[list["VoiceFile"]] = relationship(
        back_populates="meeting",
        passive_deletes=True,
    )
    results: Mapped[list["Result"]] = relationship(
        secondary="voices",
        primaryjoin="Meeting.id == VoiceFile.meeting_id",
        secondaryjoin="VoiceFile.id == Result.voice_id",
        viewonly=True,
    )
    history_events: Mapped[list["History"]] = relationship(
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
        SqlEnum(
            MeetingMemberRole,
            name="meeting_member_role",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum_class: [item.value for item in enum_class],
        ),
        nullable=False,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    meeting: Mapped["Meeting"] = relationship(back_populates="memberships")
    user: Mapped["User"] = relationship(back_populates="meeting_memberships")


class VoiceFile(Base):
    __tablename__ = "voices"
    __table_args__ = (
        UniqueConstraint("minio_bucket", "minio_key", name="uq_voice_minio_object"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    minio_bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    minio_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[VoiceStatus] = mapped_column(
        SqlEnum(
            VoiceStatus,
            name="voice_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum_class: [item.value for item in enum_class],
        ),
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
        DateTime(timezone=True), server_default=func.now()
    )
    uploaded_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    meeting: Mapped["Meeting | None"] = relationship(back_populates="voices")
    uploaded_by: Mapped["User"] = relationship(back_populates="uploaded_voices")
    result: Mapped["Result | None"] = relationship(
        back_populates="voice",
        cascade="all, delete-orphan",
        passive_deletes=True,
        single_parent=True,
    )
    history_events: Mapped[list["History"]] = relationship(
        secondary=history_affected_voices,
        back_populates="affected_voices",
    )


class Result(Base):
    __tablename__ = "results"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    voice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("voices.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    voice: Mapped["VoiceFile"] = relationship(back_populates="result")
    artifacts: Mapped[list["ResultArtifact"]] = relationship(
        back_populates="result",
        cascade="all, delete-orphan",
        passive_deletes=True,
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
        SqlEnum(
            ResultArtifactType,
            name="result_artifact_type",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum_class: [item.value for item in enum_class],
        ),
        default=ResultArtifactType.TRANSCRIPT_JSON,
        nullable=False,
    )
    minio_bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    minio_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    result: Mapped["Result"] = relationship(back_populates="artifacts")
    history_events: Mapped[list["History"]] = relationship(
        secondary=history_affected_result_artifacts,
        back_populates="affected_result_artifacts",
    )


class History(Base):
    __tablename__ = "history"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    action_description: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    actor: Mapped["User"] = relationship(back_populates="history_events")
    affected_users: Mapped[list["User"]] = relationship(
        secondary=history_affected_users,
        back_populates="affected_history_events",
    )
    affected_meetings: Mapped[list["Meeting"]] = relationship(
        secondary=history_affected_meetings,
        back_populates="history_events",
    )
    affected_voices: Mapped[list["VoiceFile"]] = relationship(
        secondary=history_affected_voices,
        back_populates="history_events",
    )
    affected_result_artifacts: Mapped[list["ResultArtifact"]] = relationship(
        secondary=history_affected_result_artifacts,
        back_populates="history_events",
    )
