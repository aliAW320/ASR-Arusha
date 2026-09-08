import asyncio
import hashlib
import io
import json
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.config import Settings, get_settings
from app.database import SessionFactory, close_database
from app.models import (
    DiarizationSpeaker,
    Meeting,
    MeetingArtifactType,
    MeetingResult,
    MeetingResultArtifact,
    MeetingResultSource,
    ProcessingAttempt,
    ProcessingAttemptStatus,
    ProcessingJob,
    ProcessingStage,
    Result,
)
from app.observability.logging import configure_logging, get_logger
from app.services.audit import add_history_event
from app.services.cancellation import ProcessingCancelled, attempt_is_cancelled
from app.services.processing import start_meeting_publication
from app.storage.base import ObjectStorage
from app.storage.minio import get_object_storage

from .composer import SourceInput, compose_meeting_transcript
from .schema import ComposerError, parse_source_segments


logger = get_logger(__name__)


@dataclass(frozen=True)
class SourceWorkItem:
    position: int
    voice_id: str
    result_id: str
    source_artifact_id: str
    source_artifact_type: str
    source_checksum_sha256: str
    original_filename: str | None
    voice_sequence_number: int | None
    duration_ms: int
    artifact_bucket: str
    artifact_key: str
    speaker_names: dict[str, tuple[str, str]]


@dataclass(frozen=True)
class WorkItem:
    attempt_id: uuid.UUID
    attempt_number: int
    job_id: uuid.UUID
    meeting_id: uuid.UUID
    meeting_result_id: uuid.UUID
    sources: list[SourceWorkItem]


