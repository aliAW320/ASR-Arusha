from functools import lru_cache
from typing import Literal
from urllib.parse import quote

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

    rabbitmq_host: str = "rabbitmq"
    rabbitmq_port: int = Field(default=5672, ge=1, le=65535)
    rabbitmq_user: str = "meeting_app"
    rabbitmq_password: SecretStr = SecretStr("change_me")
    rabbitmq_vhost: str = "/"
    rabbitmq_prefetch_count: int = Field(default=1, ge=1, le=100)
    rabbitmq_retry_delay_seconds: float = Field(default=5.0, ge=0, le=3600)
    rabbitmq_asr_queue: str = "asr.queue"
    rabbitmq_diar_queue: str = "diar.queue"
    rabbitmq_cleaning_queue: str = "cleaning.queue"
    rabbitmq_mcp_queue: str = "mcp.queue"
    rabbitmq_dead_letter_queue: str = "processing.dlq"
    processing_cancellation_poll_interval_seconds: float = Field(
        default=0.5, gt=0, le=10
    )

    base_url: str = "https://llm.irdc.arusha.ir/v1"
    transcript_api_key: SecretStr | None = None
    transcript_model_name: str = "whisper-large-v3-persian"
    asr_request_timeout_seconds: int = Field(default=600, ge=30)
    asr_num_beams: int = Field(default=5, ge=1, le=20)
    asr_max_attempts: int = Field(default=3, ge=1, le=10)
    asr_worker_name: str = "asr-worker"

    huggingface_token: SecretStr | None = None
    diarization_model_name: str = Field(
        default="pyannote/speaker-diarization-community-1",
        validation_alias=AliasChoices("DIARIZATION_MODEL_NAME", "DIARIZATION_MODEL"),
    )
    diarization_model_version: str = "3.1"
    diarization_device: Literal["cpu", "cuda"] = "cpu"
    diarization_max_attempts: int = Field(default=3, ge=1, le=10)
    diarization_worker_name: str = "diarization-worker"

    cleaner_model_name: str = "openai/Qwen3.8-27B"
    cleaner_request_timeout_seconds: int = Field(default=900, ge=30)
    cleaner_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    cleaner_max_attempts: int = Field(default=3, ge=1, le=10)
    cleaner_worker_name: str = "cleaner-worker"
    cleaner_chunk_max_chars: int = Field(default=12_000, ge=1000)
    cleaner_overlap_min_segments: int = Field(default=1, ge=0, le=20)
    cleaner_overlap_max_segments: int = Field(default=5, ge=0, le=20)

    meeting_composer_max_attempts: int = Field(default=3, ge=1, le=10)
    meeting_composer_poll_interval_seconds: float = Field(default=2.0, gt=0)
    meeting_composer_worker_name: str = "meeting-composer"
    meeting_composer_source_policy: Literal["cleaned_required", "cleaned_or_aligned"] = (
        "cleaned_required"
    )
    meeting_composer_gap_ms: int = Field(default=0, ge=0)
    meeting_composer_max_input_bytes: int = Field(default=104_857_600, ge=1)
    meeting_composer_max_segments: int = Field(default=500_000, ge=1)

    @property
    def json_logs_enabled(self) -> bool:
        if self.log_format == "json":
            return True
        if self.log_format == "console":
            return False
        return self.app_env != "development"

    @property
    def rabbitmq_url(self) -> str:
        user = quote(self.rabbitmq_user, safe="")
        password = quote(self.rabbitmq_password.get_secret_value(), safe="")
        vhost = "%2F" if self.rabbitmq_vhost == "/" else quote(self.rabbitmq_vhost, safe="")
        return f"amqp://{user}:{password}@{self.rabbitmq_host}:{self.rabbitmq_port}/{vhost}"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
