import io
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...database import get_db_session
from ...dependencies import get_current_user
from ...models import MeetingArtifactType, MeetingMember, MeetingMemberRole, MeetingResult, User
from ...schemas import (
    ComposeMeetingRequest,
    MeetingResultResponse,
    MeetingTranscriptResponse,
)
from ...services.audit import add_history_event
from ...services.permissions import MeetingPermission, is_admin, require_meeting_permission
from ...services.processing import (
    MeetingCompositionError,
    list_meeting_results,
    load_latest_completed_meeting_result,
    load_meeting_result,
    queue_meeting_composition,
)
from ...storage.base import ObjectStorage
from ...storage.minio import get_object_storage


router = APIRouter(tags=["meeting-results"])


async def _require_owner_or_admin(
    session: AsyncSession, current_user: User, meeting_id: uuid.UUID
) -> None:
    if is_admin(current_user):
        return
    role = await session.scalar(
        select(MeetingMember.role).where(
            MeetingMember.meeting_id == meeting_id,
            MeetingMember.user_id == current_user.id,
        )
    )
    if role != MeetingMemberRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the meeting owner or an admin may force a new version",
        )


def _meeting_result_response(meeting_result: MeetingResult) -> MeetingResultResponse:
    return MeetingResultResponse.model_validate(meeting_result)


def _processing_status(meeting_result: MeetingResult) -> tuple[str, dict | None]:
    jobs = sorted(
        meeting_result.processing_jobs, key=lambda job: job.created_at, reverse=True
    )
    if not jobs:
        return "queued", None
    latest_attempt = jobs[0].latest_attempt
    if latest_attempt is None:
        return "queued", None
    error = None
    if latest_attempt.status.value == "failed" and (
        latest_attempt.error_code or latest_attempt.error_message
    ):
        error = {
            "stage": jobs[0].stage.value,
            "code": latest_attempt.error_code,
            "message": latest_attempt.error_message,
            "attempt_number": latest_attempt.attempt_number,
        }
    return latest_attempt.status.value, error


async def _meeting_transcript_response(
    meeting_result: MeetingResult, storage: ObjectStorage
) -> MeetingTranscriptResponse:
    artifact = next(
        (
            item
            for item in meeting_result.artifacts
            if item.artifact_type == MeetingArtifactType.COMBINED_TRANSCRIPT_JSON
        ),
        None,
    )
    if artifact is None:
        raise HTTPException(status_code=409, detail="Meeting transcript is not ready")
    buffer = io.BytesIO()
    try:
        await storage.download_object(artifact.minio_bucket, artifact.minio_key, buffer)
        payload = json.loads(buffer.getvalue().decode("utf-8"))
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="Meeting transcript could not be read"
        ) from error
    processing_status, processing_error = _processing_status(meeting_result)
    return MeetingTranscriptResponse(
        processing_status=processing_status, processing_error=processing_error, **payload
    )


@router.post(
    "/meetings/{meeting_id}/compose",
    response_model=MeetingResultResponse,
)
async def compose_meeting(
    meeting_id: uuid.UUID,
    payload: ComposeMeetingRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    response: Response,
):
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.MANAGE_VOICES
    )
    if payload.force_new_version:
        await _require_owner_or_admin(session, current_user, meeting_id)

    try:
        meeting_result, job, created = await queue_meeting_composition(
            session,
            meeting,
            get_settings(),
            storage,
            result_ids=payload.result_ids,
            allow_aligned_fallback=payload.allow_aligned_fallback,
            force_new_version=payload.force_new_version,
        )
    except MeetingCompositionError as error:
        raise HTTPException(
            status_code=409, detail={"code": error.code, "message": str(error)}
        ) from error

    if created and job is not None:
        add_history_event(
            session,
            event_type="meeting_composition.queued",
            description="Meeting composition queued",
            actor=current_user,
            event_data={
                "job_id": str(job.id),
                "meeting_result_id": str(meeting_result.id),
                "stage": "meeting_compose",
            },
            affected_meetings=[meeting],
            affected_meeting_results=[meeting_result],
        )
        await session.commit()
        response.status_code = status.HTTP_202_ACCEPTED
    else:
        response.status_code = status.HTTP_200_OK

    loaded = await load_meeting_result(session, meeting_result.id)
    return _meeting_result_response(loaded)


@router.get(
    "/meetings/{meeting_id}/results",
    response_model=list[MeetingResultResponse],
)
async def list_meeting_result_versions(
    meeting_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    await require_meeting_permission(session, current_user, meeting_id, MeetingPermission.VIEW)
    results = await list_meeting_results(session, meeting_id)
    return [_meeting_result_response(result) for result in results]


@router.get(
    "/meeting-results/{meeting_result_id}",
    response_model=MeetingResultResponse,
)
async def get_meeting_result(
    meeting_result_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    meeting_result = await load_meeting_result(session, meeting_result_id)
    if meeting_result is None:
        raise HTTPException(status_code=404, detail="Meeting result not found")
    await require_meeting_permission(
        session, current_user, meeting_result.meeting_id, MeetingPermission.VIEW
    )
    return _meeting_result_response(meeting_result)


@router.get(
    "/meeting-results/{meeting_result_id}/transcript",
    response_model=MeetingTranscriptResponse,
)
async def get_meeting_result_transcript(
    meeting_result_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
):
    meeting_result = await load_meeting_result(session, meeting_result_id)
    if meeting_result is None:
        raise HTTPException(status_code=404, detail="Meeting result not found")
    await require_meeting_permission(
        session, current_user, meeting_result.meeting_id, MeetingPermission.VIEW
    )
    return await _meeting_transcript_response(meeting_result, storage)


@router.get(
    "/meetings/{meeting_id}/transcript",
    response_model=MeetingTranscriptResponse,
)
async def get_latest_meeting_transcript(
    meeting_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
):
    await require_meeting_permission(session, current_user, meeting_id, MeetingPermission.VIEW)
    meeting_result = await load_latest_completed_meeting_result(session, meeting_id)
    if meeting_result is None:
        raise HTTPException(status_code=409, detail="No completed meeting transcript yet")
    return await _meeting_transcript_response(meeting_result, storage)
