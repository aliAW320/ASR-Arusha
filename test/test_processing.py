import io
import json
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import (
    ExternalIntegration,
    DiarizationSpeaker,
    History,
    ModelDefinition,
    ModelRuntime,
    ProcessingAttempt,
    ProcessingAttemptStatus,
    ProcessingJob,
    ProcessingStage,
    Result,
    ResultArtifact,
    VoiceFile,
    VoiceStatus,
)
from asr.provider import TranscriptionError, TranscriptionResponse
from asr.worker import ASRWorker
from diarization.provider import DiarizationTurn
from diarization.worker import DiarizationWorker
from conftest import authorization, register_user


async def _meeting_with_voice(client, email: str):
    owner = await register_user(client, email)
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Processing"}
        )
    ).json()
    uploaded = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(owner),
        files={"upload": ("sample.wav", io.BytesIO(b"RIFF-audio"), "audio/wav")},
    )
    assert uploaded.status_code == 201, uploaded.text
    return owner, meeting, uploaded.json()


@pytest.mark.asyncio
async def test_upload_queues_persistent_job_attempt_and_registry(client, session):
    owner, meeting, voice = await _meeting_with_voice(client, "queue-owner@example.com")
    response = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )

    assert response.status_code == 200, response.text
    job = response.json()[0]
    assert job["voice_id"] == voice["id"]
    assert job["stage"] == "transcription"
    assert job["status"] == "queued"
    assert job["attempts"][0]["attempt_number"] == 1
    assert await session.scalar(select(func.count(Result.id))) == 1
    assert await session.scalar(select(func.count(ProcessingJob.id))) == 1
    assert await session.scalar(select(func.count(ProcessingAttempt.id))) == 1
    assert await session.scalar(select(func.count(ModelRuntime.id))) == 1
    assert await session.scalar(select(func.count(ModelDefinition.id))) == 1
    assert await session.scalar(select(func.count(ExternalIntegration.id))) == 1
    stored_voice = await session.get(VoiceFile, uuid.UUID(voice["id"]))
    assert stored_voice.status == VoiceStatus.PENDING
    assert await session.scalar(
        select(History.id).where(History.event_type == "processing.queued")
    ) is not None


@pytest.mark.asyncio
async def test_processing_access_and_reprocessing_keep_results_isolated(
    client, session_factory
):
    owner, meeting, _ = await _meeting_with_voice(client, "reprocess-owner@example.com")
    outsider = await register_user(client, "reprocess-outsider@example.com")

    first = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )
    duplicate = await client.post(
        f"/meetings/{meeting['id']}/process", headers=authorization(owner)
    )
    job_id = first.json()[0]["id"]
    denied_start = await client.post(
        f"/meetings/{meeting['id']}/process", headers=authorization(outsider)
    )
    denied_status = await client.get(
        f"/processing/jobs/{job_id}", headers=authorization(outsider)
    )
    assert first.status_code == 200
    assert duplicate.status_code == 409
    async with session_factory() as session:
        attempt = await session.scalar(select(ProcessingAttempt))
        voice = await session.scalar(select(VoiceFile))
        attempt.status = ProcessingAttemptStatus.SUCCEEDED
        voice.status = VoiceStatus.FINISHED
        await session.commit()
    second = await client.post(
        f"/meetings/{meeting['id']}/process", headers=authorization(owner)
    )
    owner_status = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )
    owner_result = await client.get(
        f"/results/{second.json()[0]['result_id']}", headers=authorization(owner)
    )
    denied_result = await client.get(
        f"/results/{second.json()[0]['result_id']}", headers=authorization(outsider)
    )
    assert second.status_code == 202
    assert first.json()[0]["result_id"] != second.json()[0]["result_id"]
    assert denied_start.status_code == 403
    assert denied_status.status_code == 403
    assert owner_result.status_code == 200
    assert denied_result.status_code == 403
    assert len(owner_status.json()) == 2
    async with session_factory() as session:
        assert await session.scalar(select(func.count(ModelRuntime.id))) == 1
        assert await session.scalar(select(func.count(ModelDefinition.id))) == 1
        assert await session.scalar(select(func.count(ExternalIntegration.id))) == 1


@pytest.mark.asyncio
async def test_processing_empty_meeting_is_rejected_without_creating_jobs(client):
    owner = await register_user(client, "empty-processing@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Empty"}
        )
    ).json()

    response = await client.post(
        f"/meetings/{meeting['id']}/process", headers=authorization(owner)
    )

    assert response.status_code == 409


