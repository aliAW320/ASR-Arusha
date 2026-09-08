"""Shared storage rules for everything a meeting accepts as an upload.

Voices and attachments arrive through the same door now -- one multi-file
request per meeting -- so the per-file rules (what a type means, how it is
inspected, where it lands in object storage) live here instead of being
restated by each route.
"""

import hashlib
import uuid
from pathlib import Path

from anyio import to_thread
from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import Meeting, MeetingAttachment, User, VoiceFile
from ..storage.base import ObjectStorage


# Attachment types the summarizer can actually use, each pinned to the magic
# bytes that prove the content matches its declared type.
ATTACHMENT_TYPES = {
    "image/png": ((".png",), b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": ((".jpg", ".jpeg"), b"\xff\xd8\xff"),
    "application/pdf": ((".pdf",), b"%PDF-"),
}


class UploadRejected(Exception):
    """A single file cannot be accepted; carries the HTTP status to use."""

    def __init__(self, message: str, *, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def safe_filename(filename: str | None, fallback: str = "upload.bin") -> str:
    name = Path(filename or fallback).name
    return "".join(
        character if character.isalnum() or character in ".-_" else "_"
        for character in name
    )


def normalized_content_type(upload: UploadFile) -> str:
    content_type = (upload.content_type or "").lower()
    # Browsers and curl disagree on the JPEG type; normalize before matching.
    return "image/jpeg" if content_type == "image/jpg" else content_type


def is_audio(upload: UploadFile) -> bool:
    return normalized_content_type(upload).startswith("audio/")


def is_attachment(upload: UploadFile) -> bool:
    return normalized_content_type(upload) in ATTACHMENT_TYPES


async def inspect_upload(upload: UploadFile, max_bytes: int) -> tuple[int, str, bytes]:
    """Stream the file once for size, checksum and leading magic bytes."""

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
                raise ValueError("Upload exceeds the configured limit")
            digest.update(chunk)
        upload.file.seek(0)
        return size, digest.hexdigest(), prefix

    return await to_thread.run_sync(inspect)


def validate_attachment(upload: UploadFile, prefix: bytes) -> str:
    content_type = normalized_content_type(upload)
    contract = ATTACHMENT_TYPES.get(content_type)
    if contract is None:
        raise UploadRejected(
            "Only PNG, JPEG, and PDF attachments are accepted", status_code=415
        )
    suffixes, signature = contract
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in suffixes or not prefix.startswith(signature):
        raise UploadRejected(
            "Attachment content does not match its file type", status_code=415
        )
    return content_type


async def store_attachment(
    session: AsyncSession,
    storage: ObjectStorage,
    *,
    meeting: Meeting,
    actor: User,
    upload: UploadFile,
    settings: Settings,
) -> tuple[MeetingAttachment, tuple[str, str]]:
    """Persist one attachment. Returns the row and its object-storage key,
    so a caller batching several files can clean up what it already wrote.
    """
    try:
        size_bytes, checksum, prefix = await inspect_upload(
            upload, settings.meeting_attachment_max_bytes
        )
    except ValueError as error:
        raise UploadRejected(str(error), status_code=413) from error
    if size_bytes == 0:
        raise UploadRejected("Attachment cannot be empty", status_code=422)
    content_type = validate_attachment(upload, prefix)

    attachment_id = uuid.uuid4()
    bucket = settings.minio_meetings_bucket
    filename = safe_filename(upload.filename, "attachment.bin")
    object_key = f"meetings/{meeting.id}/attachments/{attachment_id}/source/{filename}"
    await storage.put_object(bucket, object_key, upload.file, size_bytes, content_type)

    attachment = MeetingAttachment(
        id=attachment_id,
        meeting=meeting,
        uploaded_by=actor,
        minio_bucket=bucket,
        minio_key=object_key,
        original_filename=upload.filename or filename,
        content_type=content_type,
        size_bytes=size_bytes,
        checksum_sha256=checksum,
    )
    session.add(attachment)
    return attachment, (bucket, object_key)


async def store_voice(
    session: AsyncSession,
    storage: ObjectStorage,
    *,
    meeting: Meeting,
    actor: User,
    upload: UploadFile,
    settings: Settings,
    sequence_number: int | None = None,
) -> tuple[VoiceFile, tuple[str, str]]:
    """Persist one voice file. Does not queue processing -- the caller does
    that, so a batch can queue every voice after all files are accepted.
    """
    if not is_audio(upload):
        raise UploadRejected("Only audio uploads are accepted", status_code=415)
    try:
        size_bytes, checksum, _prefix = await inspect_upload(
            upload, settings.voice_upload_max_bytes
        )
    except ValueError as error:
        raise UploadRejected(str(error), status_code=413) from error
    if size_bytes == 0:
        raise UploadRejected("Voice file cannot be empty", status_code=422)

    if sequence_number is None:
        maximum = await session.scalar(
            select(func.max(VoiceFile.sequence_number)).where(
                VoiceFile.meeting_id == meeting.id
            )
        )
        sequence_number = (maximum if maximum is not None else -1) + 1

    voice_id = uuid.uuid4()
    bucket = settings.minio_meetings_bucket
    filename = safe_filename(upload.filename, "voice.bin")
    object_key = f"meetings/{meeting.id}/voices/{voice_id}/source/{filename}"
    await storage.put_object(
        bucket, object_key, upload.file, size_bytes, normalized_content_type(upload)
    )

    voice = VoiceFile(
        id=voice_id,
        minio_bucket=bucket,
        minio_key=object_key,
        original_filename=upload.filename,
        content_type=normalized_content_type(upload),
        size_bytes=size_bytes,
        checksum_sha256=checksum,
        sequence_number=sequence_number,
        meeting_id=meeting.id,
        uploaded_by_id=actor.id,
    )
    session.add(voice)
    return voice, (bucket, object_key)
