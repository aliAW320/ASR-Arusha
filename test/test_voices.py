import io
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_db_session
from app.main import app
from app.models import History, VoiceFile
from conftest import authorization, register_user


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
    assert voice.minio_key == response.json()["minio_key"]
    assert client.storage.objects[(voice.minio_bucket, voice.minio_key)] == content
    assert await session.scalar(
        select(History.id).where(History.event_type == "voice.uploaded")
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
