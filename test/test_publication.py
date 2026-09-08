import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import (
    BrokerOutboxMessage,
    MeetingArtifactType,
    MeetingPublication,
    MeetingPublicationStatus,
    MeetingResult,
    MeetingResultArtifact,
    ProcessingAttempt,
    ProcessingAttemptStatus,
    ProcessingJob,
    ProcessingStage,
)
from conftest import authorization, register_user


async def _meeting(client, email="owner-publication@example.com"):
    owner = await register_user(client, email)
    response = await client.post(
        "/meetings",
        json={"title": "جلسه آزمون انتشار"},
        headers=authorization(owner),
    )
    assert response.status_code == 201
    return owner, response.json()


async def _completed_result(session_factory, storage, meeting_id):
    payload = {
        "schema": "meeting-transcript/v1",
        "meeting_id": meeting_id,
        "meeting_result_id": "pending",
        "generated_at": "2026-09-08T00:00:00Z",
        "duration_ms": 1000,
        "source_count": 0,
        "speaker_count": 1,
        "text": "سلام",
        "sources": [],
        "speakers": [],
        "segments": [
            {
                "start_ms": 0,
                "end_ms": 1000,
                "speaker_label": "SPEAKER_00",
                "text": "سلام",
            }
        ],
    }
    async with session_factory() as session:
        result = MeetingResult(
            meeting_id=uuid.UUID(meeting_id),
            source_fingerprint="f" * 64,
            completed_at=datetime.now(timezone.utc),
        )
        session.add(result)
        await session.flush()
        payload["meeting_result_id"] = str(result.id)
        content = json.dumps(payload, ensure_ascii=False).encode()
        artifact = MeetingResultArtifact(
            meeting_result=result,
            artifact_type=MeetingArtifactType.COMBINED_TRANSCRIPT_JSON,
            minio_bucket="test-exports",
            minio_key=f"meeting-results/{result.id}/transcript.json",
            content_type="application/json",
        )
        session.add(artifact)
        await session.commit()
        result_id = result.id
    storage.objects[("test-exports", f"meeting-results/{result_id}/transcript.json")] = content
    return result_id


@pytest.mark.asyncio
async def test_attachment_upload_accepts_png_and_rejects_spoofed_files(
    client, session_factory
):
    owner, meeting = await _meeting(client)
    headers = authorization(owner)

    accepted = await client.post(
        f"/meetings/{meeting['id']}/attachments",
        headers=headers,
        files={"upload": ("diagram.png", b"\x89PNG\r\n\x1a\nvalid", "image/png")},
    )
    assert accepted.status_code == 201
    assert accepted.json()["content_type"] == "image/png"
    assert accepted.json()["original_filename"] == "diagram.png"

    spoofed = await client.post(
        f"/meetings/{meeting['id']}/attachments",
        headers=headers,
        files={"upload": ("fake.pdf", b"not a pdf", "application/pdf")},
    )
    assert spoofed.status_code == 415

    listed = await client.get(
        f"/meetings/{meeting['id']}/attachments", headers=headers
    )
    assert [item["id"] for item in listed.json()] == [accepted.json()["id"]]


@pytest.mark.asyncio
async def test_viewer_cannot_upload_delete_or_approve_publication(client, session_factory):
    owner, meeting = await _meeting(client, "owner-permissions@example.com")
    viewer = await register_user(client, "viewer-publication@example.com")
    added = await client.post(
        f"/meetings/{meeting['id']}/members",
        headers=authorization(owner),
        json={"email": viewer["user"]["email"], "role": "viewer"},
    )
    assert added.status_code == 201
    await _completed_result(session_factory, client.storage, meeting["id"])

    upload = await client.post(
        f"/meetings/{meeting['id']}/attachments",
        headers=authorization(viewer),
        files={"upload": ("image.png", b"\x89PNG\r\n\x1a\nvalid", "image/png")},
    )
    approval = await client.post(
        f"/meetings/{meeting['id']}/publication/approve",
        headers=authorization(viewer),
        json={"destination_path": "پروژه‌های کارآموزی/ASR test"},
    )
    assert upload.status_code == 403
    assert approval.status_code == 403


@pytest.mark.asyncio
async def test_approval_snapshots_attachments_and_enqueues_only_the_mcp_stage(
    client, session_factory
):
    owner, meeting = await _meeting(client, "owner-approval@example.com")
    headers = authorization(owner)
    result_id = await _completed_result(
        session_factory, client.storage, meeting["id"]
    )
    attachment = await client.post(
        f"/meetings/{meeting['id']}/attachments",
        headers=headers,
        files={"upload": ("context.jpg", b"\xff\xd8\xffcontent", "image/jpeg")},
    )
    assert attachment.status_code == 201

    approval = await client.post(
        f"/meetings/{meeting['id']}/publication/approve",
        headers=headers,
        json={"destination_path": "/ پروژه‌های کارآموزی / ASR test /"},
    )
    assert approval.status_code == 202, approval.text
    body = approval.json()
    assert body["status"] == "queued"
    assert body["meeting_result_id"] == str(result_id)
    assert body["destination_path"] == "پروژه‌های کارآموزی/ASR test"
    assert body["attachment_ids"] == [attachment.json()["id"]]

    async with session_factory() as session:
        publication = await session.scalar(select(MeetingPublication))
        job = await session.get(ProcessingJob, publication.current_job_id)
        attempt = await session.scalar(
            select(ProcessingAttempt).where(ProcessingAttempt.job_id == job.id)
        )
        outbox = await session.scalar(
            select(BrokerOutboxMessage).where(
                BrokerOutboxMessage.deduplication_key == f"attempt:{attempt.id}"
            )
        )
        assert publication.status == MeetingPublicationStatus.QUEUED
        assert job.stage == ProcessingStage.MINUTES_GENERATION
        assert attempt.status == ProcessingAttemptStatus.QUEUED
        assert outbox.queue_name == "mcp.queue"

    blocked = await client.delete(
        f"/meetings/{meeting['id']}/attachments/{attachment.json()['id']}",
        headers=headers,
    )
    assert blocked.status_code == 409


@pytest.mark.asyncio
async def test_approval_requires_a_completed_meeting_transcript(client):
    owner, meeting = await _meeting(client, "owner-no-result@example.com")
    response = await client.post(
        f"/meetings/{meeting['id']}/publication/approve",
        headers=authorization(owner),
        json={"destination_path": "پروژه‌های کارآموزی/ASR test"},
    )
    assert response.status_code == 409
