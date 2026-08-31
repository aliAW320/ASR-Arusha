from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, EmailStr, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: Literal["development", "test", "production"] = "development"
    service_name: str = "meeting-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["auto", "json", "console"] = "auto"

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
    minio_exports_bucket: str = "transcript-exports"
    voice_upload_max_bytes: int = 500 * 1024 * 1024

    base_url: str = "https://llm.irdc.arusha.ir/v1"
    transcript_api_key: SecretStr | None = None
    transcript_model_name: str = "whisper-large-v3-persian"
    asr_request_timeout_seconds: int = Field(default=600, ge=30)
    asr_max_attempts: int = Field(default=3, ge=1, le=10)
    asr_poll_interval_seconds: float = Field(default=2.0, gt=0)
    asr_worker_name: str = "asr-worker"

    huggingface_token: SecretStr | None = None
    diarization_model_name: str = Field(
        default="pyannote/speaker-diarization-3.1",
        validation_alias=AliasChoices("DIARIZATION_MODEL_NAME", "DIARIZATION_MODEL"),
    )
    diarization_model_version: str = "3.1"
    diarization_device: Literal["cpu", "cuda"] = "cpu"
    diarization_max_attempts: int = Field(default=3, ge=1, le=10)
    diarization_poll_interval_seconds: float = Field(default=2.0, gt=0)
    diarization_worker_name: str = "diarization-worker"

    @property
    def json_logs_enabled(self) -> bool:
        if self.log_format == "json":
            return True
        if self.log_format == "console":
            return False
        return self.app_env != "development"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
