import asyncio
import hashlib
import io
import json
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.config import Settings, get_settings
from app.database import SessionFactory, close_database
from app.models import (
    Meeting,
    ProcessingAttempt,
    ProcessingAttemptStatus,
    ProcessingJob,
    ProcessingStage,
    Result,
    ResultArtifact,
    ResultArtifactType,
    VoiceFile,
    VoiceStatus,
)
from app.observability.logging import configure_logging, get_logger
from app.services.audit import add_history_event
from app.services.processing import queue_result_diarization
from app.storage.base import ObjectStorage
from app.storage.minio import get_object_storage

from .canonical import canonical_transcript
from .provider import (
    OpenAICompatibleTranscriptionProvider,
    TranscriptionError,
    TranscriptionResponse,
)


logger = get_logger(__name__)


@dataclass(frozen=True)
class WorkItem:
    attempt_id: uuid.UUID
    attempt_number: int
    job_id: uuid.UUID
    result_id: uuid.UUID
    voice_id: uuid.UUID
    meeting_id: uuid.UUID | None
    source_bucket: str
    source_key: str
    filename: str
    content_type: str
    duration_seconds: float | None
    model_name: str
    base_url: str


ProviderFactory = Callable[[WorkItem], OpenAICompatibleTranscriptionProvider]


