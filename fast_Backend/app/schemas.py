import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from .models import (
    DiarizationDecision,
    MeetingMemberRole,
    MeetingPublicationStatus,
    PublicationVisionStatus,
    UserRole,
    VoiceStatus,
)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, min_length=1, max_length=150)

    # Reject ignored fields such as a client-supplied role.  Keeping request
    # models strict prevents future fields from accidentally becoming a mass
    # assignment path.
    model_config = ConfigDict(extra="forbid")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    model_config = ConfigDict(extra="forbid")


class UserResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str | None
    role: UserRole
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthResponse(TokenResponse):
    user: UserResponse


class CreateMeetingRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    date: datetime | None = None


class UpdateMeetingRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    date: datetime | None = None

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class MeetingResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    date: datetime
    owner_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AddMeetingMemberRequest(BaseModel):
    user_id: uuid.UUID | None = None
    email: EmailStr | None = None
    role: MeetingMemberRole = MeetingMemberRole.VIEWER

    @model_validator(mode="after")
    def validate_identity_and_role(self):
        if (self.user_id is None) == (self.email is None):
            raise ValueError("Provide exactly one of user_id or email")
        if self.role == MeetingMemberRole.OWNER:
            raise ValueError("Owner membership is managed by the meeting")
        return self


class UpdateMeetingMemberRequest(BaseModel):
    role: MeetingMemberRole

    @model_validator(mode="after")
    def prevent_owner_assignment(self):
        if self.role == MeetingMemberRole.OWNER:
            raise ValueError("Owner membership is managed by the meeting")
        return self


class MeetingMemberResponse(BaseModel):
    user: UserResponse
    role: MeetingMemberRole
    added_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class VoiceResponse(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID | None
    minio_bucket: str
    minio_key: str
    original_filename: str | None
    content_type: str | None
    size_bytes: int | None
    checksum_sha256: str | None
    sequence_number: int | None
    status: VoiceStatus
    diarization_decision: DiarizationDecision
    uploaded_by_id: uuid.UUID
    uploaded_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HistoryResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    action_description: str
    actor_user_id: uuid.UUID | None
    event_data: dict | None
    request_id: str | None
    correlation_id: str | None
    ip_address: str | None
    created_at: datetime
    affected_user_ids: list[uuid.UUID] = Field(default_factory=list)
    affected_meeting_ids: list[uuid.UUID] = Field(default_factory=list)
    affected_voice_ids: list[uuid.UUID] = Field(default_factory=list)
    affected_result_artifact_ids: list[uuid.UUID] = Field(default_factory=list)
    affected_meeting_result_ids: list[uuid.UUID] = Field(default_factory=list)
    affected_meeting_result_artifact_ids: list[uuid.UUID] = Field(default_factory=list)
    affected_meeting_speaker_ids: list[uuid.UUID] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ProcessingAttemptResponse(BaseModel):
    id: uuid.UUID
    attempt_number: int
    status: str
    worker_name: str | None
    external_request_id: str | None
    error_code: str | None
    error_message: str | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class ProcessingJobResponse(BaseModel):
    id: uuid.UUID
    target_type: str
    result_id: uuid.UUID | None
    voice_id: uuid.UUID | None
    meeting_id: uuid.UUID | None
    meeting_result_id: uuid.UUID | None
    stage: str
    status: str
    model_name: str | None
    created_at: datetime
    attempts: list[ProcessingAttemptResponse]


class ResultArtifactResponse(BaseModel):
    id: uuid.UUID
    artifact_type: str
    minio_bucket: str
    minio_key: str
    content_type: str | None
    checksum_sha256: str | None
    producer_job_id: uuid.UUID | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ResultResponse(BaseModel):
    id: uuid.UUID
    voice_id: uuid.UUID
    generated_at: datetime
    completed_at: datetime | None
    artifacts: list[ResultArtifactResponse]

    model_config = ConfigDict(from_attributes=True)


class TranscriptResponse(BaseModel):
    result_id: uuid.UUID
    schema_version: str
    language: str
    text: str
    source_id: str | None = None
    model: str | None = None
    diarization_model: str | None = None
    cleaner_model: str | None = None
    words: list[dict] = Field(default_factory=list)
    segments: list[dict]
    metrics: dict
    processing_status: str
    processing_error: dict | None = None


class ComposeMeetingRequest(BaseModel):
    result_ids: list[uuid.UUID] | None = None
    allow_aligned_fallback: bool = False
    force_new_version: bool = False

    model_config = ConfigDict(extra="forbid")


class MeetingResultSourceResponse(BaseModel):
    position: int
    voice_id: uuid.UUID
    result_id: uuid.UUID
    source_artifact_id: uuid.UUID
    voice_sequence_snapshot: int | None
    source_offset_ms: int
    source_duration_ms: int

    model_config = ConfigDict(from_attributes=True)


class MeetingResultArtifactResponse(BaseModel):
    id: uuid.UUID
    artifact_type: str
    minio_bucket: str
    minio_key: str
    content_type: str | None
    checksum_sha256: str | None
    producer_job_id: uuid.UUID | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MeetingResultResponse(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    schema_version: str
    source_fingerprint: str
    generated_at: datetime
    completed_at: datetime | None
    sources: list[MeetingResultSourceResponse]
    artifacts: list[MeetingResultArtifactResponse]

    model_config = ConfigDict(from_attributes=True)


class MeetingTranscriptResponse(BaseModel):
    schema_version: str = Field(alias="schema")
    meeting_id: str
    meeting_result_id: str
    generated_at: str
    duration_ms: int
    source_count: int
    speaker_count: int
    text: str
    sources: list[dict]
    speakers: list[dict]
    segments: list[dict]
    processing_status: str
    processing_error: dict | None = None

    model_config = ConfigDict(populate_by_name=True)


class MeetingAttachmentResponse(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    uploaded_by_id: uuid.UUID
    original_filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UpdateMeetingPublicationRequest(BaseModel):
    destination_path: str = Field(min_length=1, max_length=1024)

    model_config = ConfigDict(extra="forbid")


class MeetingPublicationResponse(BaseModel):
    id: uuid.UUID | None = None
    meeting_id: uuid.UUID
    meeting_result_id: uuid.UUID | None = None
    current_job_id: uuid.UUID | None = None
    status: MeetingPublicationStatus
    destination_path: str
    attachment_ids: list[str] = Field(default_factory=list)
    vision_status: PublicationVisionStatus = PublicationVisionStatus.NOT_REQUESTED
    outline_parent_document_id: str | None = None
    outline_summary_document_id: str | None = None
    outline_transcript_document_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class DiarizationAvailabilityResponse(BaseModel):
    """Whether a diarization worker is alive right now."""

    available: bool
    enabled: bool
    last_seen_at: datetime | None = None


class DiarizationChoiceRequest(BaseModel):
    enabled: bool

    model_config = ConfigDict(extra="forbid")


class MeetingUploadResponse(BaseModel):
    """What one multi-file meeting upload produced, split by what it became."""

    voices: list[VoiceResponse] = Field(default_factory=list)
    attachments: list[MeetingAttachmentResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)
