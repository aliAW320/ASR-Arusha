import hashlib
import io
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from alignment.merge import align_transcript_to_speakers
from alignment.types import DiarizationError, DiarizationTurn
from meeting_composer.composer import FINGERPRINT_POLICY_VERSION, compute_offsets, compute_source_fingerprint

from ..config import Settings
from ..models import (
    ExternalIntegration,
    ExternalIntegrationKind,
    Meeting,
    MeetingResult,
    MeetingResultSource,
    ModelDefinition,
    ModelRuntime,
    ModelRuntimeKind,
    ModelTaskType,
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
from ..schemas import ProcessingJobResponse
from ..storage.base import ObjectStorage
from .audit import add_history_event
from ..messaging.outbox import enqueue_attempt


class MeetingCompositionError(ValueError):
    """Raised when a meeting cannot be composed right now.

    Callers map this to an HTTP 409 for the manual API; the automatic
    CleanerWorker hook treats it as "not ready yet" and swallows it.
    """

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


JOB_LOAD_OPTIONS = (
    joinedload(ProcessingJob.result).joinedload(Result.voice),
    joinedload(ProcessingJob.meeting_result),
    joinedload(ProcessingJob.model),
    joinedload(ProcessingJob.integration),
    selectinload(ProcessingJob.attempts),
)


async def ensure_transcription_registry(
    session: AsyncSession,
    settings: Settings,
) -> tuple[ModelDefinition, ExternalIntegration]:
    runtime = await session.scalar(
        select(ModelRuntime).where(ModelRuntime.name == "openai-compatible-asr")
    )
    if runtime is None:
        runtime = ModelRuntime(
            name="openai-compatible-asr",
            kind=ModelRuntimeKind.REMOTE,
        )
        session.add(runtime)
        await session.flush()

    model = await session.scalar(
        select(ModelDefinition).where(
            ModelDefinition.runtime_id == runtime.id,
            ModelDefinition.task_type == ModelTaskType.TRANSCRIPTION,
            ModelDefinition.name == settings.transcript_model_name,
            ModelDefinition.version == "configured",
        )
    )
    if model is None:
        model = ModelDefinition(
            runtime=runtime,
            task_type=ModelTaskType.TRANSCRIPTION,
            name=settings.transcript_model_name,
            version="configured",
            source_uri=settings.base_url,
        )
        session.add(model)

    integration_version = hashlib.sha256(settings.base_url.encode()).hexdigest()[:12]
    integration = await session.scalar(
        select(ExternalIntegration).where(
            ExternalIntegration.name == "openai-compatible-asr-api",
            ExternalIntegration.version == integration_version,
        )
    )
    if integration is None:
        integration = ExternalIntegration(
            name="openai-compatible-asr-api",
            version=integration_version,
            kind=ExternalIntegrationKind.HTTP,
            endpoint=settings.base_url,
        )
        session.add(integration)

    await session.flush()
    return model, integration


async def ensure_diarization_registry(
    session: AsyncSession,
    settings: Settings,
) -> ModelDefinition:
    runtime = await session.scalar(
        select(ModelRuntime).where(ModelRuntime.name == "pyannote.audio-local")
    )
    if runtime is None:
        runtime = ModelRuntime(
            name="pyannote.audio-local",
            kind=ModelRuntimeKind.LOCAL,
        )
        session.add(runtime)
        await session.flush()

    model = await session.scalar(
        select(ModelDefinition).where(
            ModelDefinition.runtime_id == runtime.id,
            ModelDefinition.task_type == ModelTaskType.DIARIZATION,
            ModelDefinition.name == settings.diarization_model_name,
            ModelDefinition.version == settings.diarization_model_version,
        )
    )
    if model is None:
        model = ModelDefinition(
            runtime=runtime,
            task_type=ModelTaskType.DIARIZATION,
            name=settings.diarization_model_name,
            version=settings.diarization_model_version,
            source_uri=settings.diarization_model_name,
        )
        session.add(model)
        await session.flush()
    return model


async def ensure_cleaner_registry(
    session: AsyncSession,
    settings: Settings,
) -> tuple[ModelDefinition, ExternalIntegration]:
    runtime = await session.scalar(
        select(ModelRuntime).where(ModelRuntime.name == "openai-compatible-cleaner")
    )
    if runtime is None:
        runtime = ModelRuntime(
            name="openai-compatible-cleaner",
            kind=ModelRuntimeKind.REMOTE,
        )
        session.add(runtime)
        await session.flush()

    model = await session.scalar(
        select(ModelDefinition).where(
            ModelDefinition.runtime_id == runtime.id,
            ModelDefinition.task_type == ModelTaskType.CLEANING,
            ModelDefinition.name == settings.cleaner_model_name,
            ModelDefinition.version == "configured",
        )
    )
    if model is None:
        model = ModelDefinition(
            runtime=runtime,
            task_type=ModelTaskType.CLEANING,
            name=settings.cleaner_model_name,
            version="configured",
            source_uri=settings.base_url,
        )
        session.add(model)

    integration_version = hashlib.sha256(settings.base_url.encode()).hexdigest()[:12]
    integration = await session.scalar(
        select(ExternalIntegration).where(
            ExternalIntegration.name == "openai-compatible-cleaner-api",
            ExternalIntegration.version == integration_version,
        )
    )
    if integration is None:
        integration = ExternalIntegration(
            name="openai-compatible-cleaner-api",
            version=integration_version,
            kind=ExternalIntegrationKind.HTTP,
            endpoint=settings.base_url,
        )
        session.add(integration)

    await session.flush()
    return model, integration


async def queue_result_cleaning(
    session: AsyncSession,
    result: Result,
    settings: Settings,
    *,
    depends_on: ProcessingJob,
) -> ProcessingJob:
    model, integration = await ensure_cleaner_registry(session, settings)
    job = ProcessingJob(
        result=result,
        stage=ProcessingStage.CLEANING,
        model=model,
        integration=integration,
        dependencies=[depends_on],
    )
    attempt = ProcessingAttempt(
        job=job,
        attempt_number=1,
        status=ProcessingAttemptStatus.QUEUED,
    )
    session.add_all([job, attempt])
    await session.flush()
    enqueue_attempt(session, attempt, ProcessingStage.CLEANING, settings)
    return job


async def _queue_transcription_and_diarization(
    session: AsyncSession,
    result: Result,
    settings: Settings,
) -> tuple[ProcessingJob, ProcessingJob]:
    """Queue TRANSCRIPTION and DIARIZATION as independent root jobs.

    Both read the same source audio and neither depends on the other, so
    they can run in parallel or in either order -- whichever finishes second
    triggers alignment (see align_result_if_ready). This replaces the old
    diarization-depends-on-transcription chain, which forced diarization to
    wait even though it never actually needed the ASR output.
    """
    # Each job (and its attempt) is added to the session immediately after
    # construction, before the *next* ensure_*_registry call flushes: those
    # registry lookups can themselves flush, and a job already linked into a
    # persisted object's relationship collection (via the result=/model=
    # kwargs above) but not yet added to the session trips a SQLAlchemy
    # "will not proceed" cascade warning at that flush.
    transcription_model, integration = await ensure_transcription_registry(session, settings)
    transcription_job = ProcessingJob(
        result=result,
        stage=ProcessingStage.TRANSCRIPTION,
        model=transcription_model,
        integration=integration,
    )
    transcription_attempt = ProcessingAttempt(
        job=transcription_job,
        attempt_number=1,
        status=ProcessingAttemptStatus.QUEUED,
    )
    session.add_all([transcription_job, transcription_attempt])

    diarization_model = await ensure_diarization_registry(session, settings)
    diarization_job = ProcessingJob(
        result=result,
        stage=ProcessingStage.DIARIZATION,
        model=diarization_model,
    )
    diarization_attempt = ProcessingAttempt(
        job=diarization_job,
        attempt_number=1,
        status=ProcessingAttemptStatus.QUEUED,
    )
    session.add_all([diarization_job, diarization_attempt])

    await session.flush()
    enqueue_attempt(
        session, transcription_attempt, ProcessingStage.TRANSCRIPTION, settings
    )
    enqueue_attempt(
        session, diarization_attempt, ProcessingStage.DIARIZATION, settings
    )
    return transcription_job, diarization_job


async def align_result_if_ready(
    session: AsyncSession,
    result: Result,
    meeting_id: uuid.UUID | None,
    settings: Settings,
    storage: ObjectStorage,
) -> ProcessingJob | None:
    """Best-effort: merge ASR words with diarization turns once both are
    available for this Result, then queue cleaning.

    Safe to call from either the ASR or the diarization worker's success
    path, in either order -- whichever call observes both artifacts already
    present is the one that performs the merge; the other call is a no-op.
    Never raises for "not ready yet"; a genuine alignment failure (e.g. no
    speakers detected) is recorded on the ALIGNMENT job/attempt and returned
    to the caller rather than swallowed, since -- unlike "not ready yet" --
    it is a real, actionable outcome the caller should log.
    """
    await session.execute(select(Result.id).where(Result.id == result.id).with_for_update())
    already_aligned = await session.scalar(
        select(ProcessingJob.id).where(
            ProcessingJob.result_id == result.id,
            ProcessingJob.stage == ProcessingStage.ALIGNMENT,
        )
    )
    if already_aligned is not None:
        return None

    transcript_artifact = await session.scalar(
        select(ResultArtifact).where(
            ResultArtifact.result_id == result.id,
            ResultArtifact.artifact_type == ResultArtifactType.TRANSCRIPT_JSON,
        )
    )
    diarization_artifact = await session.scalar(
        select(ResultArtifact).where(
            ResultArtifact.result_id == result.id,
            ResultArtifact.artifact_type == ResultArtifactType.DIARIZATION_JSON,
        )
    )
    if transcript_artifact is None or diarization_artifact is None:
        return None

    transcript_buffer = io.BytesIO()
    await storage.download_object(
        transcript_artifact.minio_bucket, transcript_artifact.minio_key, transcript_buffer
    )
    transcript = json.loads(transcript_buffer.getvalue().decode("utf-8"))

    diarization_buffer = io.BytesIO()
    await storage.download_object(
        diarization_artifact.minio_bucket, diarization_artifact.minio_key, diarization_buffer
    )
    diarization_payload = json.loads(diarization_buffer.getvalue().decode("utf-8"))
    turns = [
        DiarizationTurn(int(turn["start_ms"]), int(turn["end_ms"]), str(turn["speaker_id"]))
        for turn in diarization_payload.get("segments", [])
    ]

    dependency_job_ids = [
        job_id
        for job_id in (transcript_artifact.producer_job_id, diarization_artifact.producer_job_id)
        if job_id is not None
    ]
    dependencies = (
        (await session.scalars(select(ProcessingJob).where(ProcessingJob.id.in_(dependency_job_ids)))).all()
        if dependency_job_ids
        else []
    )
    job = ProcessingJob(result=result, stage=ProcessingStage.ALIGNMENT, dependencies=list(dependencies))
    now = datetime.now(timezone.utc)
    attempt = ProcessingAttempt(
        job=job, attempt_number=1, status=ProcessingAttemptStatus.RUNNING, started_at=now
    )
    session.add_all([job, attempt])
    await session.flush()

    try:
        aligned = align_transcript_to_speakers(
            transcript, turns, diarization_model=diarization_payload.get("model") or ""
        )
    except DiarizationError as error:
        attempt.status = ProcessingAttemptStatus.FAILED
        attempt.finished_at = datetime.now(timezone.utc)
        attempt.error_code = error.code[:100]
        attempt.error_message = str(error)[:2000]
        add_history_event(
            session,
            event_type="processing.failed",
            description="Speaker alignment failed",
            event_data={
                "job_id": str(job.id),
                "stage": ProcessingStage.ALIGNMENT.value,
                "error_code": error.code,
            },
        )
        await session.flush()
        return job

    payload = json.dumps(aligned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key = f"meetings/{meeting_id}/results/{result.id}/speaker-transcript.json"
    await storage.put_object(
        settings.minio_exports_bucket, key, io.BytesIO(payload), len(payload), "application/json"
    )
    try:
        session.add(
            ResultArtifact(
                result=result,
                artifact_type=ResultArtifactType.ALIGNED_TRANSCRIPT_JSON,
                minio_bucket=settings.minio_exports_bucket,
                minio_key=key,
                content_type="application/json",
                checksum_sha256=hashlib.sha256(payload).hexdigest(),
                producer_job=job,
            )
        )
        attempt.status = ProcessingAttemptStatus.SUCCEEDED
        attempt.finished_at = datetime.now(timezone.utc)
        cleaning_job = await queue_result_cleaning(session, result, settings, depends_on=job)
        add_history_event(
            session,
            event_type="processing.succeeded",
            description="Speaker alignment completed",
            event_data={
                "job_id": str(job.id),
                "stage": ProcessingStage.ALIGNMENT.value,
                "word_count": len(aligned.get("words") or []),
            },
        )
        add_history_event(
            session,
            event_type="processing.queued",
            description="Transcript cleaning queued after alignment",
            event_data={"job_id": str(cleaning_job.id), "stage": ProcessingStage.CLEANING.value},
        )
        await session.flush()
    except Exception:
        try:
            await storage.remove_object(settings.minio_exports_bucket, key)
        except Exception:
            pass
        raise
    return job


async def queue_meeting_transcription(
    session: AsyncSession,
    meeting: Meeting,
    settings: Settings,
) -> list[ProcessingJob]:
    # Serialize start/reprocess requests for one meeting. The following active
    # attempt check then remains valid until this transaction commits.
    await session.execute(
        select(Meeting.id).where(Meeting.id == meeting.id).with_for_update()
    )
    active_attempt = await session.scalar(
        select(ProcessingAttempt.id)
        .join(ProcessingAttempt.job)
        .join(ProcessingJob.result)
        .join(Result.voice)
        .where(
            VoiceFile.meeting_id == meeting.id,
            ProcessingJob.stage == ProcessingStage.TRANSCRIPTION,
            ProcessingAttempt.status.in_(
                {ProcessingAttemptStatus.QUEUED, ProcessingAttemptStatus.RUNNING}
            ),
        )
        .limit(1)
    )
    if active_attempt is not None:
        raise ValueError("Meeting transcription is already queued or running")

    voices = (
        await session.scalars(
            select(VoiceFile)
            .where(VoiceFile.meeting_id == meeting.id)
            .order_by(VoiceFile.sequence_number, VoiceFile.uploaded_at)
        )
    ).all()
    if not voices:
        raise ValueError("Meeting has no voice files")

    jobs: list[ProcessingJob] = []
    for voice in voices:
        result = Result(voice=voice)
        session.add(result)
        transcription_job, _diarization_job = await _queue_transcription_and_diarization(
            session, result, settings
        )
        voice.status = VoiceStatus.PENDING
        jobs.append(transcription_job)

    await session.flush()
    return jobs


async def queue_voice_transcription(
    session: AsyncSession,
    voice: VoiceFile,
    settings: Settings,
) -> ProcessingJob:
    """Create the first transcription and diarization run for a newly
    uploaded voice. Returns the transcription job (the response shape
    /meetings/{id}/process and upload callers already expect); the
    diarization job is queued alongside it, independently.
    """
    result = Result(voice=voice)
    session.add(result)
    transcription_job, _diarization_job = await _queue_transcription_and_diarization(
        session, result, settings
    )
    voice.status = VoiceStatus.PENDING
    await session.flush()
    return transcription_job


async def load_processing_job(
    session: AsyncSession,
    job_id: uuid.UUID,
) -> ProcessingJob | None:
    return await session.scalar(
        select(ProcessingJob)
        .options(*JOB_LOAD_OPTIONS)
        .where(ProcessingJob.id == job_id)
    )


async def load_meeting_processing_jobs(
    session: AsyncSession,
    meeting_id: uuid.UUID,
) -> list[ProcessingJob]:
    """Voice-level (TRANSCRIPTION/DIARIZATION/CLEANING) and meeting-level
    (MEETING_COMPOSE) jobs both belong to this listing -- a job has exactly
    one of the two targets (see ProcessingJob's XOR check constraint), so
    both paths are outer-joined and combined with OR.
    """
    return list(
        (
            await session.scalars(
                select(ProcessingJob)
                .outerjoin(ProcessingJob.result)
                .outerjoin(Result.voice)
                .outerjoin(ProcessingJob.meeting_result)
                .options(*JOB_LOAD_OPTIONS)
                .where(
                    or_(
                        VoiceFile.meeting_id == meeting_id,
                        MeetingResult.meeting_id == meeting_id,
                    )
                )
                .order_by(ProcessingJob.created_at.desc())
            )
        ).unique().all()
    )


def processing_job_response(job: ProcessingJob) -> ProcessingJobResponse:
    status = job.current_status.value if job.current_status else "queued"
    if job.result is not None:
        voice = job.result.voice
        return ProcessingJobResponse(
            id=job.id,
            target_type="result",
            result_id=job.result_id,
            voice_id=voice.id,
            meeting_id=voice.meeting_id,
            meeting_result_id=None,
            stage=job.stage.value,
            status=status,
            model_name=job.model.name if job.model else None,
            created_at=job.created_at,
            attempts=job.attempts,
        )
    if job.meeting_result is not None:
        return ProcessingJobResponse(
            id=job.id,
            target_type="meeting_result",
            result_id=None,
            voice_id=None,
            meeting_id=job.meeting_result.meeting_id,
            meeting_result_id=job.meeting_result_id,
            stage=job.stage.value,
            status=status,
            model_name=job.model.name if job.model else None,
            created_at=job.created_at,
            attempts=job.attempts,
        )
    raise ValueError("Processing job has neither a Result nor a MeetingResult target")


# ---------------------------------------------------------------------------
# Meeting Composer coordinator
# ---------------------------------------------------------------------------


def _select_source_artifact(
    result: Result, *, allow_aligned_fallback: bool
) -> ResultArtifact | None:
    cleaned = next(
        (
            artifact
            for artifact in result.artifacts
            if artifact.artifact_type == ResultArtifactType.CLEANED_TEXT
            and artifact.content_type == "application/json"
        ),
        None,
    )
    if cleaned is not None:
        return cleaned
    if not allow_aligned_fallback:
        return None
    return next(
        (
            artifact
            for artifact in result.artifacts
            if artifact.artifact_type == ResultArtifactType.ALIGNED_TRANSCRIPT_JSON
        ),
        None,
    )


def _latest_successful_result(
    voice: VoiceFile, *, allow_aligned_fallback: bool
) -> tuple[Result, ResultArtifact] | None:
    # `voice.results` is already ordered ascending by generated_at (see the
    # relationship's order_by). Reverse the list itself rather than
    # re-sorting with reverse=True: Python's sort is stable, so reverse=True
    # on a key sort leaves *tied* elements in their original (ascending)
    # relative order instead of flipping them -- which would silently prefer
    # the OLDER of two results created within the same timestamp tick
    # (a real risk with whole-second DB clock resolution, e.g. SQLite).
    for result in reversed(voice.results):
        if result.completed_at is None:
            continue
        artifact = _select_source_artifact(result, allow_aligned_fallback=allow_aligned_fallback)
        if artifact is not None:
            return result, artifact
    return None


async def _resolve_source_duration_ms(
    artifact: ResultArtifact,
    voice: VoiceFile,
    storage: ObjectStorage,
) -> int:
    """VoiceFile.duration_ms first (cheap); otherwise read it from the
    transcript artifact's own metrics, falling back to the last segment end.

    No audio preprocessing stage exists yet in this codebase, so
    VoiceFile.duration_ms is normally unset and this artifact read is the
    common path -- reading a small JSON transcript is not the kind of heavy
    ML/GPU work architecture.md's "no heavy processing inside the request"
    rule is about.
    """
    if voice.duration_ms:
        return int(voice.duration_ms)
    buffer = io.BytesIO()
    await storage.download_object(artifact.minio_bucket, artifact.minio_key, buffer)
    transcript = json.loads(buffer.getvalue().decode("utf-8"))
    audio_duration_seconds = (transcript.get("metrics") or {}).get("audio_duration_seconds")
    if audio_duration_seconds:
        return max(1, round(float(audio_duration_seconds) * 1000))
    end_values = [
        int(segment["end_ms"])
        for segment in (transcript.get("segments") or [])
        if segment.get("end_ms") is not None
    ]
    if end_values:
        return max(1, max(end_values))
    raise MeetingCompositionError(
        f"Result {artifact.result_id} has no usable duration signal",
        code="source_timeline_invalid",
    )


async def queue_meeting_composition(
    session: AsyncSession,
    meeting: Meeting,
    settings: Settings,
    storage: ObjectStorage,
    *,
    result_ids: list[uuid.UUID] | None = None,
    allow_aligned_fallback: bool = False,
    force_new_version: bool = False,
) -> tuple[MeetingResult, ProcessingJob | None, bool]:
    """Snapshot the current per-voice Results into a MeetingResult and queue
    a MEETING_COMPOSE job, or return the existing MeetingResult if an
    identical source set was already composed.

    Returns (meeting_result, job, created). `job` is None and `created` is
    False when an identical composition already exists (idempotent replay).
    Raises MeetingCompositionError when the meeting is not ready or an
    explicit source is invalid; the caller decides what to do with that.
    """
    # Serialize concurrent compose requests for this meeting; the active-scan
    # below then stays valid until this transaction commits.
    await session.execute(
        select(Meeting.id).where(Meeting.id == meeting.id).with_for_update()
    )
    voices = (
        await session.scalars(
            select(VoiceFile)
            .options(selectinload(VoiceFile.results).selectinload(Result.artifacts))
            .where(VoiceFile.meeting_id == meeting.id)
            .order_by(VoiceFile.sequence_number, VoiceFile.uploaded_at)
        )
    ).all()
    if not voices:
        raise MeetingCompositionError(
            "Meeting has no voice files", code="meeting_has_no_voices"
        )

    selections: list[tuple[VoiceFile, Result, ResultArtifact]] = []
    if result_ids is not None:
        voices_by_id = {voice.id: voice for voice in voices}
        seen_voice_ids: set[uuid.UUID] = set()
        for result_id in result_ids:
            result = await session.scalar(
                select(Result)
                .options(selectinload(Result.artifacts), selectinload(Result.voice))
                .where(Result.id == result_id)
            )
            if result is None or result.voice.meeting_id != meeting.id:
                raise MeetingCompositionError(
                    f"Result {result_id} does not belong to this meeting",
                    code="source_result_wrong_meeting",
                )
            if result.voice_id in seen_voice_ids:
                raise MeetingCompositionError(
                    "Explicit result selection has more than one result for the same voice",
                    code="ambiguous_voice_sequence",
                )
            seen_voice_ids.add(result.voice_id)
            artifact = _select_source_artifact(
                result, allow_aligned_fallback=allow_aligned_fallback
            )
            if artifact is None:
                raise MeetingCompositionError(
                    f"Result {result_id} has no usable transcript artifact",
                    code="source_artifact_missing",
                )
            selections.append((voices_by_id[result.voice_id], result, artifact))
    else:
        for voice in voices:
            found = _latest_successful_result(
                voice, allow_aligned_fallback=allow_aligned_fallback
            )
            if found is None:
                raise MeetingCompositionError(
                    f"Voice {voice.id} has no successful result yet",
                    code="source_result_not_ready",
                )
            result, artifact = found
            selections.append((voice, result, artifact))

    if len(selections) > 1 and any(
        voice.sequence_number is None for voice, _, _ in selections
    ):
        raise MeetingCompositionError(
            "Meeting has more than one source voice and at least one has no "
            "sequence number",
            code="ambiguous_voice_sequence",
        )
    selections.sort(
        key=lambda item: (
            item[0].sequence_number if item[0].sequence_number is not None else 0,
            item[0].uploaded_at,
            item[0].id,
        )
    )

    durations_ms = [
        await _resolve_source_duration_ms(artifact, voice, storage)
        for voice, _, artifact in selections
    ]
    offsets_ms = compute_offsets(durations_ms, gap_ms=settings.meeting_composer_gap_ms)

    fingerprint_entries = [
        {
            "result_id": str(result.id),
            "source_artifact_id": str(artifact.id),
            "checksum_sha256": artifact.checksum_sha256 or "",
            "position": position,
            "duration_ms": duration_ms,
        }
        for position, ((_, result, artifact), duration_ms) in enumerate(
            zip(selections, durations_ms)
        )
    ]
    if force_new_version:
        fingerprint_entries.append(
            {
                "result_id": "__nonce__",
                "source_artifact_id": str(uuid.uuid4()),
                "checksum_sha256": "",
                "position": len(fingerprint_entries),
                "duration_ms": 0,
            }
        )
    fingerprint = compute_source_fingerprint(
        fingerprint_entries, policy_version=FINGERPRINT_POLICY_VERSION
    )

    if not force_new_version:
        existing = await session.scalar(
            select(MeetingResult).where(
                MeetingResult.meeting_id == meeting.id,
                MeetingResult.source_fingerprint == fingerprint,
            )
        )
        if existing is not None:
            return existing, None, False

    meeting_result = MeetingResult(meeting=meeting, source_fingerprint=fingerprint)
    session.add(meeting_result)
    await session.flush()

    producer_job_ids: list[uuid.UUID] = []
    for position, ((voice, result, artifact), duration_ms, offset_ms) in enumerate(
        zip(selections, durations_ms, offsets_ms)
    ):
        session.add(
            MeetingResultSource(
                meeting_result=meeting_result,
                result=result,
                position=position,
                voice_sequence_snapshot=voice.sequence_number,
                source_offset_ms=offset_ms,
                source_duration_ms=duration_ms,
                source_artifact=artifact,
            )
        )
        if artifact.producer_job_id is not None:
            producer_job_ids.append(artifact.producer_job_id)

    dependencies = (
        (
            await session.scalars(
                select(ProcessingJob).where(ProcessingJob.id.in_(producer_job_ids))
            )
        ).all()
        if producer_job_ids
        else []
    )
    job = ProcessingJob(
        meeting_result=meeting_result,
        stage=ProcessingStage.MEETING_COMPOSE,
        dependencies=list(dependencies),
    )
    session.add(job)
    await session.flush()
    session.add(
        ProcessingAttempt(job=job, attempt_number=1, status=ProcessingAttemptStatus.QUEUED)
    )
    await session.flush()
    return meeting_result, job, True


async def queue_meeting_composition_if_ready(
    session: AsyncSession,
    voice: VoiceFile,
    settings: Settings,
    storage: ObjectStorage,
) -> ProcessingJob | None:
    """Best-effort automatic trigger, called after a voice finishes cleaning.

    Never raises for "not ready yet" -- that is the normal case while other
    voices in the meeting are still processing. Only ever composes from
    CLEANED_TEXT (allow_aligned_fallback=False): automatic composition must
    not silently fall back to a lesser transcript.
    """
    if voice.meeting_id is None:
        return None
    meeting = await session.get(Meeting, voice.meeting_id)
    if meeting is None:
        return None
    try:
        _, job, created = await queue_meeting_composition(
            session, meeting, settings, storage, allow_aligned_fallback=False
        )
    except MeetingCompositionError:
        return None
    return job if created else None


MEETING_RESULT_LOAD_OPTIONS = (
    selectinload(MeetingResult.sources).selectinload(MeetingResultSource.result),
    selectinload(MeetingResult.artifacts),
    selectinload(MeetingResult.processing_jobs).selectinload(ProcessingJob.attempts),
)


async def load_meeting_result(
    session: AsyncSession,
    meeting_result_id: uuid.UUID,
) -> MeetingResult | None:
    return await session.scalar(
        select(MeetingResult)
        .options(*MEETING_RESULT_LOAD_OPTIONS)
        .where(MeetingResult.id == meeting_result_id)
    )


async def list_meeting_results(
    session: AsyncSession,
    meeting_id: uuid.UUID,
) -> list[MeetingResult]:
    return list(
        (
            await session.scalars(
                select(MeetingResult)
                .options(*MEETING_RESULT_LOAD_OPTIONS)
                .where(MeetingResult.meeting_id == meeting_id)
                .order_by(MeetingResult.generated_at.desc())
            )
        )
        .unique()
        .all()
    )


async def load_latest_completed_meeting_result(
    session: AsyncSession,
    meeting_id: uuid.UUID,
) -> MeetingResult | None:
    return await session.scalar(
        select(MeetingResult)
        .options(*MEETING_RESULT_LOAD_OPTIONS)
        .where(
            MeetingResult.meeting_id == meeting_id,
            MeetingResult.completed_at.isnot(None),
        )
        .order_by(MeetingResult.completed_at.desc())
        .limit(1)
    )