class MeetingComposerWorker:
    """Executes MEETING_COMPOSE jobs.

    Deliberately has no provider_factory: unlike the ASR/diarization/cleaner
    workers, composition never calls a model or a remote integration -- it
    only reads already-produced transcript artifacts and merges them.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        settings: Settings,
    ):
        self.session_factory = session_factory
        self.storage = storage
        self.settings = settings
        self.worker_name = f"{settings.meeting_composer_worker_name}@{socket.gethostname()}"

    async def claim_next(self) -> uuid.UUID | None:
        async with self.session_factory() as session:
            attempt = await session.scalar(
                select(ProcessingAttempt)
                .join(ProcessingAttempt.job)
                .options(
                    joinedload(ProcessingAttempt.job)
                    .joinedload(ProcessingJob.meeting_result)
                    .joinedload(MeetingResult.meeting)
                )
                .where(
                    ProcessingAttempt.status == ProcessingAttemptStatus.QUEUED,
                    ProcessingJob.stage == ProcessingStage.MEETING_COMPOSE,
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
            meeting_result = attempt.job.meeting_result
            meeting = meeting_result.meeting if meeting_result else None
            add_history_event(
                session,
                event_type="meeting_composition.started",
                description="Meeting composition started",
                event_data={
                    "job_id": str(attempt.job.id),
                    "attempt_number": attempt.attempt_number,
                    "stage": ProcessingStage.MEETING_COMPOSE.value,
                    "worker_name": self.worker_name,
                },
                affected_meetings=[meeting] if meeting else [],
                affected_meeting_results=[meeting_result] if meeting_result else [],
            )
            await session.commit()
            return attempt.id

    async def _load_work_item(self, attempt_id: uuid.UUID) -> WorkItem:
        async with self.session_factory() as session:
            attempt = await session.scalar(
                select(ProcessingAttempt)
                .options(
                    joinedload(ProcessingAttempt.job)
                    .joinedload(ProcessingJob.meeting_result)
                    .joinedload(MeetingResult.meeting),
                    joinedload(ProcessingAttempt.job)
                    .joinedload(ProcessingJob.meeting_result)
                    .selectinload(MeetingResult.sources)
                    .joinedload(MeetingResultSource.result)
                    .joinedload(Result.voice),
                    joinedload(ProcessingAttempt.job)
                    .joinedload(ProcessingJob.meeting_result)
                    .selectinload(MeetingResult.sources)
                    .joinedload(MeetingResultSource.source_artifact),
                )
                .where(ProcessingAttempt.id == attempt_id)
            )
            meeting_result = attempt.job.meeting_result if attempt else None
            if attempt is None or meeting_result is None or not meeting_result.sources:
                raise RuntimeError("Queued meeting composer job is incomplete")

            result_ids = [source.result_id for source in meeting_result.sources]
            diarization_speakers = (
                await session.scalars(
                    select(DiarizationSpeaker)
                    .options(joinedload(DiarizationSpeaker.meeting_speaker))
                    .where(
                        DiarizationSpeaker.result_id.in_(result_ids),
                        DiarizationSpeaker.meeting_speaker_id.isnot(None),
                    )
                )
            ).all()
            speaker_names_by_result: dict[uuid.UUID, dict[str, tuple[str, str]]] = {}
            for diarization_speaker in diarization_speakers:
                display_name = (
                    diarization_speaker.meeting_speaker.display_name
                    or f"Speaker {diarization_speaker.meeting_speaker_id}"
                )
                speaker_names_by_result.setdefault(diarization_speaker.result_id, {})[
                    diarization_speaker.label
                ] = (str(diarization_speaker.meeting_speaker_id), display_name)

            sources = [
                SourceWorkItem(
                    position=source.position,
                    voice_id=str(source.result.voice_id),
                    result_id=str(source.result_id),
                    source_artifact_id=str(source.source_artifact_id),
                    source_artifact_type=source.source_artifact.artifact_type.value,
                    source_checksum_sha256=source.source_artifact.checksum_sha256 or "",
                    original_filename=source.result.voice.original_filename,
                    voice_sequence_number=source.voice_sequence_snapshot,
                    duration_ms=source.source_duration_ms,
                    artifact_bucket=source.source_artifact.minio_bucket,
                    artifact_key=source.source_artifact.minio_key,
                    speaker_names=speaker_names_by_result.get(source.result_id, {}),
                )
                for source in meeting_result.sources
            ]
            return WorkItem(
                attempt_id=attempt.id,
                attempt_number=attempt.attempt_number,
                job_id=attempt.job.id,
                meeting_id=meeting_result.meeting_id,
                meeting_result_id=meeting_result.id,
                sources=sources,
            )

    async def _finish_success(self, item: WorkItem, payload: bytes) -> None:
        key = (
            f"meetings/{item.meeting_id}/meeting-results/"
            f"{item.meeting_result_id}/combined-transcript.json"
        )
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
                meeting_result = await session.get(MeetingResult, item.meeting_result_id)
                meeting = await session.get(Meeting, item.meeting_id)
                if (
                    attempt is None
                    or job is None
                    or meeting_result is None
                    or attempt.status == ProcessingAttemptStatus.CANCELLED
                ):
                    raise ProcessingCancelled(
                        "Meeting composition was cancelled before completion"
                    )
                artifact = MeetingResultArtifact(
                    meeting_result=meeting_result,
                    artifact_type=MeetingArtifactType.COMBINED_TRANSCRIPT_JSON,
                    minio_bucket=self.settings.minio_exports_bucket,
                    minio_key=key,
                    content_type="application/json",
                    checksum_sha256=hashlib.sha256(payload).hexdigest(),
                    producer_job=job,
                )
                session.add(artifact)
                now = datetime.now(timezone.utc)
                attempt.status = ProcessingAttemptStatus.SUCCEEDED
                attempt.finished_at = now
                meeting_result.completed_at = now
                # Composition is the last step before the knowledge base, and
                # publication is no longer gated on anyone confirming it, so
                # summarization + MCP is queued right here.
                publication_job = (
                    await start_meeting_publication(
                        session, meeting, meeting_result, self.settings
                    )
                    if meeting is not None
                    else None
                )
                if publication_job is not None:
                    add_history_event(
                        session,
                        event_type="meeting_publication.queued",
                        description="Meeting summary and knowledge-base publication queued",
                        event_data={
                            "job_id": str(publication_job.id),
                            "stage": ProcessingStage.MINUTES_GENERATION.value,
                            "meeting_result_id": str(meeting_result.id),
                        },
                        affected_meetings=[meeting],
                        affected_meeting_results=[meeting_result],
                    )
                add_history_event(
                    session,
                    event_type="meeting_composition.succeeded",
                    description="Meeting composition completed",
                    event_data={
                        "job_id": str(job.id),
                        "attempt_number": attempt.attempt_number,
                        "stage": ProcessingStage.MEETING_COMPOSE.value,
                        "source_count": len(item.sources),
                    },
                    affected_meetings=[meeting] if meeting else [],
                    affected_meeting_results=[meeting_result],
                )
                await session.commit()
        except Exception:
            try:
                await self.storage.remove_object(self.settings.minio_exports_bucket, key)
            except Exception as cleanup_error:
                logger.exception(
                    "meeting_composer_artifact_cleanup_failed",
                    "Meeting composer artifact cleanup failed",
                    error=cleanup_error,
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
            meeting_result = await session.get(MeetingResult, item.meeting_result_id)
            meeting = await session.get(Meeting, item.meeting_id)
            if attempt is None or job is None:
                return
            attempt.status = ProcessingAttemptStatus.FAILED
            attempt.finished_at = datetime.now(timezone.utc)
            attempt.error_code = code[:100]
            attempt.error_message = message[:2000]
            will_retry = (
                retryable
                and attempt.attempt_number < self.settings.meeting_composer_max_attempts
            )
            if will_retry:
                session.add(
                    ProcessingAttempt(
                        job=job,
                        attempt_number=attempt.attempt_number + 1,
                        status=ProcessingAttemptStatus.QUEUED,
                    )
                )
            add_history_event(
                session,
                event_type=(
                    "meeting_composition.retry_scheduled"
                    if will_retry
                    else "meeting_composition.failed"
                ),
                description=(
                    "Meeting composition retry scheduled"
                    if will_retry
                    else "Meeting composition failed"
                ),
                event_data={
                    "job_id": str(job.id),
                    "attempt_number": attempt.attempt_number,
                    "stage": ProcessingStage.MEETING_COMPOSE.value,
                    "error_code": code,
                    "error_message": message[:500],
                    "will_retry": will_retry,
                },
                affected_meetings=[meeting] if meeting else [],
                affected_meeting_results=[meeting_result] if meeting_result else [],
            )
            await session.commit()

    async def run_once(self) -> bool:
        attempt_id = await self.claim_next()
        if attempt_id is None:
            return False
        item = await self._load_work_item(attempt_id)
        started = time.monotonic()
        try:
            composer_sources: list[SourceInput] = []
            total_bytes = 0
            for source in item.sources:
                if await attempt_is_cancelled(self.session_factory, item.attempt_id):
                    raise ProcessingCancelled("Meeting composition was cancelled")
                buffer = io.BytesIO()
                await self.storage.download_object(
                    source.artifact_bucket, source.artifact_key, buffer
                )
                content = buffer.getvalue()
                total_bytes += len(content)
                if total_bytes > self.settings.meeting_composer_max_input_bytes:
                    raise ComposerError(
                        "Combined source artifact size exceeds the configured limit",
                        code="composition_limit_exceeded",
                    )
                try:
                    transcript = json.loads(content.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ComposerError(
                        f"Source artifact for result {source.result_id} is not valid JSON",
                        code="source_artifact_invalid",
                    ) from error
                segments = parse_source_segments(transcript)
                composer_sources.append(
                    SourceInput(
                        position=source.position,
                        voice_id=source.voice_id,
                        result_id=source.result_id,
                        source_artifact_id=source.source_artifact_id,
                        source_artifact_type=source.source_artifact_type,
                        source_checksum_sha256=source.source_checksum_sha256,
                        original_filename=source.original_filename,
                        voice_sequence_number=source.voice_sequence_number,
                        duration_ms=source.duration_ms,
                        segments=segments,
                        speaker_names=source.speaker_names,
                    )
                )
            payload = compose_meeting_transcript(
                meeting_id=str(item.meeting_id),
                meeting_result_id=str(item.meeting_result_id),
                generated_at=datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                sources=composer_sources,
                gap_ms=self.settings.meeting_composer_gap_ms,
                max_segments=self.settings.meeting_composer_max_segments,
            )
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            await self._finish_success(item, encoded)
            logger.info(
                "meeting_composer_processing_succeeded",
                "Meeting composition completed",
                job_id=str(item.job_id),
                meeting_id=str(item.meeting_id),
                source_count=len(item.sources),
                duration_seconds=time.monotonic() - started,
            )
        except ProcessingCancelled:
            logger.info(
                "meeting_composition_cancelled",
                "Meeting composition cancelled because a source voice was deleted",
                job_id=str(item.job_id),
                meeting_id=str(item.meeting_id),
            )
        except ComposerError as error:
            await self._finish_failure(
                item, code=error.code, message=str(error), retryable=False
            )
        except Exception as error:
            logger.exception(
                "meeting_composer_processing_failed",
                "Unexpected meeting composition failure",
                error=error,
                job_id=str(item.job_id),
                meeting_id=str(item.meeting_id),
            )
            await self._finish_failure(
                item,
                code="meeting_composer_unexpected_error",
                message=str(error),
                retryable=True,
            )
        return True


async def run(settings: Settings) -> None:
    """Run the meeting-composer polling loop until cancelled.

    Does not touch global logging config or the shared DB engine, so it can
    be embedded as a background task in another process (e.g. the API
    process) alongside other workers that share the same engine.
    """
    worker = MeetingComposerWorker(
        session_factory=SessionFactory,
        storage=get_object_storage(),
        settings=settings,
    )
    logger.info(
        "meeting_composer_worker_started",
        "Meeting composer worker started",
        worker_name=worker.worker_name,
    )
    while True:
        try:
            worked = await worker.run_once()
        except Exception as error:
            logger.exception(
                "meeting_composer_worker_iteration_failed",
                "Meeting composer worker iteration failed",
                error=error,
            )
            worked = False
        if not worked:
            await asyncio.sleep(settings.meeting_composer_poll_interval_seconds)


async def run_forever() -> None:
    """Standalone entrypoint: owns logging setup and DB engine teardown."""
    settings = get_settings()
    configure_logging(
        service="meeting-composer-worker",
        environment=settings.app_env,
        level=settings.log_level,
        json_output=settings.json_logs_enabled,
    )
    try:
        await run(settings)
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(run_forever())
