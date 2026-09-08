import asyncio
import io
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

import app.services.cancellation as cancellation_service
from app.config import get_settings
from app.database import get_db_session
from app.main import app
from app.models import (
    BrokerOutboxMessage,
    History,
    ProcessingAttempt,
    ProcessingJob,
    Result,
    ResultArtifact,
    VoiceFile,
    VoiceStatus,
)
from app.services.cancellation import attempt_is_cancelled
from asr.worker import ASRWorker
from conftest import accept_diarization, authorization, mark_diarization_available, register_user


@pytest.mark.asyncio
async def test_audio_upload_is_stored_in_minio_and_postgres(client, session):
    owner = await register_user(client, "voice-owner@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Audio"}
        )
    ).json()
    content = b"RIFF-test-audio"
    response = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(owner),
        files={"upload": ("meeting.wav", io.BytesIO(content), "audio/wav")},
    )

    assert response.status_code == 201, response.text
    voice = await session.get(VoiceFile, uuid.UUID(response.json()["id"]))
    assert voice.status == VoiceStatus.PENDING
    assert voice.minio_key == response.json()["minio_key"]
    assert client.storage.objects[(voice.minio_bucket, voice.minio_key)] == content
    assert await session.scalar(select(ProcessingJob.id)) is not None
    assert await session.scalar(select(ProcessingAttempt.id)) is not None
    outbox = (
        await session.scalars(
            select(BrokerOutboxMessage).order_by(BrokerOutboxMessage.queue_name)
        )
    ).all()
    # Diarization is opt-in per voice now, so a bare upload only queues ASR;
    # the diar.queue message appears once someone answers yes.
    assert [message.queue_name for message in outbox] == ["asr.queue"]
    assert {message.payload["stage"] for message in outbox} == {"transcription"}
    assert all(message.deduplication_key.startswith("attempt:") for message in outbox)
    assert await session.scalar(
        select(History.id).where(History.event_type == "voice.uploaded")
    ) is not None
    assert await session.scalar(
        select(History.id).where(History.event_type == "processing.queued")
    ) is not None


@pytest.mark.asyncio
async def test_non_audio_upload_is_rejected(client):
    owner = await register_user(client, "voice-type@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Audio"}
        )
    ).json()
    response = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(owner),
        files={"upload": ("notes.txt", b"not audio", "text/plain")},
    )
    assert response.status_code == 415
    assert client.storage.objects == {}


@pytest.mark.asyncio
async def test_authorized_contributor_upload_also_enters_processing_queue(client, session):
    owner = await register_user(client, "queue-owner@example.com")
    contributor = await register_user(client, "queue-contributor@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Queued audio"}
        )
    ).json()
    added = await client.post(
        f"/meetings/{meeting['id']}/members",
        headers=authorization(owner),
        json={"email": contributor["user"]["email"], "role": "contributor"},
    )
    assert added.status_code == 201, added.text

    response = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(contributor),
        files={"upload": ("contributor.wav", b"audio", "audio/wav")},
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "pending"
    assert await session.scalar(select(ProcessingJob.id)) is not None
    assert await session.scalar(select(ProcessingAttempt.id)) is not None


@pytest.mark.asyncio
async def test_empty_audio_upload_is_rejected(client):
    owner = await register_user(client, "empty-voice@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Empty"}
        )
    ).json()
    response = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(owner),
        files={"upload": ("empty.wav", b"", "audio/wav")},
    )
    assert response.status_code == 422
    assert client.storage.objects == {}


@pytest.mark.asyncio
async def test_failed_metadata_write_retries_three_times_and_cleans_minio(
    client, session_factory
):
    owner = await register_user(client, "voice-retry@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Retry"}
        )
    ).json()

    class CommitFailingSession:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.commit_attempts = 0

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        async def commit(self):
            self.commit_attempts += 1
            raise SQLAlchemyError("forced commit failure")

    async with session_factory() as database_session:
        failing = CommitFailingSession(database_session)

        async def override_session():
            yield failing

        app.dependency_overrides[get_db_session] = override_session
        response = await client.post(
            f"/meetings/{meeting['id']}/voices",
            headers=authorization(owner),
            files={"upload": ("retry.wav", b"audio", "audio/wav")},
        )

    assert response.status_code == 503
    assert failing.commit_attempts == 3
    assert len(client.storage.removed) == 1
    assert client.storage.objects == {}


@pytest.mark.asyncio
async def test_deleting_voice_cancels_queued_work_and_removes_outbox(
    client, session_factory
):
    await mark_diarization_available(session_factory)
    owner = await register_user(client, "delete-queued@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Delete queued"}
        )
    ).json()
    uploaded = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(owner),
        files={"upload": ("queued.wav", b"RIFF-audio", "audio/wav")},
    )
    await accept_diarization(client, owner, uploaded.json()["id"])
    voice_id = uuid.UUID(uploaded.json()["id"])
    async with session_factory() as session:
        attempt_id = await session.scalar(select(ProcessingAttempt.id))

    removed = await client.delete(
        f"/voices/{voice_id}", headers=authorization(owner)
    )

    assert removed.status_code == 204, removed.text
    async with session_factory() as session:
        assert await session.get(VoiceFile, voice_id) is None
        assert await session.scalar(select(ProcessingJob.id)) is None
        assert await session.scalar(select(ProcessingAttempt.id)) is None
        assert await session.scalar(select(BrokerOutboxMessage.id)) is None
        event = await session.scalar(
            select(History).where(History.event_type == "voice.deleted")
        )
        assert event.event_data["cancelled_processing_attempts"] == 2
        cancellation_event = await session.scalar(
            select(History).where(History.event_type == "processing.cancelled")
        )
        assert cancellation_event.event_data == {
            "voice_id": str(voice_id),
            "reason": "voice_deleted",
            "cancelled_attempts": 2,
        }
    assert await attempt_is_cancelled(session_factory, attempt_id) is True


class BlockingTranscriptionProvider:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def transcribe(self, *_args, **_kwargs):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.mark.asyncio
async def test_deleting_voice_cancels_active_asr_without_persisting_output(
    client, session_factory, monkeypatch
):
    owner = await register_user(client, "delete-running@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Delete running"}
        )
    ).json()
    uploaded = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(owner),
        files={"upload": ("running.wav", b"RIFF-audio", "audio/wav")},
    )
    voice_id = uuid.UUID(uploaded.json()["id"])
    provider = BlockingTranscriptionProvider()
    deletion_committed = asyncio.Event()

    async def cancelled_after_delete(_session_factory, _attempt_id):
        return deletion_committed.is_set()

    monkeypatch.setattr(
        cancellation_service, "attempt_is_cancelled", cancelled_after_delete
    )
    settings = get_settings().model_copy(
        update={"processing_cancellation_poll_interval_seconds": 0.01}
    )
    worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=settings,
        provider_factory=lambda _: provider,
    )
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(provider.started.wait(), timeout=1)

    removed = await client.delete(
        f"/voices/{voice_id}", headers=authorization(owner)
    )
    assert removed.status_code == 204, removed.text
    deletion_committed.set()
    assert await asyncio.wait_for(running, timeout=2) is True
    assert provider.cancelled.is_set()

    async with session_factory() as session:
        assert await session.get(VoiceFile, voice_id) is None
        assert await session.scalar(select(Result.id)) is None
        assert await session.scalar(select(ResultArtifact.id)) is None
        assert await session.scalar(select(BrokerOutboxMessage.id)) is None
