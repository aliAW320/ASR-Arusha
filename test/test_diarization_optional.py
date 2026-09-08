"""Diarization is optional per voice.

Upload never starts diarization on its own any more. The API reports whether a
diarization worker is actually alive (heartbeat), and:

  * no worker            -> the voice is marked `unavailable` and goes
                            ASR -> cleaning, with no diarization/alignment;
  * worker alive         -> the voice waits on `pending` until someone answers
                            yes/no, so ASR output is never cleaned against a
                            speaker transcript the user did not ask for.
"""

import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models import (
    DiarizationDecision,
    ProcessingJob,
    ProcessingStage,
    Result,
    ResultArtifact,
    ResultArtifactType,
    ServiceHeartbeat,
    VoiceFile,
)
from app.services.diarization import DIARIZATION_SERVICE, record_heartbeat
from asr.provider import TranscriptionResponse
from asr.worker import ASRWorker
from cleaner.provider import CleanerResponse
from cleaner.worker import CleanerWorker
from conftest import authorization, register_user


class _Transcription:
    async def transcribe(self, audio, **kwargs):
        return TranscriptionResponse(
            text="سلام دنیا",
            language="fa",
            segments=[{"id": 0, "start": 0, "end": 1, "text": "سلام دنیا"}],
            raw_response={"text": "سلام دنیا"},
            words=[
                {"word": "سلام", "start": 0.05, "end": 0.4},
                {"word": "دنیا", "start": 0.6, "end": 0.95},
            ],
            external_request_id="asr-1",
        )


class _Cleaning:
    async def clean(self, segments, **kwargs):
        return CleanerResponse(
            segments=[{**segment, "text": f"{segment['text']} اصلاح‌شده"} for segment in segments],
            external_request_id="cleaner-1",
        )

    async def aclose(self):
        return None


async def _heartbeat(session_factory, *, age_seconds: float = 0.0):
    async with session_factory() as session:
        await record_heartbeat(session, DIARIZATION_SERVICE)
        if age_seconds:
            heartbeat = await session.get(ServiceHeartbeat, DIARIZATION_SERVICE)
            heartbeat.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        await session.commit()


async def _upload(client, email: str):
    owner = await register_user(client, email)
    meeting = (
        await client.post("/meetings", headers=authorization(owner), json={"title": "Diarization"})
    ).json()
    uploaded = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(owner),
        files={"upload": ("sample.wav", io.BytesIO(b"RIFF-audio"), "audio/wav")},
    )
    assert uploaded.status_code == 201, uploaded.text
    return owner, meeting, uploaded.json()


def _stages(jobs):
    return {job["stage"] for job in jobs}


@pytest.mark.asyncio
async def test_upload_waits_for_a_choice_while_a_diarization_worker_is_alive(
    client, session_factory
):
    await _heartbeat(session_factory)
    owner, meeting, voice = await _upload(client, "diar-pending@example.com")

    assert voice["diarization_decision"] == "pending"
    processing = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )
    # Transcription starts immediately -- it never depended on diarization --
    # but diarization itself must not be queued before the user answers.
    assert _stages(processing.json()) == {"transcription"}


@pytest.mark.asyncio
async def test_upload_skips_diarization_entirely_when_no_worker_is_alive(
    client, session_factory
):
    owner, meeting, voice = await _upload(client, "diar-unavailable@example.com")

    assert voice["diarization_decision"] == "unavailable"
    processing = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )
    assert _stages(processing.json()) == {"transcription"}


@pytest.mark.asyncio
async def test_availability_endpoint_follows_heartbeat_freshness(client, session_factory):
    owner = await register_user(client, "diar-availability@example.com")

    missing = await client.get("/processing/diarization", headers=authorization(owner))
    await _heartbeat(session_factory)
    fresh = await client.get("/processing/diarization", headers=authorization(owner))
    await _heartbeat(
        session_factory,
        age_seconds=get_settings().diarization_heartbeat_ttl_seconds + 30,
    )
    stale = await client.get("/processing/diarization", headers=authorization(owner))

    assert missing.json()["available"] is False
    assert fresh.json()["available"] is True
    assert stale.json()["available"] is False


