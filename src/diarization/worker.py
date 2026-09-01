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
from app.services.audit import add_history_event
from app.storage.base import ObjectStorage
from app.storage.minio import get_object_storage

from .alignment import align_transcript_to_speakers
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
    transcript_bucket: str
    transcript_key: str
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
                    ProcessingJob.stage == ProcessingStage.DIARIZATION,
                )
                .order_by(ProcessingJob.priority.desc(), ProcessingAttempt.queued_at)
                .with_for_update(skip_locked=True, of=ProcessingAttempt)
                .limit(1)
            )
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
            transcript = await session.scalar(
                select(ResultArtifact).where(
                    ResultArtifact.result_id == result.id,
                    ResultArtifact.artifact_type == ResultArtifactType.TRANSCRIPT_JSON,
                )
            )
            if transcript is None:
                raise DiarizationError(
                    "Canonical ASR transcript artifact is missing",
                    code="missing_transcript_artifact",
                    retryable=False,
                )
            voice = result.voice
            return WorkItem(
                attempt_id=attempt.id,
                attempt_number=attempt.attempt_number,
                job_id=attempt.job.id,
                result_id=result.id,
                voice_id=voice.id,
                meeting_id=voice.meeting_id,
                source_bucket=voice.minio_bucket,
                source_key=voice.minio_key,
                filename=voice.original_filename or "voice.bin",
                transcript_bucket=transcript.minio_bucket,
                transcript_key=transcript.minio_key,
                model_name=attempt.job.model.name,
            )

    async def _finish_success(
        self,
        item: WorkItem,
        turns: list[DiarizationTurn],
        aligned: dict,
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
        uploads = (
            (
                ResultArtifactType.DIARIZATION_JSON,
                f"{prefix}/diarization.json",
                json.dumps(diarization, ensure_ascii=False, separators=(",", ":")).encode(),
            ),
            (
                ResultArtifactType.ALIGNED_TRANSCRIPT_JSON,
                f"{prefix}/speaker-transcript.json",
                json.dumps(aligned, ensure_ascii=False, separators=(",", ":")).encode(),
            ),
        )
        uploaded: list[tuple[str, str]] = []
        try:
            for _, key, payload in uploads:
                await self.storage.put_object(
                    self.settings.minio_exports_bucket,
                    key,
                    io.BytesIO(payload),
                    len(payload),
                    "application/json",
                )
                uploaded.append((self.settings.minio_exports_bucket, key))

            async with self.session_factory() as session:
                attempt = await session.get(ProcessingAttempt, item.attempt_id)
                job = await session.get(ProcessingJob, item.job_id)
                result = await session.get(Result, item.result_id)
                voice = await session.get(VoiceFile, item.voice_id)
                if attempt is None or job is None or result is None or voice is None:
                    raise RuntimeError("Diarization job state disappeared before completion")
                for artifact_type, key, payload in uploads:
                    session.add(
                        ResultArtifact(
                            result=result,
                            artifact_type=artifact_type,
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
                result.completed_at = now
                voice.status = VoiceStatus.FINISHED
                meeting = await session.get(Meeting, voice.meeting_id) if voice.meeting_id else None
                add_history_event(
                    session,
                    event_type="processing.succeeded",
                    description="Speaker diarization and transcript alignment completed",
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
            for bucket, key in uploaded:
                try:
                    await self.storage.remove_object(bucket, key)
                except Exception as cleanup_error:
                    logger.exception(
                        "diarization_artifact_cleanup_failed",
                        "Diarization artifact cleanup failed",
                        error=cleanup_error,
                        bucket=bucket,
                        object_key=key,
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
            will_retry = retryable and attempt.attempt_number < self.settings.diarization_max_attempts
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

    async def run_once(self) -> bool:
        attempt_id = await self.claim_next()
        if attempt_id is None:
            return False
        item = await self._load_work_item(attempt_id)
        started = time.monotonic()
        try:
            provider = self.provider_factory(item)
            with tempfile.TemporaryDirectory(prefix="diarization-") as temp_dir:
                audio_path = Path(temp_dir) / Path(item.filename).name
                transcript_buffer = io.BytesIO()
                with audio_path.open("w+b") as audio:
                    await self.storage.download_object(item.source_bucket, item.source_key, audio)
                await self.storage.download_object(
                    item.transcript_bucket,
                    item.transcript_key,
                    transcript_buffer,
                )
                transcript = json.loads(transcript_buffer.getvalue().decode("utf-8"))
                turns = await provider.diarize(audio_path)
            aligned = align_transcript_to_speakers(
                transcript,
                turns,
                diarization_model=item.model_name,
            )
            await self._finish_success(item, turns, aligned)
            logger.info(
                "diarization_processing_succeeded",
                "Speaker diarization completed",
                job_id=str(item.job_id),
                voice_id=str(item.voice_id),
                attempt_number=item.attempt_number,
                inference_duration_seconds=time.monotonic() - started,
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
    try:
        while True:
            try:
                worked = await worker.run_once()
            except Exception as error:
                logger.exception(
                    "diarization_worker_iteration_failed",
                    "Diarization worker iteration failed",
                    error=error,
                )
                worked = False
            if not worked:
                await asyncio.sleep(settings.diarization_poll_interval_seconds)
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(run_forever())
