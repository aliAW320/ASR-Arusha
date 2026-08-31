import hashlib
import uuid
from pathlib import Path
from typing import Annotated

from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...database import get_db_session
from ...dependencies import get_current_user
from ...models import Meeting, User, VoiceFile
from ...observability.context import bind_log_context
from ...observability.events import LogEvent
from ...observability.logging import get_logger
from ...schemas import VoiceResponse
from ...services.audit import add_history_event
from ...services.permissions import (
    MeetingPermission,
    is_admin,
    require_meeting_permission,
)
from ...services.processing import queue_voice_transcription
from ...storage.base import ObjectStorage
from ...storage.minio import get_object_storage


router = APIRouter(tags=["voices"])
DB_WRITE_ATTEMPTS = 3
logger = get_logger(__name__)


async def _inspect_upload(upload: UploadFile, max_bytes: int) -> tuple[int, str]:
    def inspect() -> tuple[int, str]:
        upload.file.seek(0)
        digest = hashlib.sha256()
        size = 0
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("Voice file exceeds the configured upload limit")
            digest.update(chunk)
        upload.file.seek(0)
        return size, digest.hexdigest()

    return await to_thread.run_sync(inspect)


def _safe_filename(filename: str | None) -> str:
    name = Path(filename or "voice.bin").name
    return "".join(character if character.isalnum() or character in ".-_" else "_" for character in name)


async def _remove_uploaded_object(
    storage: ObjectStorage,
    bucket: str,
    object_key: str,
) -> None:
    try:
        await storage.remove_object(bucket, object_key)
    except Exception as error:
        logger.exception(
            LogEvent.UPLOAD_CLEANUP_FAILED,
            "Uploaded object cleanup failed",
            error=error,
            bucket=bucket,
            object_key=object_key,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload metadata failed and the uploaded object could not be cleaned up",
        ) from error


@router.post(
    "/meetings/{meeting_id}/voices",
    response_model=VoiceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_voice(
    meeting_id: uuid.UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    upload: Annotated[UploadFile, File()],
    sequence_number: Annotated[int | None, Form(ge=0)] = None,
):
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.MANAGE_VOICES
    )
    content_type = upload.content_type or "application/octet-stream"
    if not content_type.startswith("audio/"):
        raise HTTPException(status_code=415, detail="Only audio uploads are accepted")

    settings = get_settings()
    try:
        size_bytes, checksum = await _inspect_upload(upload, settings.voice_upload_max_bytes)
    except ValueError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    if size_bytes == 0:
        raise HTTPException(status_code=422, detail="Voice file cannot be empty")

    voice_id = uuid.uuid4()
    bind_log_context(voice_id=str(voice_id))
    bucket = settings.minio_meetings_bucket
    filename = _safe_filename(upload.filename)
    object_key = f"meetings/{meeting_id}/voices/{voice_id}/source/{filename}"
    try:
        await storage.put_object(bucket, object_key, upload.file, size_bytes, content_type)
    except Exception as error:
        logger.exception(
            LogEvent.OBJECT_STORAGE_UPLOAD_FAILED,
            "Voice upload to object storage failed",
            error=error,
            bucket=bucket,
            object_key=object_key,
            size_bytes=size_bytes,
        )
        raise HTTPException(status_code=503, detail="Object storage upload failed") from error

    actor_id = current_user.id
    last_error: SQLAlchemyError | None = None
    for attempt_number in range(DB_WRITE_ATTEMPTS):
        try:
            actor = (
                current_user
                if attempt_number == 0
                else await session.get(User, actor_id)
            )
            meeting_for_event = (
                meeting
                if attempt_number == 0
                else await session.get(Meeting, meeting_id)
            )
            selected_sequence = sequence_number
            if selected_sequence is None:
                maximum = await session.scalar(
                    select(func.max(VoiceFile.sequence_number)).where(
                        VoiceFile.meeting_id == meeting_id
                    )
                )
                selected_sequence = (maximum if maximum is not None else -1) + 1
            voice = VoiceFile(
                id=voice_id,
                minio_bucket=bucket,
                minio_key=object_key,
                original_filename=upload.filename,
                content_type=content_type,
                size_bytes=size_bytes,
                checksum_sha256=checksum,
                sequence_number=selected_sequence,
                meeting_id=meeting_id,
                uploaded_by_id=actor_id,
            )
            session.add(voice)
            add_history_event(
                session,
                event_type="voice.uploaded",
                description="Voice file uploaded",
                actor=actor,
                request=request,
                event_data={"filename": upload.filename, "size_bytes": size_bytes},
                affected_meetings=[meeting_for_event],
                affected_voices=[voice],
            )
            await queue_voice_transcription(session, voice, settings)
            add_history_event(
                session,
                event_type="processing.queued",
                description="Voice transcription queued after upload",
                actor=actor,
                request=request,
                event_data={"stage": "transcription", "trigger": "voice.upload"},
                affected_meetings=[meeting_for_event],
                affected_voices=[voice],
            )
            await session.commit()
            await session.refresh(voice)
            return voice
        except SQLAlchemyError as error:
            last_error = error
            await session.rollback()
            existing = await session.get(VoiceFile, voice_id)
            if existing is not None:
                return existing
            fields = {
                "attempt": attempt_number + 1,
                "max_attempts": DB_WRITE_ATTEMPTS,
                "error_type": type(error).__name__,
            }
            if attempt_number + 1 < DB_WRITE_ATTEMPTS:
                logger.warning(
                    LogEvent.DATABASE_WRITE_RETRY,
                    "Voice metadata write failed; retrying",
                    **fields,
                )
            else:
                logger.error(
                    LogEvent.DATABASE_WRITE_FAILED,
                    "Voice metadata write attempts exhausted",
                    **fields,
                )

    await _remove_uploaded_object(storage, bucket, object_key)
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Voice metadata could not be saved after three attempts",
    ) from last_error


