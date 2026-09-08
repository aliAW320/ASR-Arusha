import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from anyio import to_thread
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
    ApproveMeetingPublicationRequest,
    MeetingAttachmentResponse,
    MeetingPublicationResponse,
)
from ...services.audit import add_history_event
from ...services.permissions import MeetingPermission, require_meeting_permission
from ...services.processing import (
    load_latest_completed_meeting_result,
    queue_meeting_publication,
)
from ...storage.base import ObjectStorage
from ...storage.minio import get_object_storage


router = APIRouter(prefix="/meetings", tags=["meeting-publication"])
SUPPORTED_ATTACHMENTS = {
    "image/png": ((".png",), b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": ((".jpg", ".jpeg"), b"\xff\xd8\xff"),
    "application/pdf": ((".pdf",), b"%PDF-"),
}
ACTIVE_PUBLICATION_STATUSES = {
    MeetingPublicationStatus.QUEUED,
    MeetingPublicationStatus.RUNNING,
}


def _safe_filename(filename: str | None) -> str:
    name = Path(filename or "attachment.bin").name
    return "".join(
        character if character.isalnum() or character in ".-_" else "_"
        for character in name
    )


async def _inspect_attachment(
    upload: UploadFile, max_bytes: int
) -> tuple[int, str, bytes]:
    def inspect() -> tuple[int, str, bytes]:
        upload.file.seek(0)
        digest = hashlib.sha256()
        size = 0
        prefix = b""
        while chunk := upload.file.read(1024 * 1024):
            if not prefix:
                prefix = chunk[:16]
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("Attachment exceeds the configured upload limit")
            digest.update(chunk)
        upload.file.seek(0)
        return size, digest.hexdigest(), prefix

    return await to_thread.run_sync(inspect)


def _validate_attachment(upload: UploadFile, prefix: bytes) -> str:
    content_type = (upload.content_type or "").lower()
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    contract = SUPPORTED_ATTACHMENTS.get(content_type)
    if contract is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PNG, JPEG, and PDF attachments are accepted",
        )
    suffixes, signature = contract
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in suffixes or not prefix.startswith(signature):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Attachment content does not match its file type",
        )
    return content_type


def _normalize_destination_path(value: str) -> str:
    parts = [part.strip() for part in value.strip().strip("/").split("/")]
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise HTTPException(status_code=422, detail="Destination path is invalid")
    return "/".join(parts)


def _default_publication(meeting_id: uuid.UUID) -> MeetingPublicationResponse:
    settings = get_settings()
    return MeetingPublicationResponse(
        meeting_id=meeting_id,
        status=MeetingPublicationStatus.AWAITING_APPROVAL,
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
        size_bytes, checksum, prefix = await _inspect_attachment(
            upload, settings.meeting_attachment_max_bytes
        )
    except ValueError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    if size_bytes == 0:
        raise HTTPException(status_code=422, detail="Attachment cannot be empty")
    content_type = _validate_attachment(upload, prefix)

    attachment_id = uuid.uuid4()
    bucket = settings.minio_meetings_bucket
    filename = _safe_filename(upload.filename)
    object_key = (
        f"meetings/{meeting_id}/attachments/{attachment_id}/source/{filename}"
    )
    try:
        await storage.put_object(
            bucket, object_key, upload.file, size_bytes, content_type
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail="Object storage upload failed") from error

    attachment = MeetingAttachment(
        id=attachment_id,
        meeting=meeting,
        uploaded_by=current_user,
        minio_bucket=bucket,
        minio_key=object_key,
        original_filename=upload.filename or filename,
        content_type=content_type,
        size_bytes=size_bytes,
        checksum_sha256=checksum,
    )
    session.add(attachment)
    if publication is not None:
        publication.status = MeetingPublicationStatus.AWAITING_APPROVAL
        publication.approved_by_id = None
        publication.approved_at = None
        publication.error_code = None
        publication.error_message = None
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
    if publication is not None:
        publication.status = MeetingPublicationStatus.AWAITING_APPROVAL
        publication.approved_by_id = None
        publication.approved_at = None
        publication.error_code = None
        publication.error_message = None
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


@router.post(
    "/{meeting_id}/publication/approve",
    response_model=MeetingPublicationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_publication(
    meeting_id: uuid.UUID,
    payload: ApproveMeetingPublicationRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.EDIT
    )
    meeting_result = await load_latest_completed_meeting_result(session, meeting_id)
    if meeting_result is None:
        raise HTTPException(
            status_code=409,
            detail="A completed meeting transcript is required before approval",
        )
    publication = await _lock_publication(session, meeting_id)
    if publication is not None and publication.status in ACTIVE_PUBLICATION_STATUSES:
        raise HTTPException(status_code=409, detail="Publication is already active")

    destination_path = _normalize_destination_path(payload.destination_path)
    attachment_ids = [
        str(value)
        for value in await session.scalars(
            select(MeetingAttachment.id)
            .where(MeetingAttachment.meeting_id == meeting_id)
            .order_by(MeetingAttachment.created_at, MeetingAttachment.id)
        )
    ]
    if publication is None:
        publication = MeetingPublication(meeting=meeting)
        session.add(publication)
    publication.meeting_result = meeting_result
    publication.destination_path = destination_path
    publication.approved_by = current_user
    publication.approved_at = datetime.now(timezone.utc)
    publication.attachment_ids = attachment_ids
    publication.status = MeetingPublicationStatus.QUEUED
    publication.vision_status = PublicationVisionStatus.NOT_REQUESTED
    publication.error_code = None
    publication.error_message = None
    publication.completed_at = None

    job = await queue_meeting_publication(session, meeting_result, get_settings())
    publication.current_job = job
    add_history_event(
        session,
        event_type="meeting_publication.approved",
        description="Meeting publication approved and queued",
        actor=current_user,
        request=request,
        event_data={
            "publication_id": str(publication.id),
            "meeting_result_id": str(meeting_result.id),
            "job_id": str(job.id),
            "destination_path": destination_path,
            "attachment_count": len(attachment_ids),
        },
        affected_meetings=[meeting],
        affected_meeting_results=[meeting_result],
    )
    await session.commit()
    await session.refresh(publication)
    return publication
