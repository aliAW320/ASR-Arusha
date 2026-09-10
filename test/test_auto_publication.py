"""Publication follows composition automatically, and one upload takes everything.

Two changes are covered here:

  * there is no approval step -- once a meeting transcript is composed the
    summarize + MCP job is queued straight away;
  * audio and supporting files go to the same endpoint in a single
    multi-file request, and the batch is all-or-nothing.
"""

import io
import uuid

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models import (
    MeetingAttachment,
    MeetingPublication,
    MeetingResult,
    MeetingPublicationStatus,
    ProcessingJob,
    ProcessingStage,
    VoiceFile,
)
from conftest import authorization, register_user
from meeting_composer.worker import MeetingComposerWorker


PNG = b"\x89PNG\r\n\x1a\nfake-image-body"
PDF = b"%PDF-1.4 fake-pdf-body"


async def _meeting(client, email: str):
    owner = await register_user(client, email)
    meeting = (
        await client.post("/meetings", headers=authorization(owner), json={"title": "Auto"})
    ).json()
    return owner, meeting


@pytest.mark.asyncio
async def test_one_request_stores_audio_and_supporting_files_together(client, session_factory):
    owner, meeting = await _meeting(client, "files-together@example.com")

    response = await client.post(
        f"/meetings/{meeting['id']}/files",
        headers=authorization(owner),
        files=[
            ("uploads", ("talk.wav", io.BytesIO(b"RIFF-audio"), "audio/wav")),
            ("uploads", ("slide.png", io.BytesIO(PNG), "image/png")),
            ("uploads", ("brief.pdf", io.BytesIO(PDF), "application/pdf")),
        ],
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert [voice["original_filename"] for voice in body["voices"]] == ["talk.wav"]
    assert sorted(item["original_filename"] for item in body["attachments"]) == [
        "brief.pdf",
        "slide.png",
    ]
    async with session_factory() as session:
        # Audio first enters canonical WAV preprocessing; supporting files just
        # wait for the summarizer.
        assert await session.scalar(
            select(func.count(ProcessingJob.id)).where(
                ProcessingJob.stage == ProcessingStage.PREPROCESS
            )
        ) == 1
        assert await session.scalar(select(func.count(MeetingAttachment.id))) == 2


@pytest.mark.asyncio
async def test_several_recordings_upload_at_once_and_keep_their_order(client, session_factory):
    owner, meeting = await _meeting(client, "files-multi-audio@example.com")

    response = await client.post(
        f"/meetings/{meeting['id']}/files",
        headers=authorization(owner),
        files=[
            ("uploads", ("part-1.wav", io.BytesIO(b"RIFF-one"), "audio/wav")),
            ("uploads", ("part-2.wav", io.BytesIO(b"RIFF-two"), "audio/wav")),
            ("uploads", ("part-3.wav", io.BytesIO(b"RIFF-three"), "audio/wav")),
        ],
    )

    assert response.status_code == 201, response.text
    voices = response.json()["voices"]
    assert [voice["original_filename"] for voice in voices] == [
        "part-1.wav",
        "part-2.wav",
        "part-3.wav",
    ]
    # Sequence numbers drive meeting composition order, so they must not collide.
    assert [voice["sequence_number"] for voice in voices] == [0, 1, 2]


@pytest.mark.asyncio
async def test_a_rejected_file_leaves_nothing_behind(client, session_factory):
    owner, meeting = await _meeting(client, "files-atomic@example.com")

    response = await client.post(
        f"/meetings/{meeting['id']}/files",
        headers=authorization(owner),
        files=[
            ("uploads", ("talk.wav", io.BytesIO(b"RIFF-audio"), "audio/wav")),
            ("uploads", ("notes.txt", io.BytesIO(b"plain text"), "text/plain")),
        ],
    )

    assert response.status_code == 415
    async with session_factory() as session:
        assert await session.scalar(select(func.count(VoiceFile.id))) == 0
        assert await session.scalar(select(func.count(MeetingAttachment.id))) == 0
    # The accepted half of the batch must not be left orphaned in storage.
    assert client.storage.objects == {}


@pytest.mark.asyncio
async def test_a_spoofed_attachment_is_refused(client):
    owner, meeting = await _meeting(client, "files-spoof@example.com")

    response = await client.post(
        f"/meetings/{meeting['id']}/files",
        headers=authorization(owner),
        files=[("uploads", ("fake.pdf", io.BytesIO(b"not a pdf"), "application/pdf"))],
    )

    assert response.status_code == 415


@pytest.mark.asyncio
async def test_viewers_cannot_upload(client):
    owner, meeting = await _meeting(client, "files-owner-perm@example.com")
    viewer = await register_user(client, "files-viewer@example.com")
    added = await client.post(
        f"/meetings/{meeting['id']}/members",
        headers=authorization(owner),
        json={"email": viewer["user"]["email"], "role": "viewer"},
    )
    assert added.status_code == 201

    response = await client.post(
        f"/meetings/{meeting['id']}/files",
        headers=authorization(viewer),
        files=[("uploads", ("talk.wav", io.BytesIO(b"RIFF-audio"), "audio/wav"))],
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_composition_queues_publication_without_anyone_approving(
    client, session_factory
):
    from test_meeting_composer import _meeting_with_finished_voices

    owner, meeting, _voices = await _meeting_with_finished_voices(
        client, session_factory, "auto-publish@example.com"
    )
    attached = await client.post(
        f"/meetings/{meeting['id']}/files",
        headers=authorization(owner),
        files=[("uploads", ("slide.png", io.BytesIO(PNG), "image/png"))],
    )
    assert attached.status_code == 201
    composer = MeetingComposerWorker(
        session_factory=session_factory, storage=client.storage, settings=get_settings()
    )

    assert await composer.run_once() is True

    async with session_factory() as session:
        publication = await session.scalar(select(MeetingPublication))
        job = await session.get(ProcessingJob, publication.current_job_id)
        assert publication.status == MeetingPublicationStatus.QUEUED
        assert job.stage == ProcessingStage.MINUTES_GENERATION
        # Attachments present at composition time are snapshotted for the run.
        assert len(publication.attachment_ids) == 1
        assert publication.destination_path == get_settings().meeting_publication_default_path


@pytest.mark.asyncio
async def test_the_approval_endpoint_is_gone(client, session_factory):
    owner, meeting = await _meeting(client, "no-approval@example.com")

    response = await client.post(
        f"/meetings/{meeting['id']}/publication/approve",
        headers=authorization(owner),
        json={"destination_path": "پروژه‌های کارآموزی/ASR test"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_destination_can_still_be_chosen_before_publishing(client, session_factory):
    owner, meeting = await _meeting(client, "destination@example.com")

    response = await client.patch(
        f"/meetings/{meeting['id']}/publication",
        headers=authorization(owner),
        json={"destination_path": "/ پروژه‌های کارآموزی / ASR test /"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["destination_path"] == "پروژه‌های کارآموزی/ASR test"
    assert response.json()["status"] == "pending"
    read_back = await client.get(
        f"/meetings/{meeting['id']}/publication", headers=authorization(owner)
    )
    assert read_back.json()["destination_path"] == "پروژه‌های کارآموزی/ASR test"


@pytest.mark.asyncio
async def test_recomposing_republishes_into_the_same_documents(client, session_factory):
    from test_meeting_composer import _meeting_with_finished_voices

    owner, meeting, _voices = await _meeting_with_finished_voices(
        client, session_factory, "auto-republish@example.com"
    )
    composer = MeetingComposerWorker(
        session_factory=session_factory, storage=client.storage, settings=get_settings()
    )
    assert await composer.run_once() is True
    async with session_factory() as session:
        first_result_id = await session.scalar(select(MeetingResult.id))
        publication = await session.scalar(select(MeetingPublication))
        publication.status = MeetingPublicationStatus.PUBLISHED
        publication.outline_parent_document_id = "doc-parent"
        publication.outline_summary_document_id = "doc-summary"
        publication.outline_transcript_document_id = "doc-transcript"
        first_job_id = publication.current_job_id
        await session.commit()

    recompose = await client.post(
        f"/meetings/{meeting['id']}/compose",
        headers=authorization(owner),
        json={"force_new_version": True},
    )
    assert recompose.status_code == 202, recompose.text
    assert await composer.run_once() is True

    async with session_factory() as session:
        result_ids = (await session.scalars(select(MeetingResult.id))).all()
        assert len(result_ids) == 2
        publication = await session.scalar(select(MeetingPublication))
        assert publication.status == MeetingPublicationStatus.QUEUED
        assert publication.current_job_id != first_job_id
        # The publication now points at the new version, not the stale one.
        second_result_id = next(rid for rid in result_ids if rid != first_result_id)
        assert publication.meeting_result_id == second_result_id
        # Reusing the stored document ids is what keeps a re-publish an update
        # instead of a duplicate document tree in the knowledge base.
        assert publication.outline_parent_document_id == "doc-parent"
        assert publication.outline_summary_document_id == "doc-summary"
        assert publication.outline_transcript_document_id == "doc-transcript"
