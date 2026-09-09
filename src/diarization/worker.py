import asyncio
import hashlib
import io
import json
import socket
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.config import Settings, get_settings
from app.database import SessionFactory, close_database
from app.models import (
    DiarizationSpeaker,
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
from app.messaging.consumer import consume_attempt_queue
from app.messaging.outbox import enqueue_attempt
from app.messaging.topology import QueueNames
from app.services.audit import add_history_event
from app.services.cancellation import ProcessingCancelled, run_cancellable
from app.services.diarization import heartbeat_forever
from app.services.processing import align_result_if_ready
from app.storage.base import ObjectStorage
from app.storage.minio import get_object_storage

from .provider import DiarizationError, DiarizationProvider, DiarizationTurn, PyannoteLocalProvider


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
    model_name: str


ProviderFactory = Callable[[WorkItem], DiarizationProvider]


class DiarizationWorker:
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
        self.worker_name = f"{settings.diarization_worker_name}@{socket.gethostname()}"
        self.provider_factory = provider_factory or self._provider

    def _provider(self, item: WorkItem) -> DiarizationProvider:
        token = self.settings.huggingface_token
        return PyannoteLocalProvider(
            model_name=item.model_name,
            token=token.get_secret_value() if token else "",
            device=self.settings.diarization_device,
        )

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
                    ProcessingJob.stage == ProcessingStage.DIARIZATION,
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
            meeting = await session.get(Meeting, voice.meeting_id) if voice and voice.meeting_id else None
            add_history_event(
                session,
                event_type="processing.started",
                description="Speaker diarization started",
                event_data={
                    "job_id": str(attempt.job.id),
                    "attempt_number": attempt.attempt_number,
                    "stage": ProcessingStage.DIARIZATION.value,
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
                )
                .where(ProcessingAttempt.id == attempt_id)
            )
            if attempt is None or attempt.job.result is None or attempt.job.model is None:
                raise RuntimeError("Queued diarization job is incomplete")
            result = attempt.job.result
            voice = result.voice
            normalized = await session.scalar(
                select(ResultArtifact).where(
                    ResultArtifact.result_id == result.id,
                    ResultArtifact.artifact_type
                    == ResultArtifactType.NORMALIZED_AUDIO,
                )
            )
            if normalized is None:
                raise RuntimeError("Normalized audio artifact is missing")
            return WorkItem(
                attempt_id=attempt.id,
                attempt_number=attempt.attempt_number,
                job_id=attempt.job.id,
                result_id=result.id,
                voice_id=voice.id,
                meeting_id=voice.meeting_id,
                source_bucket=normalized.minio_bucket,
                source_key=normalized.minio_key,
                filename=f"{voice.id}.wav",
                model_name=attempt.job.model.name,
            )

    async def _finish_success(
        self,
        item: WorkItem,
        turns: list[DiarizationTurn],
    ) -> None:
        prefix = f"meetings/{item.meeting_id}/results/{item.result_id}"
        diarization = {
            "schema_version": "diarization/v1",
            "model": item.model_name,
            "segments": [
                {
                    "start_ms": turn.start_ms,
                    "end_ms": turn.end_ms,
                    "speaker_id": turn.speaker_id,
                }
                for turn in turns
            ],
        }
        key = f"{prefix}/diarization.json"
        payload = json.dumps(diarization, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            await self.storage.put_object(
                self.settings.minio_exports_bucket,
                key,
                io.BytesIO(payload),
                len(payload),
                "application/json",
            )

            async with self.session_factory() as session:
                attempt = await session.get(ProcessingAttempt, item.attempt_id)
                job = await session.get(ProcessingJob, item.job_id)
                result = await session.get(Result, item.result_id)
                voice = await session.get(VoiceFile, item.voice_id)
                if (
                    attempt is None
                    or job is None
                    or result is None
                    or voice is None
                    or attempt.status == ProcessingAttemptStatus.CANCELLED
                ):
                    raise ProcessingCancelled(
                        "Diarization job was cancelled before completion"
                    )
                session.add(
                    ResultArtifact(
                        result=result,
                        artifact_type=ResultArtifactType.DIARIZATION_JSON,
                        minio_bucket=self.settings.minio_exports_bucket,
                        minio_key=key,
                        content_type="application/json",
                        checksum_sha256=hashlib.sha256(payload).hexdigest(),
                        producer_job=job,
                    )
                )
                for label in sorted({turn.speaker_id for turn in turns}):
                    session.add(DiarizationSpeaker(result=result, label=label))
                now = datetime.now(timezone.utc)
                attempt.status = ProcessingAttemptStatus.SUCCEEDED
                attempt.finished_at = now
                # Stays PENDING: cleaning is queued once alignment (which
                # needs both this and the ASR transcript) completes, not here.
                if voice.status != VoiceStatus.ERROR:
                    voice.status = VoiceStatus.PENDING
                meeting = await session.get(Meeting, voice.meeting_id) if voice.meeting_id else None
                add_history_event(
                    session,
                    event_type="processing.succeeded",
                    description="Speaker diarization completed",
                    event_data={
                        "job_id": str(job.id),
                        "attempt_number": attempt.attempt_number,
                        "stage": ProcessingStage.DIARIZATION.value,
                        "speaker_count": len({turn.speaker_id for turn in turns}),
                    },
                    affected_meetings=[meeting] if meeting else [],
                    affected_voices=[voice],
                )
                await session.commit()
        except Exception:
            try:
                await self.storage.remove_object(self.settings.minio_exports_bucket, key)
            except Exception as cleanup_error:
                logger.exception(
                    "diarization_artifact_cleanup_failed",
                    "Diarization artifact cleanup failed",
                    error=cleanup_error,
                    bucket=self.settings.minio_exports_bucket,
                    object_key=key,
                )
            raise

        # Deliberately a separate transaction from the diarization success
        # above, mirroring the ASR worker: alignment either does nothing
        # (transcription isn't done yet) or merges + queues cleaning, and
        # neither outcome should be able to roll back the diarization result
        # that just committed.
        try:
            async with self.session_factory() as session:
                result = await session.get(Result, item.result_id)
                if result is None:
                    return
                await align_result_if_ready(
                    session, result, item.meeting_id, self.settings, self.storage
                )
                await session.commit()
        except Exception as error:
            logger.exception(
                "alignment_trigger_failed",
                "Speaker alignment trigger failed after diarization succeeded",
                error=error,
                result_id=str(item.result_id),
            )

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
            will_retry = retryable and attempt.attempt_number < self.settings.diarization_max_attempts
            if will_retry:
                retry_attempt = ProcessingAttempt(
                    job=job,
                    attempt_number=attempt.attempt_number + 1,
                    status=ProcessingAttemptStatus.QUEUED,
                )
                session.add(retry_attempt)
                await session.flush()
                enqueue_attempt(
                    session,
                    retry_attempt,
                    ProcessingStage.DIARIZATION,
                    self.settings,
                    retry=True,
                )
            else:
                voice.status = VoiceStatus.ERROR
            meeting = await session.get(Meeting, voice.meeting_id) if voice.meeting_id else None
            add_history_event(
                session,
                event_type="processing.retry_queued" if will_retry else "processing.failed",
                description=(
                    "Speaker diarization retry queued"
                    if will_retry
                    else "Speaker diarization failed"
                ),
                event_data={
                    "job_id": str(job.id),
                    "attempt_number": attempt.attempt_number,
                    "stage": ProcessingStage.DIARIZATION.value,
                    "error_code": code,
                    "error_message": message[:500],
                    "will_retry": will_retry,
                },
                affected_meetings=[meeting] if meeting else [],
                affected_voices=[voice],
            )
            await session.commit()

    async def run_once(self, requested_attempt_id: uuid.UUID | None = None) -> bool:
        attempt_id = await self.claim_next(requested_attempt_id)
        if attempt_id is None:
            return False
        item = await self._load_work_item(attempt_id)
        started = time.monotonic()
        try:
            provider = self.provider_factory(item)
            with tempfile.TemporaryDirectory(prefix="diarization-") as temp_dir:
                audio_path = Path(temp_dir) / Path(item.filename).name
                with audio_path.open("w+b") as audio:
                    await self.storage.download_object(item.source_bucket, item.source_key, audio)
                turns = await run_cancellable(
                    provider.diarize(audio_path),
                    session_factory=self.session_factory,
                    attempt_id=item.attempt_id,
                    poll_interval_seconds=(
                        self.settings.processing_cancellation_poll_interval_seconds
                    ),
                )
            await self._finish_success(item, turns)
            logger.info(
                "diarization_processing_succeeded",
                "Speaker diarization completed",
                job_id=str(item.job_id),
                voice_id=str(item.voice_id),
                attempt_number=item.attempt_number,
                inference_duration_seconds=time.monotonic() - started,
            )
        except ProcessingCancelled:
            logger.info(
                "diarization_processing_cancelled",
                "Speaker diarization cancelled",
                job_id=str(item.job_id),
                voice_id=str(item.voice_id),
            )
        except DiarizationError as error:
            await self._finish_failure(
                item,
                code=error.code,
                message=str(error),
                retryable=error.retryable,
            )
        except Exception as error:
            logger.exception(
                "diarization_processing_failed",
                "Unexpected speaker diarization failure",
                error=error,
                job_id=str(item.job_id),
                voice_id=str(item.voice_id),
            )
            await self._finish_failure(
                item,
                code="diarization_unexpected_error",
                message=str(error),
                retryable=True,
            )
        return True


async def run_forever(
    *,
    provider_factory: ProviderFactory | None = None,
    configure_worker_logging: bool = True,
) -> None:
    settings = get_settings()
    if configure_worker_logging:
        configure_logging(
            service="diarization-worker",
            environment=settings.app_env,
            level=settings.log_level,
            json_output=settings.json_logs_enabled,
        )
    worker = DiarizationWorker(
        session_factory=SessionFactory,
        storage=get_object_storage(),
        settings=settings,
        provider_factory=provider_factory,
    )
    logger.info(
        "diarization_worker_started",
        "Diarization worker started",
        worker_name=worker.worker_name,
        device=settings.diarization_device,
        model=settings.diarization_model_name,
    )
    # The heartbeat is what tells the API that speaker separation can be
    # offered at all; without a live worker the API stops asking users about
    # it and sends new uploads straight from ASR to cleaning.
    heartbeat = asyncio.create_task(heartbeat_forever(SessionFactory, settings))
    try:
        await consume_attempt_queue(
            settings=settings,
            queue_name=QueueNames.from_settings(settings).diar,
            session_factory=SessionFactory,
            handler=worker.run_once,
        )
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        await close_database()


if __name__ == "__main__":
    asyncio.run(run_forever())
