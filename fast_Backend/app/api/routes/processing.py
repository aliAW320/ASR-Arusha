import io
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...config import get_settings
from ...database import get_db_session
from ...dependencies import get_current_user
from ...models import (
    ProcessingAttemptStatus,
    ProcessingJob,
    ProcessingStage,
    Result,
    ResultArtifactType,
    User,
    VoiceFile,
)
from ...schemas import ProcessingJobResponse, ResultResponse, TranscriptResponse
from ...services.audit import add_history_event
from ...services.permissions import MeetingPermission, is_admin, require_meeting_permission
from ...services.processing import (
    JOB_LOAD_OPTIONS,
    load_meeting_processing_jobs,
    load_processing_job,
    processing_job_response,
    queue_meeting_transcription,
)
from ...storage.base import ObjectStorage
from ...storage.minio import get_object_storage


router = APIRouter(tags=["processing"])


async def _require_voice_access(
    session: AsyncSession,
    current_user: User,
    voice: VoiceFile,
    permission: MeetingPermission = MeetingPermission.VIEW,
) -> None:
    if voice.meeting_id is None:
        if not is_admin(current_user):
            raise HTTPException(status_code=403, detail="Insufficient permission")
        return
    await require_meeting_permission(session, current_user, voice.meeting_id, permission)


@router.post(
    "/meetings/{meeting_id}/process",
    response_model=list[ProcessingJobResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
async def process_meeting(
    meeting_id: uuid.UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.MANAGE_VOICES
    )
    try:
        jobs = await queue_meeting_transcription(session, meeting, get_settings())
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    voices = [job.result.voice for job in jobs]
    add_history_event(
        session,
        event_type="processing.queued",
        description="Meeting transcription queued",
        actor=current_user,
        request=request,
        event_data={"stage": "transcription", "job_count": len(jobs)},
        affected_meetings=[meeting],
        affected_voices=voices,
    )
    await session.commit()
    loaded = [await load_processing_job(session, job.id) for job in jobs]
    return [processing_job_response(job) for job in loaded if job is not None]


@router.get(
    "/meetings/{meeting_id}/processing",
    response_model=list[ProcessingJobResponse],
)
async def get_meeting_processing(
    meeting_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    await require_meeting_permission(session, current_user, meeting_id, MeetingPermission.VIEW)
    jobs = await load_meeting_processing_jobs(session, meeting_id)
    return [processing_job_response(job) for job in jobs]


@router.get("/processing/jobs/{job_id}", response_model=ProcessingJobResponse)
async def get_processing_job(
    job_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    job = await load_processing_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Processing job not found")
    if job.result is not None:
        await _require_voice_access(session, current_user, job.result.voice)
    elif job.meeting_result is not None:
        await require_meeting_permission(
            session, current_user, job.meeting_result.meeting_id, MeetingPermission.VIEW
        )
    else:
        raise HTTPException(status_code=404, detail="Processing job not found")
    return processing_job_response(job)


@router.get("/voices/{voice_id}/results", response_model=list[ResultResponse])
async def list_voice_results(
    voice_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    voice = await session.get(VoiceFile, voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail="Voice file not found")
    await _require_voice_access(session, current_user, voice)
    results = (
        await session.scalars(
            select(Result)
            .options(selectinload(Result.artifacts))
            .where(Result.voice_id == voice_id)
            .order_by(Result.generated_at.desc())
        )
    ).all()
    return results


@router.get("/results/{result_id}", response_model=ResultResponse)
async def get_result(
    result_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    result = await session.scalar(
        select(Result)
        .options(selectinload(Result.artifacts), selectinload(Result.voice))
        .where(Result.id == result_id)
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found")
    await _require_voice_access(session, current_user, result.voice)
    return result


@router.get("/results/{result_id}/transcript", response_model=TranscriptResponse)
async def get_result_transcript(
    result_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
):
    result = await session.scalar(
        select(Result)
        .options(
            selectinload(Result.artifacts),
            selectinload(Result.voice),
            selectinload(Result.processing_jobs).selectinload(ProcessingJob.attempts),
        )
        .where(Result.id == result_id)
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found")
    await _require_voice_access(session, current_user, result.voice)
    artifact = next(
        (
            item
            for item in result.artifacts
            if item.artifact_type == ResultArtifactType.CLEANED_TEXT
            and item.content_type == "application/json"
        ),
        None,
    )
    if artifact is None:
        artifact = next(
            (
                item
                for item in result.artifacts
                if item.artifact_type == ResultArtifactType.ALIGNED_TRANSCRIPT_JSON
            ),
            None,
        )
    if artifact is None:
        artifact = next(
            (
                item
                for item in result.artifacts
                if item.artifact_type == ResultArtifactType.TRANSCRIPT_JSON
            ),
            None,
        )
    if artifact is None:
        raise HTTPException(status_code=409, detail="Transcript is not ready")
    buffer = io.BytesIO()
    try:
        await storage.download_object(artifact.minio_bucket, artifact.minio_key, buffer)
        payload = json.loads(buffer.getvalue().decode("utf-8"))
    except Exception as error:
        raise HTTPException(status_code=503, detail="Transcript could not be read") from error
    cleaner_job = next(
        (
            job
            for job in sorted(
                result.processing_jobs,
                key=lambda item: item.created_at,
                reverse=True,
            )
            if job.stage == ProcessingStage.CLEANING
        ),
        None,
    )
    diarization_job = next(
        (
            job
            for job in sorted(
                result.processing_jobs,
                key=lambda item: item.created_at,
                reverse=True,
            )
            if job.stage == ProcessingStage.DIARIZATION
        ),
        None,
    )
    processing_status = "transcription_succeeded"
    processing_error = None
    status_job = cleaner_job or diarization_job
    if status_job is not None:
        latest_attempt = status_job.latest_attempt
        processing_status = (
            latest_attempt.status.value if latest_attempt else ProcessingAttemptStatus.QUEUED.value
        )
        failed_attempt = (
            latest_attempt
            if latest_attempt
            and latest_attempt.status == ProcessingAttemptStatus.FAILED
            and (latest_attempt.error_code or latest_attempt.error_message)
            else None
        )
        if failed_attempt is not None:
            processing_error = {
                "stage": status_job.stage.value,
                "code": failed_attempt.error_code,
                "message": failed_attempt.error_message,
                "attempt_number": failed_attempt.attempt_number,
            }
    return TranscriptResponse(
        result_id=result.id,
        processing_status=processing_status,
        processing_error=processing_error,
        **payload,
    )