@pytest.mark.asyncio
async def test_accepting_diarization_queues_the_diarization_job(client, session_factory):
    await _heartbeat(session_factory)
    owner, meeting, voice = await _upload(client, "diar-accept@example.com")

    response = await client.post(
        f"/voices/{voice['id']}/diarization",
        headers=authorization(owner),
        json={"enabled": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["diarization_decision"] == "enabled"
    processing = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )
    assert _stages(processing.json()) == {"transcription", "diarization"}


@pytest.mark.asyncio
async def test_accepting_diarization_is_refused_while_no_worker_is_alive(
    client, session_factory
):
    owner, _meeting, voice = await _upload(client, "diar-accept-offline@example.com")

    response = await client.post(
        f"/voices/{voice['id']}/diarization",
        headers=authorization(owner),
        json={"enabled": True},
    )

    assert response.status_code == 409
    async with session_factory() as session:
        stored = await session.get(VoiceFile, uuid.UUID(voice["id"]))
        assert stored.diarization_decision == DiarizationDecision.UNAVAILABLE


@pytest.mark.asyncio
async def test_a_recorded_choice_cannot_be_flipped_afterwards(client, session_factory):
    await _heartbeat(session_factory)
    owner, _meeting, voice = await _upload(client, "diar-locked@example.com")
    headers = authorization(owner)

    first = await client.post(
        f"/voices/{voice['id']}/diarization", headers=headers, json={"enabled": False}
    )
    second = await client.post(
        f"/voices/{voice['id']}/diarization", headers=headers, json={"enabled": True}
    )

    assert first.status_code == 200
    assert first.json()["diarization_decision"] == "skipped"
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_viewers_cannot_choose_for_a_meeting_they_only_read(client, session_factory):
    await _heartbeat(session_factory)
    owner, meeting, voice = await _upload(client, "diar-owner-perm@example.com")
    viewer = await register_user(client, "diar-viewer@example.com")
    added = await client.post(
        f"/meetings/{meeting['id']}/members",
        headers=authorization(owner),
        json={"email": viewer["user"]["email"], "role": "viewer"},
    )
    assert added.status_code == 201

    response = await client.post(
        f"/voices/{voice['id']}/diarization",
        headers=authorization(viewer),
        json={"enabled": True},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_transcription_success_holds_the_pipeline_until_the_choice_is_made(
    client, session_factory
):
    await _heartbeat(session_factory)
    owner, meeting, _voice = await _upload(client, "diar-hold@example.com")
    worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _Transcription(),
    )

    assert await worker.run_once() is True

    processing = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )
    # No alignment, and above all no cleaning: cleaning the ASR text now would
    # throw away the speaker transcript the user may still ask for.
    assert _stages(processing.json()) == {"transcription"}


@pytest.mark.asyncio
async def test_declining_after_transcription_releases_cleaning_immediately(
    client, session_factory
):
    await _heartbeat(session_factory)
    owner, meeting, voice = await _upload(client, "diar-decline-late@example.com")
    worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _Transcription(),
    )
    assert await worker.run_once() is True

    declined = await client.post(
        f"/voices/{voice['id']}/diarization",
        headers=authorization(owner),
        json={"enabled": False},
    )

    assert declined.status_code == 200
    processing = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )
    assert _stages(processing.json()) == {"transcription", "cleaning"}


@pytest.mark.asyncio
async def test_pipeline_without_diarization_still_produces_a_cleaned_transcript(
    client, session_factory
):
    owner, meeting, voice = await _upload(client, "diar-none-e2e@example.com")
    asr_worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _Transcription(),
    )
    cleaner_worker = CleanerWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _Cleaning(),
    )

    assert await asr_worker.run_once() is True
    assert await cleaner_worker.run_once() is True

    results = await client.get(
        f"/voices/{voice['id']}/results", headers=authorization(owner)
    )
    result = results.json()[0]
    transcript = await client.get(
        f"/results/{result['id']}/transcript", headers=authorization(owner)
    )
    assert transcript.status_code == 200, transcript.text
    assert transcript.json()["text"].strip() != ""
    assert transcript.json()["segments"][0]["text"].endswith("اصلاح‌شده")
    async with session_factory() as session:
        stages = set(await session.scalars(select(ProcessingJob.stage)))
        artifacts = set(await session.scalars(select(ResultArtifact.artifact_type)))
        assert ProcessingStage.DIARIZATION not in stages
        assert ProcessingStage.ALIGNMENT not in stages
        assert ResultArtifactType.DIARIZATION_JSON not in artifacts
        # Cleaning still consumes the same speaker-transcript slot, so the rest
        # of the pipeline (cleaner, composer, publication) needs no special case.
        assert ResultArtifactType.ALIGNED_TRANSCRIPT_JSON in artifacts
        assert ResultArtifactType.CLEANED_TEXT in artifacts


@pytest.mark.asyncio
async def test_reprocessing_keeps_an_explicit_choice_and_requeues_diarization(
    client, session_factory
):
    await _heartbeat(session_factory)
    owner, meeting, voice = await _upload(client, "diar-reprocess@example.com")
    accepted = await client.post(
        f"/voices/{voice['id']}/diarization",
        headers=authorization(owner),
        json={"enabled": True},
    )
    assert accepted.status_code == 200
    # Reprocessing is refused while the first transcription is still queued.
    worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _Transcription(),
    )
    assert await worker.run_once() is True

    reprocess = await client.post(
        f"/meetings/{meeting['id']}/process", headers=authorization(owner)
    )

    assert reprocess.status_code == 202, reprocess.text
    async with session_factory() as session:
        # The second run must not ask again: the user already answered.
        assert await session.scalar(
            select(func.count(ProcessingJob.id)).where(
                ProcessingJob.stage == ProcessingStage.DIARIZATION
            )
        ) == 2
        assert await session.scalar(select(func.count(Result.id))) == 2