class SuccessfulProvider:
    async def transcribe(self, audio, **kwargs):
        assert audio.read() == b"RIFF-audio"
        assert kwargs["model"] == get_settings().transcript_model_name
        return TranscriptionResponse(
            text="سلام دنیا",
            language="fa",
            segments=[{"id": 0, "start": 0, "end": 1, "text": "سلام دنیا"}],
            raw_response={
                "text": "سلام دنیا",
                "words": [
                    {"word": "سلام", "start": 0.05, "end": 0.4},
                    {"word": "دنیا", "start": 0.6, "end": 0.95},
                ],
            },
            external_request_id="remote-123",
        )


@pytest.mark.asyncio
async def test_worker_completes_job_and_persists_canonical_and_raw_artifacts(
    client, session_factory
):
    owner, meeting, voice = await _meeting_with_voice(client, "worker-owner@example.com")
    queued = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )
    job = queued.json()[0]
    worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: SuccessfulProvider(),
    )

    assert await worker.run_once() is True

    status = await client.get(
        f"/processing/jobs/{job['id']}", headers=authorization(owner)
    )
    results = await client.get(
        f"/voices/{voice['id']}/results", headers=authorization(owner)
    )
    transcript = await client.get(
        f"/results/{job['result_id']}/transcript", headers=authorization(owner)
    )
    assert status.json()["status"] == "succeeded"
    assert status.json()["attempts"][0]["external_request_id"] == "remote-123"
    assert len(results.json()) == 1
    assert transcript.status_code == 200
    assert transcript.json()["text"] == "سلام دنیا"
    assert transcript.json()["processing_status"] == "queued"
    assert {item["artifact_type"] for item in results.json()[0]["artifacts"]} == {
        "transcript_json",
        "raw_text",
    }
    transcript_artifact = next(
        item for item in results.json()[0]["artifacts"] if item["artifact_type"] == "transcript_json"
    )
    payload = json.loads(
        client.storage.objects[
            (transcript_artifact["minio_bucket"], transcript_artifact["minio_key"])
        ]
    )
    assert payload["schema_version"] == "canonical-transcript/v1"
    assert payload["text"] == "سلام دنیا"
    async with session_factory() as session:
        event_types = set(await session.scalars(select(History.event_type)))
        assert {"processing.started", "processing.succeeded"} <= event_types


class SuccessfulDiarizationProvider:
    async def diarize(self, audio_path):
        assert audio_path.read_bytes() == b"RIFF-audio"
        return [
            DiarizationTurn(0, 500, "SPEAKER_00"),
            DiarizationTurn(500, 1000, "SPEAKER_01"),
        ]


@pytest.mark.asyncio
async def test_diarization_worker_finishes_pipeline_and_exposes_speaker_transcript(
    client, session_factory
):
    owner, meeting, voice = await _meeting_with_voice(client, "diarization-owner@example.com")
    asr_worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: SuccessfulProvider(),
    )
    diarization_worker = DiarizationWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: SuccessfulDiarizationProvider(),
    )

    assert await asr_worker.run_once() is True
    assert await diarization_worker.run_once() is True

    results = await client.get(
        f"/voices/{voice['id']}/results", headers=authorization(owner)
    )
    result = results.json()[0]
    transcript = await client.get(
        f"/results/{result['id']}/transcript", headers=authorization(owner)
    )
    assert transcript.status_code == 200
    assert transcript.json()["schema_version"] == "speaker-transcript/v1"
    assert transcript.json()["processing_status"] == "succeeded"
    assert [segment["speaker_id"] for segment in transcript.json()["segments"]] == [
        "SPEAKER_00",
    ]
    assert transcript.json()["segments"][0]["speaker_ids"] == [
        "SPEAKER_00",
    ]
    assert {item["artifact_type"] for item in result["artifacts"]} == {
        "transcript_json",
        "raw_text",
        "diarization_json",
        "aligned_transcript_json",
    }
    async with session_factory() as session:
        stored_voice = await session.get(VoiceFile, uuid.UUID(voice["id"]))
        stages = set(await session.scalars(select(ProcessingJob.stage)))
        labels = set(await session.scalars(select(DiarizationSpeaker.label)))
        assert stored_voice.status == VoiceStatus.FINISHED
        assert stages == {ProcessingStage.TRANSCRIPTION, ProcessingStage.DIARIZATION}
        assert labels == {"SPEAKER_00", "SPEAKER_01"}


class SegmentOnlyProvider:
    async def transcribe(self, *_args, **_kwargs):
        return TranscriptionResponse(
            text="متن بدون زمان",
            language="fa",
            segments=[{"id": 0, "start": 0, "end": 1, "text": "متن بدون زمان"}],
            raw_response={"text": "متن بدون زمان"},
        )


