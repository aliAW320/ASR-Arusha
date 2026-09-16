import io
import json
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import (
    BrokerOutboxMessage,
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
    ResultArtifactType,
    VoiceFile,
    VoiceStatus,
)
from asr.provider import TranscriptionError, TranscriptionResponse
from asr.worker import ASRWorker
from cleaner.provider import CleanerError, CleanerResponse
from cleaner.worker import CleanerWorker
from diarization.provider import DiarizationTurn
from diarization.worker import DiarizationWorker
from conftest import (
    accept_diarization,
    authorization,
    mark_diarization_available,
    register_user,
    run_preprocessing,
)


async def _meeting_with_voice(client, email: str, session_factory=None):
    """Upload a voice and opt it into diarization.

    Diarization is optional per voice now, so a test that wants the diarized
    pipeline has to say so: declare a live worker, then answer yes.
    """
    if session_factory is not None:
        await mark_diarization_available(session_factory)
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
    if session_factory is not None:
        await accept_diarization(client, owner, uploaded.json()["id"])
        await run_preprocessing(session_factory, client.storage)
    return owner, meeting, uploaded.json()


@pytest.mark.asyncio
async def test_upload_queues_persistent_job_attempt_and_registry(client, session, session_factory):
    # Was written when upload queued a single TRANSCRIPTION job. Uploading now
    # queues TRANSCRIPTION and DIARIZATION as two independent root jobs (see
    # _queue_transcription_and_diarization) so alignment can run regardless of
    # which one finishes first -- counts and the job lookup below are updated
    # for that, everything else about the test is unchanged.
    owner, meeting, voice = await _meeting_with_voice(client, "queue-owner@example.com", session_factory)
    response = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )

    assert response.status_code == 200, response.text
    jobs = {job["stage"]: job for job in response.json()}
    assert set(jobs) == {"preprocess", "transcription", "diarization"}
    job = jobs["transcription"]
    assert job["voice_id"] == voice["id"]
    assert job["status"] == "queued"
    assert job["attempts"][0]["attempt_number"] == 1
    assert jobs["diarization"]["status"] == "queued"
    assert await session.scalar(select(func.count(Result.id))) == 1
    assert await session.scalar(select(func.count(ProcessingJob.id))) == 3
    assert await session.scalar(select(func.count(ProcessingAttempt.id))) == 3
    assert await session.scalar(select(func.count(ModelRuntime.id))) == 2
    assert await session.scalar(select(func.count(ModelDefinition.id))) == 2
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
    # Updated the same way as test_upload_queues_persistent_job_attempt_and_registry
    # above: every "process" call now creates two independent jobs (TRANSCRIPTION
    # + DIARIZATION) instead of one, so job/attempt lookups here target the
    # TRANSCRIPTION one explicitly and the final count doubles (2 calls x 2 jobs).
    owner, meeting, _ = await _meeting_with_voice(client, "reprocess-owner@example.com", session_factory)
    outsider = await register_user(client, "reprocess-outsider@example.com")

    first = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )
    duplicate = await client.post(
        f"/meetings/{meeting['id']}/process", headers=authorization(owner)
    )
    first_transcription = next(
        job for job in first.json() if job["stage"] == "transcription"
    )
    job_id = first_transcription["id"]
    denied_start = await client.post(
        f"/meetings/{meeting['id']}/process", headers=authorization(outsider)
    )
    denied_status = await client.get(
        f"/processing/jobs/{job_id}", headers=authorization(outsider)
    )
    assert first.status_code == 200
    assert duplicate.status_code == 409
    async with session_factory() as session:
        attempt = await session.scalar(
            select(ProcessingAttempt)
            .join(ProcessingAttempt.job)
            .where(ProcessingJob.stage == ProcessingStage.TRANSCRIPTION)
        )
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
    second_preprocess = next(
        job for job in second.json() if job["stage"] == "preprocess"
    )
    owner_result = await client.get(
        f"/results/{second_preprocess['result_id']}", headers=authorization(owner)
    )
    denied_result = await client.get(
        f"/results/{second_preprocess['result_id']}", headers=authorization(outsider)
    )
    assert second.status_code == 202
    assert first_transcription["result_id"] != second_preprocess["result_id"]
    assert denied_start.status_code == 403
    assert denied_status.status_code == 403
    assert owner_result.status_code == 200
    assert denied_result.status_code == 403
    assert len(owner_status.json()) == 4
    async with session_factory() as session:
        # 2 runtimes/definitions now: ASR ("openai-compatible-asr") and
        # diarization ("pyannote.audio-local"), each registered once and
        # reused across both process() calls -- not duplicated per call.
        assert await session.scalar(select(func.count(ModelRuntime.id))) == 2
        assert await session.scalar(select(func.count(ModelDefinition.id))) == 2
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
    owner, meeting, voice = await _meeting_with_voice(client, "worker-owner@example.com", session_factory)
    queued = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )
    # Upload now queues TRANSCRIPTION and DIARIZATION independently; this test
    # only drives the ASR worker, so it needs the TRANSCRIPTION job specifically.
    job = next(item for item in queued.json() if item["stage"] == "transcription")
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
        "normalized_audio",
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


