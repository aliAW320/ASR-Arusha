import asyncio
import hashlib
import io
import json
import socket
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
from app.messaging.consumer import consume_attempt_queue
from app.messaging.outbox import enqueue_attempt
from app.messaging.topology import QueueNames
from app.services.audit import add_history_event
from app.services.cancellation import ProcessingCancelled, run_cancellable
from app.services.processing import queue_meeting_composition_if_ready
from app.storage.base import ObjectStorage
from app.storage.minio import get_object_storage

from .chunking import CleanerChunk, build_cleaner_chunks, merge_cleaned_chunks
from .provider import CleanerError, OpenAICompatibleCleanerProvider


logger = get_logger(__name__)


@dataclass(frozen=True)
class WorkItem:
    attempt_id: uuid.UUID
    attempt_number: int
    job_id: uuid.UUID
    result_id: uuid.UUID
    voice_id: uuid.UUID
    meeting_id: uuid.UUID | None
    transcript_bucket: str
    transcript_key: str
    model_name: str
    base_url: str


ProviderFactory = Callable[[WorkItem], OpenAICompatibleCleanerProvider]


class CleanerWorker:
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
        self.worker_name = f"{settings.cleaner_worker_name}@{socket.gethostname()}"
        self.provider_factory = provider_factory or self._provider

    def _provider(self, item: WorkItem) -> OpenAICompatibleCleanerProvider:
        if self.settings.transcript_api_key is None:
            raise CleanerError(
                "TRANSCRIPT_API_KEY is not configured",
                code="cleaner_configuration_error",
                retryable=False,
            )
        return OpenAICompatibleCleanerProvider(
            base_url=item.base_url,
            api_key=self.settings.transcript_api_key.get_secret_value(),
            timeout_seconds=self.settings.cleaner_request_timeout_seconds,
            temperature=self.settings.cleaner_temperature,
            enable_thinking=self.settings.cleaner_enable_thinking,
        )

    async def _clean_chunks(
        self,
        chunks: list[CleanerChunk],
        provider: OpenAICompatibleCleanerProvider,
        item: WorkItem,
    ) -> tuple[list[tuple[CleanerChunk, list[dict]]], list[str]]:
        semaphore = asyncio.Semaphore(self.settings.cleaner_max_concurrency)

        async def clean_one(chunk: CleanerChunk):
            async with semaphore:
                return await run_cancellable(
                    provider.clean(chunk.segments, model=item.model_name),
                    session_factory=self.session_factory,
                    attempt_id=item.attempt_id,
                    poll_interval_seconds=(
                        self.settings.processing_cancellation_poll_interval_seconds
                    ),
                )

        tasks = [asyncio.create_task(clean_one(chunk)) for chunk in chunks]
        try:
            responses = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        cleaned_chunks = [
            (chunk, response.segments)
            for chunk, response in zip(chunks, responses, strict=True)
        ]
        request_ids = [
            response.external_request_id
            for response in responses
            if response.external_request_id
        ]
        return cleaned_chunks, request_ids

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
                    ProcessingJob.stage == ProcessingStage.CLEANING,
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
                if voice and voice.meeting_id
                else None
            )
            add_history_event(
                session,
                event_type="processing.started",
                description="Transcript cleaning started",
                event_data={
                    "job_id": str(attempt.job.id),
                    "attempt_number": attempt.attempt_number,
                    "stage": ProcessingStage.CLEANING.value,
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
                raise RuntimeError("Queued cleaner job is incomplete")
            result = attempt.job.result
            artifact = await session.scalar(
                select(ResultArtifact).where(
                    ResultArtifact.result_id == result.id,
                    ResultArtifact.artifact_type
                    == ResultArtifactType.ALIGNED_TRANSCRIPT_JSON,
                )
            )
            if artifact is None:
                raise CleanerError(
                    "Aligned transcript artifact is missing",
                    code="missing_aligned_transcript",
                    retryable=False,
                )
            voice = result.voice
            integration = attempt.job.integration
            return WorkItem(
                attempt_id=attempt.id,
                attempt_number=attempt.attempt_number,
                job_id=attempt.job.id,
                result_id=result.id,
                voice_id=voice.id,
                meeting_id=voice.meeting_id,
                transcript_bucket=artifact.minio_bucket,
                transcript_key=artifact.minio_key,
                model_name=attempt.job.model.name,
                base_url=(
                    integration.endpoint
                    if integration and integration.endpoint
                    else self.settings.base_url
                ),
            )

    async def _finish_success(
        self,
        item: WorkItem,
        payload: bytes,
        external_request_id: str | None,
        chunk_count: int,
    ) -> None:
        key = f"meetings/{item.meeting_id}/results/{item.result_id}/cleaned-transcript.json"
        await self.storage.put_object(
            self.settings.minio_exports_bucket,
            key,
            io.BytesIO(payload),
            len(payload),
            "application/json",
        )
        try:
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
                    raise ProcessingCancelled("Cleaner job was cancelled before completion")
                session.add(
                    ResultArtifact(
                        result=result,
                        artifact_type=ResultArtifactType.CLEANED_TEXT,
                        minio_bucket=self.settings.minio_exports_bucket,
                        minio_key=key,
                        content_type="application/json",
                        checksum_sha256=hashlib.sha256(payload).hexdigest(),
                        producer_job=job,
                    )
                )
                now = datetime.now(timezone.utc)
                attempt.status = ProcessingAttemptStatus.SUCCEEDED
                attempt.external_request_id = external_request_id
                attempt.finished_at = now
                result.completed_at = now
                voice.status = VoiceStatus.FINISHED
                meeting = await session.get(Meeting, voice.meeting_id) if voice.meeting_id else None
                add_history_event(
                    session,
                    event_type="processing.succeeded",
                    description="Transcript cleaning completed",
                    event_data={
                        "job_id": str(job.id),
                        "attempt_number": attempt.attempt_number,
                        "stage": ProcessingStage.CLEANING.value,
                        "chunk_count": chunk_count,
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
                    "cleaner_artifact_cleanup_failed",
                    "Cleaner artifact cleanup failed",
                    error=cleanup_error,
                    object_key=key,
                )
            raise

        # Deliberately a separate transaction from the cleaning success above:
        # this must never roll back (or otherwise jeopardize) the cleaning
        # result that just committed successfully. Composition readiness is
        # re-checked from durable state, so running it after commit loses
        # nothing.
        try:
            async with self.session_factory() as session:
                voice = await session.get(VoiceFile, item.voice_id)
                if voice is None:
                    return
                composition_job = await queue_meeting_composition_if_ready(
                    session, voice, self.settings, self.storage
                )
                if composition_job is not None:
                    meeting = (
                        await session.get(Meeting, voice.meeting_id)
                        if voice.meeting_id
                        else None
                    )
                    add_history_event(
                        session,
                        event_type="meeting_composition.queued",
                        description="Meeting composition queued",
                        event_data={
                            "job_id": str(composition_job.id),
                            "meeting_result_id": str(composition_job.meeting_result_id),
                            "stage": ProcessingStage.MEETING_COMPOSE.value,
                        },
                        affected_meetings=[meeting] if meeting else [],
                    )
                    await session.commit()
        except Exception as error:
            logger.exception(
                "meeting_composition_trigger_failed",
                "Automatic meeting composition trigger failed after cleaning succeeded",
                error=error,
                voice_id=str(item.voice_id),
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
            will_retry = (
                retryable
                and attempt.attempt_number < self.settings.cleaner_max_attempts
            )
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
                    ProcessingStage.CLEANING,
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
                    "Transcript cleaning retry queued"
                    if will_retry
                    else "Transcript cleaning failed"
                ),
                event_data={
                    "job_id": str(job.id),
                    "attempt_number": attempt.attempt_number,
                    "stage": ProcessingStage.CLEANING.value,
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
        provider = None
        started = time.monotonic()
        try:
            transcript_buffer = io.BytesIO()
            await self.storage.download_object(
                item.transcript_bucket,
                item.transcript_key,
                transcript_buffer,
            )
            transcript = json.loads(transcript_buffer.getvalue().decode("utf-8"))
            segments = transcript.get("segments") or []
            chunks = build_cleaner_chunks(
                segments,
                max_chars=self.settings.cleaner_chunk_max_chars,
                overlap_min_segments=self.settings.cleaner_overlap_min_segments,
                overlap_max_segments=self.settings.cleaner_overlap_max_segments,
            )
            provider = self.provider_factory(item)
            cleaned_chunks, request_ids = await self._clean_chunks(
                chunks, provider, item
            )
            cleaned_segments = merge_cleaned_chunks(segments, cleaned_chunks) if chunks else []
            cleaned = {
                **transcript,
                "schema_version": "cleaned-speaker-transcript/v1",
                "cleaner_model": item.model_name,
                "text": " ".join(
                    segment["text"].strip()
                    for segment in cleaned_segments
                    if segment["text"].strip()
                ),
                "segments": cleaned_segments,
            }
            payload = json.dumps(
                cleaned, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            await self._finish_success(
                item,
                payload,
                request_ids[-1] if request_ids else None,
                len(chunks),
            )
            logger.info(
                "cleaner_processing_succeeded",
                "Transcript cleaning completed",
                job_id=str(item.job_id),
                voice_id=str(item.voice_id),
                attempt_number=item.attempt_number,
                chunk_count=len(chunks),
                duration_seconds=time.monotonic() - started,
            )
        except ProcessingCancelled:
            logger.info(
                "cleaner_processing_cancelled",
                "Transcript cleaning cancelled",
                job_id=str(item.job_id),
                voice_id=str(item.voice_id),
            )
        except CleanerError as error:
            await self._finish_failure(
                item,
                code=error.code,
                message=str(error),
                retryable=error.retryable,
            )
        except Exception as error:
            logger.exception(
                "cleaner_processing_failed",
                "Unexpected transcript cleaning failure",
                error=error,
                job_id=str(item.job_id),
                voice_id=str(item.voice_id),
            )
            await self._finish_failure(
                item,
                code="cleaner_unexpected_error",
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
        service="cleaner-worker",
        environment=settings.app_env,
        level=settings.log_level,
        json_output=settings.json_logs_enabled,
    )
    try:
        await run(settings)
    finally:
        await close_database()


async def run(settings: Settings) -> None:
    """Run the cleaner consume loop until cancelled.

    Does not touch global logging config or the shared DB engine, so it can
    be embedded as a background task in another process (e.g. the API
    process) alongside other workers that share the same engine.
    """
    worker = CleanerWorker(
        session_factory=SessionFactory,
        storage=get_object_storage(),
        settings=settings,
    )
    logger.info(
        "cleaner_worker_started",
        "Cleaner worker started",
        worker_name=worker.worker_name,
        model=settings.cleaner_model_name,
    )
    await consume_attempt_queue(
        settings=settings,
        queue_name=QueueNames.from_settings(settings).cleaning,
        session_factory=SessionFactory,
        handler=worker.run_once,
    )


if __name__ == "__main__":
    asyncio.run(run_forever())