@pytest.mark.asyncio
async def test_segment_timestamps_complete_pipeline_without_word_timestamps(
    client, session_factory
):
    owner, meeting, voice = await _meeting_with_voice(client, "timestamp-error@example.com")
    asr_worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: SegmentOnlyProvider(),
    )
    diarization_worker = DiarizationWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: SuccessfulDiarizationProvider(),
    )

    assert await asr_worker.run_once() is True
    assert await diarization_worker.run_once() is True
    assert await diarization_worker.run_once() is False

    results = await client.get(
        f"/voices/{voice['id']}/results", headers=authorization(owner)
    )
    transcript = await client.get(
        f"/results/{results.json()[0]['id']}/transcript", headers=authorization(owner)
    )
    assert transcript.status_code == 200
    assert transcript.json()["schema_version"] == "speaker-transcript/v1"
    assert transcript.json()["processing_status"] == "succeeded"
    assert transcript.json()["processing_error"] is None
    assert transcript.json()["segments"][0]["speaker_ids"] == [
        "SPEAKER_00",
    ]
    async with session_factory() as session:
        stored_voice = await session.get(VoiceFile, uuid.UUID(voice["id"]))
        assert stored_voice.status == VoiceStatus.FINISHED


class FlakyProvider:
    def __init__(self):
        self.calls = 0

    async def transcribe(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls < 3:
            raise TranscriptionError("temporary", code="asr_timeout", retryable=True)
        return TranscriptionResponse(
            text="موفق",
            language="fa",
            segments=[],
            raw_response={"text": "موفق"},
        )


@pytest.mark.asyncio
async def test_worker_retries_retryable_failure_three_times_as_append_only_attempts(
    client, session_factory
):
    owner, meeting, _ = await _meeting_with_voice(client, "retry-worker@example.com")
    provider = FlakyProvider()
    worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: provider,
    )

    assert await worker.run_once() is True
    assert await worker.run_once() is True
    assert await worker.run_once() is True

    async with session_factory() as session:
        job = await session.scalar(
            select(ProcessingJob).options(selectinload(ProcessingJob.attempts))
        )
        assert [attempt.status for attempt in job.attempts] == [
            ProcessingAttemptStatus.FAILED,
            ProcessingAttemptStatus.FAILED,
            ProcessingAttemptStatus.SUCCEEDED,
        ]
        assert await session.scalar(select(func.count(ResultArtifact.id))) == 2


class PermanentFailureProvider:
    async def transcribe(self, *_args, **_kwargs):
        raise TranscriptionError(
            "bad credentials",
            code="asr_authentication_failed",
            retryable=False,
        )


@pytest.mark.asyncio
async def test_worker_does_not_retry_permanent_failure_and_marks_voice_error(
    client, session_factory
):
    owner, meeting, voice = await _meeting_with_voice(client, "failed-worker@example.com")
    worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: PermanentFailureProvider(),
    )

    assert await worker.run_once() is True
    assert await worker.run_once() is False

    async with session_factory() as session:
        attempts = (await session.scalars(select(ProcessingAttempt))).all()
        stored_voice = await session.get(VoiceFile, uuid.UUID(voice["id"]))
        assert len(attempts) == 1
        assert attempts[0].status == ProcessingAttemptStatus.FAILED
        assert attempts[0].error_code == "asr_authentication_failed"
        assert stored_voice.status == VoiceStatus.ERROR
        event_types = set(await session.scalars(select(History.event_type)))
        assert {"processing.started", "processing.failed"} <= event_types


class TemporaryFailureProvider:
    async def transcribe(self, *_args, **_kwargs):
        raise TranscriptionError(
            "temporary outage",
            code="asr_http_503",
            retryable=True,
        )


@pytest.mark.asyncio
async def test_worker_stops_after_three_retryable_attempts_and_keeps_failure_history(
    client, session_factory
):
    owner, meeting, voice = await _meeting_with_voice(
        client, "exhausted-worker@example.com"
    )
    worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: TemporaryFailureProvider(),
    )

    assert [await worker.run_once() for _ in range(3)] == [True, True, True]
    assert await worker.run_once() is False

    async with session_factory() as session:
        attempts = (
            await session.scalars(
                select(ProcessingAttempt).order_by(ProcessingAttempt.attempt_number)
            )
        ).all()
        stored_voice = await session.get(VoiceFile, uuid.UUID(voice["id"]))
        event_types = list(await session.scalars(select(History.event_type)))
        assert [attempt.attempt_number for attempt in attempts] == [1, 2, 3]
        assert all(
            attempt.status == ProcessingAttemptStatus.FAILED for attempt in attempts
        )
        assert stored_voice.status == VoiceStatus.ERROR
        assert event_types.count("processing.retry_queued") == 2
        assert event_types.count("processing.failed") == 1