@router.get("/meetings/{meeting_id}/voices", response_model=list[VoiceResponse])
async def list_meeting_voices(
    meeting_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    await require_meeting_permission(session, current_user, meeting_id, MeetingPermission.VIEW)
    return (
        await session.scalars(
            select(VoiceFile)
            .where(VoiceFile.meeting_id == meeting_id)
            .order_by(VoiceFile.sequence_number, VoiceFile.uploaded_at)
        )
    ).all()


@router.get("/voices", response_model=list[VoiceResponse])
async def list_all_voices(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Administrator access required")
    return (await session.scalars(select(VoiceFile).order_by(VoiceFile.uploaded_at.desc()))).all()


@router.get("/voices/{voice_id}", response_model=VoiceResponse)
async def get_voice(
    voice_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    voice = await session.get(VoiceFile, voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail="Voice file not found")
    bind_log_context(voice_id=str(voice.id))
    if voice.meeting_id is None:
        if not is_admin(current_user):
            raise HTTPException(status_code=403, detail="Insufficient permission")
    else:
        await require_meeting_permission(
            session, current_user, voice.meeting_id, MeetingPermission.VIEW
        )
    return voice


@router.delete("/voices/{voice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_voice(
    voice_id: uuid.UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
):
    voice = await session.get(VoiceFile, voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail="Voice file not found")
    bind_log_context(voice_id=str(voice.id))
    meeting: Meeting | None = None
    if voice.meeting_id is not None:
        meeting = await require_meeting_permission(
            session, current_user, voice.meeting_id, MeetingPermission.MANAGE_VOICES
        )
    elif not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Insufficient permission")

    try:
        await storage.remove_object(voice.minio_bucket, voice.minio_key)
    except Exception as error:
        logger.exception(
            LogEvent.OBJECT_STORAGE_DELETE_FAILED,
            "Voice deletion from object storage failed",
            error=error,
            bucket=voice.minio_bucket,
            object_key=voice.minio_key,
        )
        raise HTTPException(status_code=503, detail="Object storage deletion failed") from error

    add_history_event(
        session,
        event_type="voice.deleted",
        description="Voice file deleted",
        actor=current_user,
        request=request,
        event_data={
            "voice_id": str(voice.id),
            "filename": voice.original_filename,
            "minio_bucket": voice.minio_bucket,
            "minio_key": voice.minio_key,
        },
        affected_meetings=[meeting] if meeting else [],
    )
    await session.delete(voice)
    await session.commit()
