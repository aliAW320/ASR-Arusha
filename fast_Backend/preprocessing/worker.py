import asyncio
import hashlib
import io
import socket
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.config import Settings
from app.database import SessionFactory
from app.messaging.consumer import consume_attempt_queue
from app.messaging.outbox import enqueue_attempt
from app.messaging.topology import QueueNames
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
from app.observability.logging import get_logger
from app.services.audit import add_history_event
from app.services.cancellation import ProcessingCancelled, run_cancellable
from app.services.processing import queue_after_preprocessing
from app.storage.base import ObjectStorage
from app.storage.minio import get_object_storage

from .audio import AudioPreprocessingError, NormalizedAudio, normalize_audio


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


Normalizer = Callable[[Path, Path], Awaitable[NormalizedAudio]]


class PreprocessingWorker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        settings: Settings,
        normalizer: Normalizer = normalize_audio,
    ):
        self.session_factory = session_factory
        self.storage = storage
        self.settings = settings
        self.normalizer = normalizer
        self.worker_name = f"{settings.preprocess_worker_name}@{socket.gethostname()}"

    async def claim_next(self, attempt_id: uuid.UUID | None = None) -> uuid.UUID | None:
        async with self.session_factory() as session:
            statement = (
                select(ProcessingAttempt)
                .join(ProcessingAttempt.job)
                .options(
                    joinedload(ProcessingAttempt.job)
                    .joinedload(ProcessingJob.result)
                    .joinedload(Result.voice)
                )
                .where(
                    ProcessingAttempt.status == ProcessingAttemptStatus.QUEUED,
                    ProcessingJob.stage == ProcessingStage.PREPROCESS,
                )
                .order_by(ProcessingJob.priority.desc(), ProcessingAttempt.queued_at)
                .with_for_update(skip_locked=True, of=ProcessingAttempt)
                .limit(1)
            )
            if attempt_id is not None:
                statement = statement.where(ProcessingAttempt.id == attempt_id)
            attempt = await session.scalar(statement)
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
                description="Audio preprocessing started",
                event_data={"job_id": str(attempt.job.id), "stage": "preprocess"},
                affected_meetings=[meeting] if meeting else [],
                affected_voices=[voice] if voice else [],
            )
            await session.commit()
            return attempt.id

    async def _load(self, attempt_id: uuid.UUID) -> WorkItem:
        async with self.session_factory() as session:
            attempt = await session.scalar(
                select(ProcessingAttempt)
                .options(
                    joinedload(ProcessingAttempt.job)
                    .joinedload(ProcessingJob.result)
                    .joinedload(Result.voice)
                )
                .where(ProcessingAttempt.id == attempt_id)
            )
            if attempt is None or attempt.job.result is None:
                raise RuntimeError("Queued preprocessing job is incomplete")
            voice = attempt.job.result.voice
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
            )

    async def _succeed(
        self, item: WorkItem, payload: bytes, metadata: NormalizedAudio
    ) -> None:
        key = f"meetings/{item.meeting_id}/results/{item.result_id}/normalized.wav"
        await self.storage.put_object(
            self.settings.minio_exports_bucket,
            key,
            io.BytesIO(payload),
            len(payload),
            "audio/wav",
        )
        try:
            async with self.session_factory() as session:
                attempt = await session.get(ProcessingAttempt, item.attempt_id)
                job = await session.get(ProcessingJob, item.job_id)
                result = await session.get(Result, item.result_id)
                voice = await session.get(VoiceFile, item.voice_id)
                if not all((attempt, job, result, voice)) or attempt.status == ProcessingAttemptStatus.CANCELLED:
                    raise ProcessingCancelled("Preprocessing was cancelled")
                session.add(
                    ResultArtifact(
                        result=result,
                        artifact_type=ResultArtifactType.NORMALIZED_AUDIO,
                        minio_bucket=self.settings.minio_exports_bucket,
                        minio_key=key,
                        content_type="audio/wav",
                        checksum_sha256=hashlib.sha256(payload).hexdigest(),
                        producer_job=job,
                    )
                )
                voice.duration_ms = metadata.duration_ms
                voice.codec = "pcm_s16le"
                voice.sample_rate_hz = 16000
                voice.channels = 1
                attempt.status = ProcessingAttemptStatus.SUCCEEDED
                attempt.finished_at = datetime.now(timezone.utc)
                await queue_after_preprocessing(session, result, self.settings, job)
                meeting = await session.get(Meeting, item.meeting_id) if item.meeting_id else None
                add_history_event(
                    session,
                    event_type="processing.succeeded",
                    description="Audio normalized to 16 kHz mono WAV",
                    event_data={"job_id": str(job.id), "stage": "preprocess"},
                    affected_meetings=[meeting] if meeting else [],
                    affected_voices=[voice],
                )
                await session.commit()
        except Exception:
            await self.storage.remove_object(self.settings.minio_exports_bucket, key)
            raise

    async def _fail(self, item: WorkItem, error: AudioPreprocessingError) -> None:
        async with self.session_factory() as session:
            attempt = await session.get(ProcessingAttempt, item.attempt_id)
            job = await session.get(ProcessingJob, item.job_id)
            voice = await session.get(VoiceFile, item.voice_id)
            if attempt is None or job is None or voice is None:
                return
            attempt.status = ProcessingAttemptStatus.FAILED
            attempt.finished_at = datetime.now(timezone.utc)
            attempt.error_code = error.code[:100]
            attempt.error_message = str(error)[:2000]
            retry = error.retryable and attempt.attempt_number < self.settings.preprocess_max_attempts
            if retry:
                next_attempt = ProcessingAttempt(
                    job=job,
                    attempt_number=attempt.attempt_number + 1,
                    status=ProcessingAttemptStatus.QUEUED,
                )
                session.add(next_attempt)
                await session.flush()
                enqueue_attempt(session, next_attempt, ProcessingStage.PREPROCESS, self.settings, retry=True)
            else:
                voice.status = VoiceStatus.ERROR
            await session.commit()

    async def run_once(self, requested_attempt_id: uuid.UUID | None = None) -> bool:
        attempt_id = await self.claim_next(requested_attempt_id)
        if attempt_id is None:
            return False
        item = await self._load(attempt_id)
        try:
            with tempfile.TemporaryDirectory(prefix="preprocess-") as temp_dir:
                source = Path(temp_dir) / Path(item.filename).name
                destination = Path(temp_dir) / "normalized.wav"
                with source.open("w+b") as audio:
                    await self.storage.download_object(item.source_bucket, item.source_key, audio)
                metadata = await run_cancellable(
                    self.normalizer(source, destination),
                    session_factory=self.session_factory,
                    attempt_id=item.attempt_id,
                    poll_interval_seconds=self.settings.processing_cancellation_poll_interval_seconds,
                )
                payload = destination.read_bytes()
            await self._succeed(item, payload, metadata)
        except ProcessingCancelled:
            logger.info(
                "audio_preprocessing_cancelled",
                "Audio preprocessing cancelled",
                voice_id=str(item.voice_id),
            )
        except AudioPreprocessingError as error:
            await self._fail(item, error)
        except Exception as error:
            logger.exception(
                "audio_preprocessing_failed",
                "Unexpected audio preprocessing failure",
                error=error,
                voice_id=str(item.voice_id),
            )
            await self._fail(
                item,
                AudioPreprocessingError(
                    str(error), code="audio_preprocess_unexpected", retryable=True
                ),
            )
        return True


async def run(settings: Settings) -> None:
    worker = PreprocessingWorker(
        session_factory=SessionFactory,
        storage=get_object_storage(),
        settings=settings,
    )
    logger.info(
        "preprocess_worker_started",
        "Audio preprocessing worker started",
        worker_name=worker.worker_name,
    )
    await consume_attempt_queue(
        settings=settings,
        queue_name=QueueNames.from_settings(settings).preprocess,
        session_factory=SessionFactory,
        handler=worker.run_once,
    )