class SuccessfulCleanerProvider:
    async def clean(self, segments, **kwargs):
        assert kwargs["model"] == get_settings().cleaner_model_name
        return CleanerResponse(
            segments=[{**segment, "text": f"{segment['text']} اصلاح‌شده"} for segment in segments],
            external_request_id="cleaner-remote-123",
        )

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_diarization_worker_finishes_pipeline_and_exposes_speaker_transcript(
    client, session_factory
):
    owner, meeting, voice = await _meeting_with_voice(client, "diarization-owner@example.com", session_factory)
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
    cleaner_worker = CleanerWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: SuccessfulCleanerProvider(),
    )

    assert await asr_worker.run_once() is True
    async with session_factory() as session:
        assert await session.scalar(
            select(func.count(BrokerOutboxMessage.id)).where(
                BrokerOutboxMessage.queue_name == "cleaning.queue"
            )
        ) == 0
    assert await diarization_worker.run_once() is True
    async with session_factory() as session:
        assert await session.scalar(
            select(func.count(BrokerOutboxMessage.id)).where(
                BrokerOutboxMessage.queue_name == "cleaning.queue"
            )
        ) == 1
    assert await cleaner_worker.run_once() is True

    results = await client.get(
        f"/voices/{voice['id']}/results", headers=authorization(owner)
    )
    result = results.json()[0]
    transcript = await client.get(
        f"/results/{result['id']}/transcript", headers=authorization(owner)
    )
    assert transcript.status_code == 200
    assert transcript.json()["schema_version"] == "cleaned-speaker-transcript/v1"
    assert transcript.json()["processing_status"] == "succeeded"
    assert [segment["speaker_id"] for segment in transcript.json()["segments"]] == [
        "SPEAKER_00",
    ]
    assert transcript.json()["segments"][0]["speaker_ids"] == [
        "SPEAKER_00",
    ]
    assert transcript.json()["segments"][0]["text"].endswith("اصلاح‌شده")
    assert {item["artifact_type"] for item in result["artifacts"]} == {
        "normalized_audio",
        "transcript_json",
        "raw_text",
        "diarization_json",
        "aligned_transcript_json",
        "cleaned_text",
    }
    async with session_factory() as session:
        stored_voice = await session.get(VoiceFile, uuid.UUID(voice["id"]))
        stages = set(await session.scalars(select(ProcessingJob.stage)))
        labels = set(await session.scalars(select(DiarizationSpeaker.label)))
        assert stored_voice.status == VoiceStatus.FINISHED
        assert stages == {
            ProcessingStage.PREPROCESS,
            ProcessingStage.TRANSCRIPTION,
            ProcessingStage.DIARIZATION,
            ProcessingStage.ALIGNMENT,
            ProcessingStage.CLEANING,
            ProcessingStage.MEETING_COMPOSE,
        }
        assert labels == {"SPEAKER_00", "SPEAKER_01"}
        # Cleaning completing (even for the whole meeting) must not push
        # straight to the KB: publication now waits for an explicit human
        # approval on the Meeting, so no mcp.queue work exists yet.
        mcp_messages = (
            await session.scalars(
                select(BrokerOutboxMessage).where(
                    BrokerOutboxMessage.queue_name == "mcp.queue"
                )
            )
        ).all()
        assert mcp_messages == []


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
    owner, meeting, voice = await _meeting_with_voice(client, "timestamp-error@example.com", session_factory)
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
    cleaner_worker = CleanerWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: SuccessfulCleanerProvider(),
    )

    assert await asr_worker.run_once() is True
    assert await diarization_worker.run_once() is True
    assert await cleaner_worker.run_once() is True
    assert await diarization_worker.run_once() is False

    results = await client.get(
        f"/voices/{voice['id']}/results", headers=authorization(owner)
    )
    transcript = await client.get(
        f"/results/{results.json()[0]['id']}/transcript", headers=authorization(owner)
    )
    assert transcript.status_code == 200
    assert transcript.json()["schema_version"] == "cleaned-speaker-transcript/v1"
    assert transcript.json()["processing_status"] == "succeeded"
    assert transcript.json()["processing_error"] is None
    assert transcript.json()["segments"][0]["speaker_ids"] == [
        "SPEAKER_00",
    ]
    async with session_factory() as session:
        stored_voice = await session.get(VoiceFile, uuid.UUID(voice["id"]))
        assert stored_voice.status == VoiceStatus.FINISHED


