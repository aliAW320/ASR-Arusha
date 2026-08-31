import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from ..config import Settings
from ..models import (
    ExternalIntegration,
    ExternalIntegrationKind,
    Meeting,
    ModelDefinition,
    ModelRuntime,
    ModelRuntimeKind,
    ModelTaskType,
    ProcessingAttempt,
    ProcessingAttemptStatus,
    ProcessingJob,
    ProcessingStage,
    Result,
    VoiceFile,
    VoiceStatus,
)
from ..schemas import ProcessingJobResponse


JOB_LOAD_OPTIONS = (
    joinedload(ProcessingJob.result).joinedload(Result.voice),
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

    model, integration = await ensure_transcription_registry(session, settings)
    jobs: list[ProcessingJob] = []
    for voice in voices:
        result = Result(voice=voice)
        job = ProcessingJob(
            result=result,
            stage=ProcessingStage.TRANSCRIPTION,
            model=model,
            integration=integration,
        )
        attempt = ProcessingAttempt(
            job=job,
            attempt_number=1,
            status=ProcessingAttemptStatus.QUEUED,
        )
        voice.status = VoiceStatus.PENDING
        session.add_all([result, job, attempt])
        jobs.append(job)

    await session.flush()
    return jobs


async def queue_voice_transcription(
    session: AsyncSession,
    voice: VoiceFile,
    settings: Settings,
) -> ProcessingJob:
    """Create the first transcription run for a newly uploaded voice."""
    model, integration = await ensure_transcription_registry(session, settings)
    result = Result(voice=voice)
    job = ProcessingJob(
        result=result,
        stage=ProcessingStage.TRANSCRIPTION,
        model=model,
        integration=integration,
    )
    attempt = ProcessingAttempt(
        job=job,
        attempt_number=1,
        status=ProcessingAttemptStatus.QUEUED,
    )
    voice.status = VoiceStatus.PENDING
    session.add_all([result, job, attempt])
    await session.flush()
    return job


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
    return list(
        (
            await session.scalars(
                select(ProcessingJob)
                .join(ProcessingJob.result)
                .join(Result.voice)
                .options(*JOB_LOAD_OPTIONS)
                .where(VoiceFile.meeting_id == meeting_id)
                .order_by(ProcessingJob.created_at.desc())
            )
        ).unique().all()
    )


def processing_job_response(job: ProcessingJob) -> ProcessingJobResponse:
    if job.result is None:
        raise ValueError("Only voice-level processing jobs are currently exposed")
    voice = job.result.voice
    return ProcessingJobResponse(
        id=job.id,
        result_id=job.result_id,
        voice_id=voice.id,
        meeting_id=voice.meeting_id,
        stage=job.stage.value,
        status=job.current_status.value if job.current_status else "queued",
        model_name=job.model.name if job.model else None,
        created_at=job.created_at,
        attempts=job.attempts,
    )
