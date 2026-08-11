import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


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
    title: str
    description: str | None = None
    scheduled_time: datetime | None = None
    date : datetime | None = None


class MeetingResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    scheduled_time: datetime | None = None
    date : datetime | None = None
    owner_id: uuid.UUID