@pytest.mark.asyncio
async def test_transcript_endpoint_serves_the_text_before_and_after_cleaning(
    client, session_factory
):
    owner, _, voice = await _meeting_with_voice(client, "transcript-variant@example.com", session_factory)
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
    cleaner_worker = CleanerWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: SuccessfulCleanerProvider(),
    )
    assert await asr_worker.run_once() is True
    assert await diarization_worker.run_once() is True
    assert await cleaner_worker.run_once() is True
    results = await client.get(
        f"/voices/{voice['id']}/results", headers=authorization(owner)
    )
    result_id = results.json()[0]["id"]

    cleaned = await client.get(
        f"/results/{result_id}/transcript", headers=authorization(owner)
    )
    original = await client.get(
        f"/results/{result_id}/transcript?variant=original",
        headers=authorization(owner),
    )
    rejected = await client.get(
        f"/results/{result_id}/transcript?variant=nonsense",
        headers=authorization(owner),
    )

    assert cleaned.status_code == 200, cleaned.text
    assert original.status_code == 200, original.text
    # The default stays the best available text; "original" reaches past the
    # cleaner so the UI can put the two versions side by side.
    assert cleaned.json()["schema_version"] == "cleaned-speaker-transcript/v1"
    assert original.json()["schema_version"] == "speaker-transcript/v1"
    assert cleaned.json()["segments"][0]["text"].endswith("اصلاح‌شده")
    assert not original.json()["segments"][0]["text"].endswith("اصلاح‌شده")
    assert rejected.status_code == 422


class PermanentCleanerFailureProvider:
    async def clean(self, *_args, **_kwargs):
        raise CleanerError(
            "cleaner rejected output",
            code="cleaner_invalid_response",
            retryable=False,
        )

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_cleaner_failure_keeps_aligned_transcript_and_exposes_exact_error(
    client, session_factory
):
    owner, _, voice = await _meeting_with_voice(client, "cleaner-error@example.com", session_factory)
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
    cleaner_worker = CleanerWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: PermanentCleanerFailureProvider(),
    )

    assert await asr_worker.run_once() is True
    assert await diarization_worker.run_once() is True
    assert await cleaner_worker.run_once() is True

    results = await client.get(
        f"/voices/{voice['id']}/results", headers=authorization(owner)
    )
    transcript = await client.get(
        f"/results/{results.json()[0]['id']}/transcript", headers=authorization(owner)
    )
    assert transcript.status_code == 200
    assert transcript.json()["schema_version"] == "speaker-transcript/v1"
    assert transcript.json()["processing_status"] == "failed"
    assert transcript.json()["processing_error"] == {
        "stage": "cleaning",
        "code": "cleaner_invalid_response",
        "message": "cleaner rejected output",
        "attempt_number": 1,
    }
    async with session_factory() as session:
        stored_voice = await session.get(VoiceFile, uuid.UUID(voice["id"]))
        assert stored_voice.status == VoiceStatus.ERROR


