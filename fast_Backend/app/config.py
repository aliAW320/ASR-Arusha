from functools import lru_cache
from typing import Literal

from pydantic import EmailStr, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    jwt_secret_key: SecretStr = Field(min_length=32)
    jwt_algorithm: Literal["HS256"] = "HS256"
    access_token_expire_minutes: int = 30
    admin_email: EmailStr | None = None
    admin_password: SecretStr | None = None
    admin_full_name: str | None = None

    minio_endpoint: str = "minio:9000"
    minio_root_user: str = "minioadmin"
    minio_root_password: SecretStr = SecretStr("minioadmin")
    minio_secure: bool = False
    minio_meetings_bucket: str = "meetings"
    voice_upload_max_bytes: int = 500 * 1024 * 1024

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
