import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from .models import MeetingMemberRole, UserRole, VoiceStatus


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, min_length=1, max_length=150)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


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