class RetryingCleanerProvider(SuccessfulCleanerProvider):
    def __init__(self):
        self.calls = 0

    async def clean(self, segments, **kwargs):
        self.calls += 1
        if self.calls < 3:
            raise CleanerError("temporary", code="cleaner_timeout", retryable=True)
        return await super().clean(segments, **kwargs)


@pytest.mark.asyncio
async def test_cleaner_retries_three_times_and_persists_only_final_artifact(
    client, session_factory
):
    await _meeting_with_voice(client, "cleaner-retry@example.com", session_factory)
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
    provider = RetryingCleanerProvider()
    cleaner_worker = CleanerWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: provider,
    )

    assert await asr_worker.run_once() is True
    assert await diarization_worker.run_once() is True
    assert await cleaner_worker.run_once() is True
    assert await cleaner_worker.run_once() is True
    assert await cleaner_worker.run_once() is True

    async with session_factory() as session:
        cleaning_job = await session.scalar(
            select(ProcessingJob)
            .where(ProcessingJob.stage == ProcessingStage.CLEANING)
            .options(selectinload(ProcessingJob.attempts))
        )
        assert [attempt.status for attempt in cleaning_job.attempts] == [
            ProcessingAttemptStatus.FAILED,
            ProcessingAttemptStatus.FAILED,
            ProcessingAttemptStatus.SUCCEEDED,
        ]
        cleaned_count = await session.scalar(
            select(func.count(ResultArtifact.id)).where(
                ResultArtifact.artifact_type == ResultArtifactType.CLEANED_TEXT
            )
        )
        assert cleaned_count == 1


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
    owner, meeting, _ = await _meeting_with_voice(client, "retry-worker@example.com", session_factory)
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
            select(ProcessingJob)
            .options(selectinload(ProcessingJob.attempts))
            .where(ProcessingJob.stage == ProcessingStage.TRANSCRIPTION)
        )
        assert [attempt.status for attempt in job.attempts] == [
            ProcessingAttemptStatus.FAILED,
            ProcessingAttemptStatus.FAILED,
            ProcessingAttemptStatus.SUCCEEDED,
        ]
        # Canonical ASR JSON + raw text + the normalized audio input artifact.
        assert await session.scalar(select(func.count(ResultArtifact.id))) == 3


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
    owner, meeting, voice = await _meeting_with_voice(client, "failed-worker@example.com", session_factory)
    worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: PermanentFailureProvider(),
    )

    assert await worker.run_once() is True
    assert await worker.run_once() is False

    async with session_factory() as session:
        # Upload also queues an independent DIARIZATION job/attempt now, so
        # this filters to the TRANSCRIPTION attempt this worker actually ran.
        attempts = (
            await session.scalars(
                select(ProcessingAttempt)
                .join(ProcessingAttempt.job)
                .where(ProcessingJob.stage == ProcessingStage.TRANSCRIPTION)
            )
        ).all()
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
        client, "exhausted-worker@example.com", session_factory
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
        # Same as above: filter to the TRANSCRIPTION job's attempts so the
        # independently-queued DIARIZATION attempt doesn't get counted here.
        attempts = (
            await session.scalars(
                select(ProcessingAttempt)
                .join(ProcessingAttempt.job)
                .where(ProcessingJob.stage == ProcessingStage.TRANSCRIPTION)
                .order_by(ProcessingAttempt.attempt_number)
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


# ---------------------------------------------------------------------------
# ASR + diarization run independently and merge via word-level alignment,
# regardless of which one finishes first.
# ---------------------------------------------------------------------------


class WordTimestampProvider:
    async def transcribe(self, audio, **kwargs):
        assert audio.read() == b"RIFF-audio"
        return TranscriptionResponse(
            text="سلام دنیا",
            language="fa",
            segments=[{"id": 0, "start": 0, "end": 1.5, "text": "سلام دنیا"}],
            raw_response={"text": "سلام دنیا"},
            words=[
                {"word": "سلام", "start": 0.05, "end": 0.4},
                {"word": "دنیا", "start": 1.1, "end": 1.4},
            ],
        )


@pytest.mark.asyncio
async def test_alignment_runs_via_word_level_merge_when_asr_finishes_before_diarization(
    client, session_factory
):
    owner, meeting, voice = await _meeting_with_voice(
        client, "order-asr-first@example.com", session_factory
    )
    asr_worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: WordTimestampProvider(),
    )
    diarization_worker = DiarizationWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: SuccessfulDiarizationProvider(),
    )

    assert await asr_worker.run_once() is True

    async with session_factory() as session:
        stages = set(await session.scalars(select(ProcessingJob.stage)))
        assert ProcessingStage.ALIGNMENT not in stages  # diarization not done yet

    assert await diarization_worker.run_once() is True

    async with session_factory() as session:
        stages = set(await session.scalars(select(ProcessingJob.stage)))
        assert {
            ProcessingStage.TRANSCRIPTION,
            ProcessingStage.DIARIZATION,
            ProcessingStage.ALIGNMENT,
            ProcessingStage.CLEANING,
        } <= stages

    results = await client.get(
        f"/voices/{voice['id']}/results", headers=authorization(owner)
    )
    result = results.json()[0]
    assert {item["artifact_type"] for item in result["artifacts"]} >= {
        "transcript_json",
        "diarization_json",
        "aligned_transcript_json",
    }
    artifact = next(
        item for item in result["artifacts"] if item["artifact_type"] == "aligned_transcript_json"
    )
    payload = json.loads(
        client.storage.objects[(artifact["minio_bucket"], artifact["minio_key"])]
    )
    assert [word["text"] for word in payload["words"]] == ["سلام", "دنیا"]
    assert [word["speaker_id"] for word in payload["words"]] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]