class ASRWorker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        settings: Settings,
        provider_factory: ProviderFactory | None = None,
    ):
        self.session_factory = session_factory
        self.storage = storage
        self.settings = settings
        self.worker_name = f"{settings.asr_worker_name}@{socket.gethostname()}"
        self.provider_factory = provider_factory or self._provider

    def _provider(self, item: WorkItem) -> OpenAICompatibleTranscriptionProvider:
        if self.settings.transcript_api_key is None:
            raise TranscriptionError(
                "TRANSCRIPT_API_KEY is not configured",
                code="asr_configuration_error",
                retryable=False,
            )
        return OpenAICompatibleTranscriptionProvider(
            base_url=item.base_url,
            api_key=self.settings.transcript_api_key.get_secret_value(),
            timeout_seconds=self.settings.asr_request_timeout_seconds,
            num_beams=self.settings.asr_num_beams,
        )

    async def claim_next(self) -> uuid.UUID | None:
        async with self.session_factory() as session:
            attempt = await session.scalar(
                select(ProcessingAttempt)
                .join(ProcessingAttempt.job)
                .options(
                    joinedload(ProcessingAttempt.job)
                    .joinedload(ProcessingJob.result)
                    .joinedload(Result.voice)
                )
                .where(
                    ProcessingAttempt.status == ProcessingAttemptStatus.QUEUED,
                    ProcessingJob.stage == ProcessingStage.TRANSCRIPTION,
                )
                .order_by(
                    ProcessingJob.priority.desc(),
                    ProcessingAttempt.queued_at,
                )
                .with_for_update(skip_locked=True, of=ProcessingAttempt)
                .limit(1)
            )
            if attempt is None:
                return None
            attempt.status = ProcessingAttemptStatus.RUNNING
            attempt.started_at = datetime.now(timezone.utc)
            attempt.worker_name = self.worker_name
            voice = attempt.job.result.voice if attempt.job.result else None
            meeting = (
                await session.get(Meeting, voice.meeting_id)
                if voice is not None and voice.meeting_id
                else None
            )
            add_history_event(
                session,
                event_type="processing.started",
                description="ASR transcription started",
                event_data={
                    "job_id": str(attempt.job.id),
                    "attempt_number": attempt.attempt_number,
                    "worker_name": self.worker_name,
                },
                affected_meetings=[meeting] if meeting else [],
                affected_voices=[voice] if voice else [],
            )
            await session.commit()
            return attempt.id

    async def _load_work_item(self, attempt_id: uuid.UUID) -> WorkItem:
        async with self.session_factory() as session:
            attempt = await session.scalar(
                select(ProcessingAttempt)
                .options(
                    joinedload(ProcessingAttempt.job)
                    .joinedload(ProcessingJob.result)
                    .joinedload(Result.voice),
                    joinedload(ProcessingAttempt.job).joinedload(ProcessingJob.model),
                    joinedload(ProcessingAttempt.job).joinedload(ProcessingJob.integration),
                )
                .where(ProcessingAttempt.id == attempt_id)
            )
            if attempt is None or attempt.job.result is None or attempt.job.model is None:
                raise RuntimeError("Queued ASR job is incomplete")
            voice = attempt.job.result.voice
            integration = attempt.job.integration
            return WorkItem(
                attempt_id=attempt.id,
                attempt_number=attempt.attempt_number,
                job_id=attempt.job.id,
                result_id=attempt.job.result.id,
                voice_id=voice.id,
                meeting_id=voice.meeting_id,
                source_bucket=voice.minio_bucket,
                source_key=voice.minio_key,
                filename=voice.original_filename or "voice.bin",
                content_type=voice.content_type or "application/octet-stream",
                duration_seconds=(voice.duration_ms / 1000 if voice.duration_ms else None),
                model_name=attempt.job.model.name,
                base_url=(integration.endpoint if integration and integration.endpoint else self.settings.base_url),
            )

    async def _finish_success(
        self,
        item: WorkItem,
        transcript_payload: bytes,
        raw_text_payload: bytes,
        external_request_id: str | None,
    ) -> None:
        prefix = f"meetings/{item.meeting_id}/results/{item.result_id}"
        uploads = (
            (
                ResultArtifactType.TRANSCRIPT_JSON,
                f"{prefix}/transcript.json",
                transcript_payload,
                "application/json",
            ),
            (
                ResultArtifactType.RAW_TEXT,
                f"{prefix}/raw.txt",
                raw_text_payload,
                "text/plain; charset=utf-8",
            ),
        )
        uploaded: list[tuple[str, str]] = []
        try:
            for _, object_key, payload, content_type in uploads:
                await self.storage.put_object(
                    self.settings.minio_exports_bucket,
                    object_key,
                    io.BytesIO(payload),
                    len(payload),
                    content_type,
                )
                uploaded.append((self.settings.minio_exports_bucket, object_key))

            async with self.session_factory() as session:
                attempt = await session.get(ProcessingAttempt, item.attempt_id)
                job = await session.get(ProcessingJob, item.job_id)
                result = await session.get(Result, item.result_id)
                voice = await session.get(VoiceFile, item.voice_id)
                if attempt is None or job is None or result is None or voice is None:
                    raise RuntimeError("ASR job state disappeared before completion")
                for artifact_type, object_key, payload, content_type in uploads:
                    session.add(
                        ResultArtifact(
                            result=result,
                            artifact_type=artifact_type,
                            minio_bucket=self.settings.minio_exports_bucket,
                            minio_key=object_key,
                            content_type=content_type,
                            checksum_sha256=hashlib.sha256(payload).hexdigest(),
                            producer_job=job,
                        )
                    )
                now = datetime.now(timezone.utc)
                attempt.status = ProcessingAttemptStatus.SUCCEEDED
                attempt.finished_at = now
                attempt.external_request_id = external_request_id
                diarization_job = await queue_result_diarization(
                    session,
                    result,
                    self.settings,
                    depends_on=job,
                )
                voice.status = VoiceStatus.PENDING
                meeting = await session.get(Meeting, voice.meeting_id) if voice.meeting_id else None
                add_history_event(
                    session,
                    event_type="processing.succeeded",
                    description="ASR transcription completed",
                    event_data={
                        "job_id": str(job.id),
                        "attempt_number": attempt.attempt_number,
                        "stage": job.stage.value,
                    },
                    affected_meetings=[meeting] if meeting else [],
                    affected_voices=[voice],
                )
                add_history_event(
                    session,
                    event_type="processing.queued",
                    description="Speaker diarization queued after transcription",
                    event_data={
                        "job_id": str(diarization_job.id),
                        "stage": ProcessingStage.DIARIZATION.value,
                        "depends_on_job_id": str(job.id),
                    },
                    affected_meetings=[meeting] if meeting else [],
                    affected_voices=[voice],
                )
                await session.commit()
        except Exception:
            for bucket, object_key in uploaded:
                try:
                    await self.storage.remove_object(bucket, object_key)
                except Exception as cleanup_error:
                    logger.exception(
                        "asr_artifact_cleanup_failed",
                        "ASR artifact cleanup failed",
                        error=cleanup_error,
                        bucket=bucket,
                        object_key=object_key,
                    )
            raise

    async def _finish_failure(
        self,
        item: WorkItem,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        async with self.session_factory() as session:
            attempt = await session.get(ProcessingAttempt, item.attempt_id)
            job = await session.get(ProcessingJob, item.job_id)
            voice = await session.get(VoiceFile, item.voice_id)
            if attempt is None or job is None or voice is None:
                return
            attempt.status = ProcessingAttemptStatus.FAILED
            attempt.finished_at = datetime.now(timezone.utc)
            attempt.error_code = code[:100]
            attempt.error_message = message[:2000]
            will_retry = retryable and attempt.attempt_number < self.settings.asr_max_attempts
            if will_retry:
                session.add(
                    ProcessingAttempt(
                        job=job,
                        attempt_number=attempt.attempt_number + 1,
                        status=ProcessingAttemptStatus.QUEUED,
                    )
                )
            else:
                voice.status = VoiceStatus.ERROR
            meeting = await session.get(Meeting, voice.meeting_id) if voice.meeting_id else None
            add_history_event(
                session,
                event_type="processing.retry_queued" if will_retry else "processing.failed",
                description=(
                    "ASR transcription retry queued"
                    if will_retry
                    else "ASR transcription failed"
                ),
                event_data={
                    "job_id": str(job.id),
                    "attempt_number": attempt.attempt_number,
                    "error_code": code,
                    "will_retry": will_retry,
                },
                affected_meetings=[meeting] if meeting else [],
                affected_voices=[voice],
            )
            await session.commit()

    async def run_once(self) -> bool:
        attempt_id = await self.claim_next()
        if attempt_id is None:
            return False
        item = await self._load_work_item(attempt_id)
        provider = None
        started = time.monotonic()
        try:
            provider = self.provider_factory(item)
            with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as audio:
                await self.storage.download_object(
                    item.source_bucket,
                    item.source_key,
                    audio,
                )
                response = await provider.transcribe(
                    audio,
                    filename=item.filename,
                    content_type=item.content_type,
                    model=item.model_name,
                )
            elapsed = time.monotonic() - started
            canonical = canonical_transcript(
                response,
                source_id=str(item.voice_id),
                model_name=item.model_name,
                inference_duration_seconds=elapsed,
                audio_duration_seconds=item.duration_seconds,
            )
            transcript_payload = json.dumps(
                canonical, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            await self._finish_success(
                item,
                transcript_payload,
                response.text.encode("utf-8"),
                response.external_request_id,
            )
            logger.info(
                "asr_processing_succeeded",
                "ASR transcription completed",
                job_id=str(item.job_id),
                voice_id=str(item.voice_id),
                attempt_number=item.attempt_number,
                inference_duration_seconds=elapsed,
            )
        except TranscriptionError as error:
            await self._finish_failure(
                item,
                code=error.code,
                message=str(error),
                retryable=error.retryable,
            )
        except Exception as error:
            logger.exception(
                "asr_processing_failed",
                "Unexpected ASR processing failure",
                error=error,
                job_id=str(item.job_id),
                voice_id=str(item.voice_id),
            )
            await self._finish_failure(
                item,
                code="asr_unexpected_error",
                message=str(error),
                retryable=True,
            )
        finally:
            close = getattr(provider, "aclose", None) if provider is not None else None
            if close is not None:
                await close()
        return True


async def run_forever() -> None:
    settings = get_settings()
    configure_logging(
        service="asr-worker",
        environment=settings.app_env,
        level=settings.log_level,
        json_output=settings.json_logs_enabled,
    )
    worker = ASRWorker(
        session_factory=SessionFactory,
        storage=get_object_storage(),
        settings=settings,
    )
    logger.info("asr_worker_started", "ASR worker started", worker_name=worker.worker_name)
    try:
        while True:
            try:
                worked = await worker.run_once()
            except Exception as error:
                logger.exception(
                    "asr_worker_iteration_failed",
                    "ASR worker iteration failed",
                    error=error,
                )
                worked = False
            if not worked:
                await asyncio.sleep(settings.asr_poll_interval_seconds)
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(run_forever())
