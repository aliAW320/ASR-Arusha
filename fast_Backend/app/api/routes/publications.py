import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...database import get_db_session
from ...dependencies import get_current_user
from ...models import (
    MeetingAttachment,
    MeetingPublication,
    MeetingPublicationStatus,
    PublicationVisionStatus,
    User,
)
from ...schemas import (
    MeetingAttachmentResponse,
    MeetingPublicationResponse,
    UpdateMeetingPublicationRequest,
)
from ...services.audit import add_history_event
from ...services.permissions import MeetingPermission, require_meeting_permission
from ...services.uploads import UploadRejected, store_attachment
from ...storage.base import ObjectStorage
from ...storage.minio import get_object_storage


router = APIRouter(prefix="/meetings", tags=["meeting-publication"])
ACTIVE_PUBLICATION_STATUSES = {
    MeetingPublicationStatus.QUEUED,
    MeetingPublicationStatus.RUNNING,
}


def _normalize_destination_path(value: str) -> str:
    parts = [part.strip() for part in value.strip().strip("/").split("/")]
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise HTTPException(status_code=422, detail="Destination path is invalid")
    return "/".join(parts)


def _default_publication(meeting_id: uuid.UUID) -> MeetingPublicationResponse:
    settings = get_settings()
    return MeetingPublicationResponse(
        meeting_id=meeting_id,
        status=MeetingPublicationStatus.PENDING,
        destination_path=settings.meeting_publication_default_path,
        vision_status=PublicationVisionStatus.NOT_REQUESTED,
    )


async def _lock_publication(
    session: AsyncSession, meeting_id: uuid.UUID
) -> MeetingPublication | None:
    return await session.scalar(
        select(MeetingPublication)
        .where(MeetingPublication.meeting_id == meeting_id)
        .with_for_update()
    )


@router.get(
    "/{meeting_id}/attachments", response_model=list[MeetingAttachmentResponse]
)
async def list_attachments(
    meeting_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.VIEW
    )
    return list(
        await session.scalars(
            select(MeetingAttachment)
            .where(MeetingAttachment.meeting_id == meeting_id)
            .order_by(MeetingAttachment.created_at, MeetingAttachment.id)
        )
    )


@router.post(
    "/{meeting_id}/attachments",
    response_model=MeetingAttachmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    meeting_id: uuid.UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    upload: Annotated[UploadFile, File()],
):
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.EDIT
    )
    publication = await _lock_publication(session, meeting_id)
    if publication is not None and publication.status in ACTIVE_PUBLICATION_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Attachments cannot change while publication is active",
        )

    settings = get_settings()
    try:
        attachment, written = await store_attachment(
            session,
            storage,
            meeting=meeting,
            actor=current_user,
            upload=upload,
            settings=settings,
        )
    except UploadRejected as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="Object storage upload failed") from error
    bucket, object_key = written
    content_type = attachment.content_type
    size_bytes = attachment.size_bytes
    add_history_event(
        session,
        event_type="meeting_attachment.uploaded",
        description="Meeting attachment uploaded",
        actor=current_user,
        request=request,
        event_data={
            "attachment_id": str(attachment.id),
            "filename": attachment.original_filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
        },
        affected_meetings=[meeting],
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        try:
            await storage.remove_object(bucket, object_key)
        except Exception:
            pass
        raise
    await session.refresh(attachment)
    return attachment


@router.delete("/{meeting_id}/attachments/{attachment_id}", status_code=204)
async def delete_attachment(
    meeting_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
):
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.EDIT
    )
    publication = await _lock_publication(session, meeting_id)
    if publication is not None and publication.status in ACTIVE_PUBLICATION_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Attachments cannot change while publication is active",
        )
    attachment = await session.get(MeetingAttachment, attachment_id)
    if attachment is None or attachment.meeting_id != meeting_id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    try:
        await storage.remove_object(attachment.minio_bucket, attachment.minio_key)
    except Exception as error:
        raise HTTPException(status_code=503, detail="Object storage delete failed") from error
    add_history_event(
        session,
        event_type="meeting_attachment.deleted",
        description="Meeting attachment deleted",
        actor=current_user,
        request=request,
        event_data={
            "attachment_id": str(attachment.id),
            "filename": attachment.original_filename,
        },
        affected_meetings=[meeting],
    )
    await session.delete(attachment)
    await session.commit()


@router.get("/{meeting_id}/publication", response_model=MeetingPublicationResponse)
async def get_publication(
    meeting_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.VIEW
    )
    publication = await session.scalar(
        select(MeetingPublication).where(MeetingPublication.meeting_id == meeting_id)
    )
    return publication or _default_publication(meeting_id)


@router.patch("/{meeting_id}/publication", response_model=MeetingPublicationResponse)
async def update_publication_destination(
    meeting_id: uuid.UUID,
    payload: UpdateMeetingPublicationRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    """Point this meeting at a different knowledge-base path.

    Publication itself runs automatically once the meeting transcript is
    composed, so this only chooses *where* the documents land. It is refused
    while a publish is in flight, since the worker has already read the path.
    """
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.EDIT
    )
    publication = await _lock_publication(session, meeting_id)
    if publication is not None and publication.status in ACTIVE_PUBLICATION_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="The destination cannot change while publication is active",
        )

    destination_path = _normalize_destination_path(payload.destination_path)
    if publication is None:
        publication = MeetingPublication(meeting=meeting)
        session.add(publication)
    publication.destination_path = destination_path

    add_history_event(
        session,
        event_type="meeting_publication.destination_changed",
        description="Knowledge-base destination updated",
        actor=current_user,
        request=request,
        event_data={"destination_path": destination_path},
        affected_meetings=[meeting],
    )
    await session.commit()
    await session.refresh(publication)
    return publication