@pytest.mark.asyncio
async def test_alignment_runs_via_word_level_merge_when_diarization_finishes_before_asr(
    client, session_factory
):
    """Same scenario as above with the two workers run in the opposite order
    -- proves the merge does not depend on execution order."""
    owner, meeting, voice = await _meeting_with_voice(
        client, "order-diarization-first@example.com", session_factory
    )
    asr_worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: WordTimestampProvider(),
    )
    diarization_worker = DiarizationWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: SuccessfulDiarizationProvider(),
    )

    assert await diarization_worker.run_once() is True

    async with session_factory() as session:
        stages = set(await session.scalars(select(ProcessingJob.stage)))
        assert ProcessingStage.ALIGNMENT not in stages  # transcription not done yet

    assert await asr_worker.run_once() is True

    results = await client.get(
        f"/voices/{voice['id']}/results", headers=authorization(owner)
    )
    result = results.json()[0]
    artifact = next(
        item for item in result["artifacts"] if item["artifact_type"] == "aligned_transcript_json"
    )
    payload = json.loads(
        client.storage.objects[(artifact["minio_bucket"], artifact["minio_key"])]
    )
    assert [word["text"] for word in payload["words"]] == ["سلام", "دنیا"]
    assert [word["speaker_id"] for word in payload["words"]] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]
    async with session_factory() as session:
        stages = set(await session.scalars(select(ProcessingJob.stage)))
        assert {
            ProcessingStage.TRANSCRIPTION,
            ProcessingStage.DIARIZATION,
            ProcessingStage.ALIGNMENT,
            ProcessingStage.CLEANING,
        } <= stages
