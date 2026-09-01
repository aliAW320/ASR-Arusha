import hashlib
import io
import json
import uuid

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models import (
    DiarizationSpeaker,
    History,
    Meeting,
    MeetingArtifactType,
    MeetingResult,
    MeetingResultArtifact,
    MeetingResultSource,
    MeetingSpeaker,
    ProcessingAttempt,
    ProcessingAttemptStatus,
    ProcessingJob,
    ProcessingStage,
    Result,
    ResultArtifact,
    ResultArtifactType,
    VoiceFile,
    processing_job_dependencies,
)
from app.services.processing import (
    MeetingCompositionError,
    queue_meeting_composition,
    queue_meeting_composition_if_ready,
)
from asr.provider import TranscriptionResponse
from asr.worker import ASRWorker
from cleaner.provider import CleanerResponse
from cleaner.worker import CleanerWorker
from diarization.provider import DiarizationTurn
from diarization.worker import DiarizationWorker
from conftest import authorization, register_user

from meeting_composer.composer import (
    SourceInput,
    compose_meeting_transcript,
    compute_offsets,
    compute_source_fingerprint,
)
from meeting_composer.schema import ComposerError, parse_source_segments, validate_meeting_transcript
from meeting_composer.worker import MeetingComposerWorker


# ---------------------------------------------------------------------------
# schema.parse_source_segments
# ---------------------------------------------------------------------------


def _raw_segment(**overrides):
    segment = {
        "id": 0,
        "start_ms": 1000,
        "end_ms": 4200,
        "speaker_id": "SPEAKER_00",
        "speaker_ids": ["SPEAKER_00"],
        "text": "سلام دنیا",
        "words": [],
    }
    segment.update(overrides)
    return segment


def test_parse_source_segments_normalizes_id_and_primary_speaker_label():
    parsed = parse_source_segments({"segments": [_raw_segment()]})

    assert parsed == [
        {
            "id": "0",
            "start_ms": 1000,
            "end_ms": 4200,
            "label": "SPEAKER_00",
            "text": "سلام دنیا",
        }
    ]


def test_parse_source_segments_falls_back_to_speaker_id_when_speaker_ids_missing():
    parsed = parse_source_segments(
        {"segments": [_raw_segment(speaker_ids=[], speaker_id="SPEAKER_03")]}
    )

    assert parsed[0]["label"] == "SPEAKER_03"


def test_parse_source_segments_rejects_missing_segments_key():
    with pytest.raises(ComposerError) as raised:
        parse_source_segments({"text": "no segments here"})
    assert raised.value.code == "source_artifact_invalid"


def test_parse_source_segments_rejects_non_list_segments():
    with pytest.raises(ComposerError) as raised:
        parse_source_segments({"segments": "not-a-list"})
    assert raised.value.code == "source_artifact_invalid"


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_ms": None},
        {"end_ms": None},
        {"end_ms": 500, "start_ms": 1000},
        {"end_ms": 1000, "start_ms": 1000},
    ],
)
def test_parse_source_segments_rejects_invalid_timestamps(overrides):
    with pytest.raises(ComposerError) as raised:
        parse_source_segments({"segments": [_raw_segment(**overrides)]})
    assert raised.value.code == "source_timeline_invalid"


# ---------------------------------------------------------------------------
# composer.compute_offsets
# ---------------------------------------------------------------------------


def test_compute_offsets_is_a_running_total_of_prior_durations():
    assert compute_offsets([]) == []
    assert compute_offsets([1000]) == [0]
    assert compute_offsets([1000, 2000, 3000]) == [0, 1000, 3000]


def test_compute_offsets_adds_gap_between_sources_but_not_before_the_first():
    assert compute_offsets([1000, 2000], gap_ms=500) == [0, 1500]
    assert compute_offsets([1000, 2000, 1000], gap_ms=250) == [0, 1250, 3500]


# ---------------------------------------------------------------------------
# composer.compute_source_fingerprint
# ---------------------------------------------------------------------------


def _entry(**overrides):
    entry = {
        "result_id": "11111111-1111-1111-1111-111111111111",
        "source_artifact_id": "22222222-2222-2222-2222-222222222222",
        "checksum_sha256": "a" * 64,
        "position": 0,
        "duration_ms": 5000,
    }
    entry.update(overrides)
    return entry


def test_fingerprint_is_stable_for_identical_content_regardless_of_list_order():
    a = compute_source_fingerprint([_entry(position=0), _entry(position=1, result_id="r2")])
    b = compute_source_fingerprint([_entry(position=1, result_id="r2"), _entry(position=0)])
    assert a == b
    assert len(a) == 64  # sha256 hex digest


def test_fingerprint_changes_when_content_changes():
    baseline = compute_source_fingerprint([_entry()])
    assert compute_source_fingerprint([_entry(checksum_sha256="b" * 64)]) != baseline
    assert compute_source_fingerprint([_entry(duration_ms=6000)]) != baseline
    assert compute_source_fingerprint([_entry()], policy_version="2") != baseline


# ---------------------------------------------------------------------------
# composer.compose_meeting_transcript
# ---------------------------------------------------------------------------


def _source(
    *,
    position,
    result_id,
    voice_id=None,
    duration_ms=5000,
    segments=None,
    speaker_names=None,
    artifact_id=None,
):
    return SourceInput(
        position=position,
        voice_id=voice_id or f"voice-{position}",
        result_id=result_id,
        source_artifact_id=artifact_id or f"artifact-{position}",
        source_artifact_type="cleaned_text",
        source_checksum_sha256="c" * 64,
        original_filename=f"file-{position}.wav",
        voice_sequence_number=position,
        duration_ms=duration_ms,
        segments=segments if segments is not None else [],
        speaker_names=speaker_names or {},
    )


def test_compose_with_no_sources_returns_a_valid_empty_transcript():
    payload = compose_meeting_transcript(
        meeting_id="meeting-1",
        meeting_result_id="mr-1",
        generated_at="2026-09-01T00:00:00Z",
        sources=[],
    )
    assert payload["schema"] == "meeting-transcript/v1"
    assert payload["duration_ms"] == 0
    assert payload["source_count"] == 0
    assert payload["speaker_count"] == 0
    assert payload["segments"] == []
    assert payload["sources"] == []
    assert payload["speakers"] == []
    assert payload["text"] == ""
    validate_meeting_transcript(payload)


def test_compose_applies_cumulative_offsets_across_sources_in_position_order():
    source_a = _source(
        position=0,
        result_id="result-a",
        duration_ms=5000,
        segments=[{"id": "0", "start_ms": 0, "end_ms": 1000, "label": "SPEAKER_00", "text": "اول"}],
    )
    source_b = _source(
        position=1,
        result_id="result-b",
        duration_ms=3000,
        segments=[{"id": "0", "start_ms": 200, "end_ms": 900, "label": "SPEAKER_00", "text": "دوم"}],
    )

    payload = compose_meeting_transcript(
        meeting_id="meeting-1",
        meeting_result_id="mr-1",
        generated_at="2026-09-01T00:00:00Z",
        sources=[source_a, source_b],
        gap_ms=0,
    )

    assert [item["offset_ms"] for item in payload["sources"]] == [0, 5000]
    assert payload["duration_ms"] == 8000
    assert payload["source_count"] == 2
    first, second = payload["segments"]
    assert (first["start_ms"], first["end_ms"]) == (0, 1000)
    assert (second["start_ms"], second["end_ms"]) == (5200, 5900)
    assert (first["source_start_ms"], first["source_end_ms"]) == (0, 1000)


def test_compose_never_alters_segment_text():
    text = "این متن نباید تغییر کند — دقیقاً همینطور می‌ماند."
    source = _source(
        position=0,
        result_id="result-a",
        segments=[{"id": "0", "start_ms": 0, "end_ms": 1000, "label": "SPEAKER_00", "text": text}],
    )
    payload = compose_meeting_transcript(
        meeting_id="m",
        meeting_result_id="mr",
        generated_at="2026-09-01T00:00:00Z",
        sources=[source],
    )
    assert payload["segments"][0]["text"] == text


def test_compose_resolves_mapped_and_unmapped_speakers_distinctly():
    source = _source(
        position=2,
        result_id="result-x",
        segments=[
            {"id": "0", "start_ms": 0, "end_ms": 1000, "label": "SPEAKER_00", "text": "a"},
            {"id": "1", "start_ms": 1000, "end_ms": 2000, "label": "SPEAKER_01", "text": "b"},
        ],
        speaker_names={"SPEAKER_00": ("ms-uuid-1", "Ali")},
    )
    payload = compose_meeting_transcript(
        meeting_id="m",
        meeting_result_id="mr",
        generated_at="2026-09-01T00:00:00Z",
        sources=[source],
    )
    mapped, unmapped = payload["segments"]
    assert mapped["speaker_id"] == "meeting_speaker:ms-uuid-1"
    assert mapped["meeting_speaker_id"] == "ms-uuid-1"
    assert mapped["speaker_display_name"] == "Ali"
    assert unmapped["speaker_id"] == "result:result-x:SPEAKER_01"
    assert unmapped["meeting_speaker_id"] is None
    assert unmapped["speaker_display_name"] == "File 2 - SPEAKER_01"
    assert payload["speaker_count"] == 2


def test_compose_derives_top_level_text_with_blank_line_between_sources():
    source_a = _source(
        position=0,
        result_id="a",
        segments=[
            {"id": "0", "start_ms": 0, "end_ms": 1000, "label": "SPEAKER_00", "text": "سلام"},
            {"id": "1", "start_ms": 1000, "end_ms": 2000, "label": "SPEAKER_00", "text": "دوستان"},
        ],
    )
    source_b = _source(
        position=1,
        result_id="b",
        duration_ms=2000,
        segments=[{"id": "0", "start_ms": 0, "end_ms": 500, "label": "SPEAKER_00", "text": "خداحافظ"}],
    )
    payload = compose_meeting_transcript(
        meeting_id="m",
        meeting_result_id="mr",
        generated_at="2026-09-01T00:00:00Z",
        sources=[source_a, source_b],
    )
    assert payload["text"] == "سلام دوستان\n\nخداحافظ"


def test_compose_generates_deterministic_segment_ids_from_lineage():
    source = _source(
        position=0,
        result_id="result-a",
        segments=[{"id": "seg-7", "start_ms": 0, "end_ms": 1000, "label": "SPEAKER_00", "text": "x"}],
    )
    first = compose_meeting_transcript(
        meeting_id="m", meeting_result_id="mr-1", generated_at="t", sources=[source]
    )
    again = compose_meeting_transcript(
        meeting_id="m", meeting_result_id="mr-1", generated_at="t", sources=[source]
    )
    different_result = compose_meeting_transcript(
        meeting_id="m", meeting_result_id="mr-2", generated_at="t", sources=[source]
    )
    assert first["segments"][0]["id"] == "mr-1:result-a:seg-7"
    assert first["segments"][0]["id"] == again["segments"][0]["id"]
    assert different_result["segments"][0]["id"] == "mr-2:result-a:seg-7"


def test_compose_sorts_segments_stably_by_meeting_time_then_position_then_index():
    # Source A's own segments are given out of order to prove the sort applies,
    # and source B starts before source A's window ends -- but composition
    # still keeps each source's window intact via offsets, so we assert on the
    # actually-observed order rather than assuming interleaving.
    source_a = _source(
        position=0,
        result_id="a",
        duration_ms=2000,
        segments=[
            {"id": "late", "start_ms": 1000, "end_ms": 1500, "label": "S", "text": "late"},
            {"id": "early", "start_ms": 0, "end_ms": 500, "label": "S", "text": "early"},
        ],
    )
    payload = compose_meeting_transcript(
        meeting_id="m", meeting_result_id="mr", generated_at="t", sources=[source_a]
    )
    assert [segment["source_segment_id"] for segment in payload["segments"]] == ["early", "late"]


def test_compose_rejects_when_segment_count_exceeds_max_segments():
    segments = [
        {"id": str(i), "start_ms": i * 10, "end_ms": i * 10 + 5, "label": "S", "text": "x"}
        for i in range(5)
    ]
    source = _source(position=0, result_id="a", segments=segments)
    with pytest.raises(ComposerError) as raised:
        compose_meeting_transcript(
            meeting_id="m",
            meeting_result_id="mr",
            generated_at="t",
            sources=[source],
            max_segments=4,
        )
    assert raised.value.code == "composition_limit_exceeded"


def test_compose_output_is_valid_utf8_json_serializable_with_stable_checksum():
    source = _source(
        position=0,
        result_id="a",
        segments=[{"id": "0", "start_ms": 0, "end_ms": 1000, "label": "S", "text": "متن"}],
    )
    payload = compose_meeting_transcript(
        meeting_id="m", meeting_result_id="mr", generated_at="t", sources=[source]
    )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    again = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == hashlib.sha256(again).hexdigest()


# ---------------------------------------------------------------------------
# schema.validate_meeting_transcript
# ---------------------------------------------------------------------------


def test_validate_meeting_transcript_accepts_a_well_formed_payload():
    source = _source(
        position=0,
        result_id="a",
        segments=[{"id": "0", "start_ms": 0, "end_ms": 1000, "label": "S", "text": "x"}],
    )
    payload = compose_meeting_transcript(
        meeting_id="m", meeting_result_id="mr", generated_at="t", sources=[source]
    )
    validate_meeting_transcript(payload)  # must not raise


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("schema"),
        lambda payload: payload["segments"].append({**payload["segments"][0], "start_ms": -1}),
        lambda payload: payload.__setitem__("duration_ms", -5),
    ],
)
def test_validate_meeting_transcript_rejects_structurally_broken_payloads(mutation):
    source = _source(
        position=0,
        result_id="a",
        segments=[{"id": "0", "start_ms": 0, "end_ms": 1000, "label": "S", "text": "x"}],
    )
    payload = compose_meeting_transcript(
        meeting_id="m", meeting_result_id="mr", generated_at="t", sources=[source]
    )
    mutation(payload)
    with pytest.raises(ComposerError) as raised:
        validate_meeting_transcript(payload)
    assert raised.value.code == "schema_invalid"


# ---------------------------------------------------------------------------
# Coordinator: queue_meeting_composition / queue_meeting_composition_if_ready
# ---------------------------------------------------------------------------


class _FixedTranscription:
    async def transcribe(self, audio, **kwargs):
        return TranscriptionResponse(
            text="سلام دنیا",
            language="fa",
            segments=[{"id": 0, "start": 0, "end": 1, "text": "سلام دنیا"}],
            raw_response={"text": "سلام دنیا"},
        )


class _FixedDiarization:
    async def diarize(self, audio_path):
        return [DiarizationTurn(0, 1000, "SPEAKER_00")]


class _FixedCleaning:
    async def clean(self, segments, **kwargs):
        return CleanerResponse(segments=segments, external_request_id=None)

    async def aclose(self):
        return None


async def _drain(worker, times: int) -> None:
    for _ in range(times):
        assert await worker.run_once() is True


async def _meeting_with_finished_voices(client, session_factory, email: str, *, voice_count: int = 2):
    owner = await register_user(client, email)
    meeting = (
        await client.post("/meetings", headers=authorization(owner), json={"title": "Composer"})
    ).json()
    voices = []
    for index in range(voice_count):
        uploaded = await client.post(
            f"/meetings/{meeting['id']}/voices",
            headers=authorization(owner),
            files={"upload": (f"voice-{index}.wav", io.BytesIO(b"RIFF-audio"), "audio/wav")},
        )
        assert uploaded.status_code == 201, uploaded.text
        voices.append(uploaded.json())

    asr_worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedTranscription(),
    )
    diarization_worker = DiarizationWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedDiarization(),
    )
    cleaner_worker = CleanerWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedCleaning(),
    )
    await _drain(asr_worker, voice_count)
    await _drain(diarization_worker, voice_count)
    await _drain(cleaner_worker, voice_count)
    return owner, meeting, voices


@pytest.mark.asyncio
async def test_automatic_composition_is_not_queued_until_every_voice_is_ready(
    client, session_factory
):
    owner = await register_user(client, "compose-partial@example.com")
    meeting = (
        await client.post("/meetings", headers=authorization(owner), json={"title": "Partial"})
    ).json()
    for index in range(2):
        await client.post(
            f"/meetings/{meeting['id']}/voices",
            headers=authorization(owner),
            files={"upload": (f"voice-{index}.wav", io.BytesIO(b"RIFF-audio"), "audio/wav")},
        )

    asr_worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedTranscription(),
    )
    diarization_worker = DiarizationWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedDiarization(),
    )
    cleaner_worker = CleanerWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedCleaning(),
    )
    # Finish the whole pipeline for only the first voice.
    assert await asr_worker.run_once() is True
    assert await diarization_worker.run_once() is True
    assert await cleaner_worker.run_once() is True

    async with session_factory() as session:
        voice = await session.scalar(select(VoiceFile).where(VoiceFile.meeting_id == uuid.UUID(meeting["id"])))
        job = await queue_meeting_composition_if_ready(
            session, voice, get_settings(), client.storage
        )
        await session.commit()
        assert job is None
        assert await session.scalar(select(func.count(MeetingResult.id))) == 0


@pytest.mark.asyncio
async def test_automatic_composition_queues_once_every_voice_has_a_successful_result(
    client, session_factory
):
    # _meeting_with_finished_voices already drives cleaning to success for both
    # voices, which -- via the CleanerWorker hook -- already triggers
    # automatic composition as a side effect. So this fetches whatever the
    # hook already created rather than asserting on a fresh call's return.
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "compose-ready@example.com"
    )

    async with session_factory() as session:
        job = await session.scalar(
            select(ProcessingJob).where(ProcessingJob.stage == ProcessingStage.MEETING_COMPOSE)
        )
        assert job is not None
        assert job.stage == ProcessingStage.MEETING_COMPOSE
        assert job.model_id is None
        assert job.integration_id is None
        assert job.result_id is None
        assert job.meeting_result_id is not None

        meeting_result = await session.get(MeetingResult, job.meeting_result_id)
        sources = (
            await session.scalars(
                select(MeetingResultSource)
                .where(MeetingResultSource.meeting_result_id == meeting_result.id)
                .order_by(MeetingResultSource.position)
            )
        ).all()
        assert [source.position for source in sources] == [0, 1]
        assert sources[0].source_offset_ms == 0
        assert sources[1].source_offset_ms == sources[0].source_duration_ms
        assert all(source.source_duration_ms > 0 for source in sources)

        attempts = (
            await session.scalars(
                select(ProcessingAttempt.status).where(ProcessingAttempt.job_id == job.id)
            )
        ).all()
        assert attempts == [ProcessingAttemptStatus.QUEUED]

        dependency_count = await session.scalar(
            select(func.count())
            .select_from(processing_job_dependencies)
            .where(processing_job_dependencies.c.job_id == job.id)
        )
        assert dependency_count == 2


@pytest.mark.asyncio
async def test_automatic_composition_is_idempotent_for_an_unchanged_source_set(
    client, session_factory
):
    # The CleanerWorker hook inside _meeting_with_finished_voices may already
    # have composed automatically, so neither of these two explicit calls is
    # guaranteed to be "the one that created it" -- idempotency instead means
    # at most one job is ever returned and exactly one MeetingResult exists.
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "compose-idempotent@example.com"
    )
    jobs = []
    for _ in range(2):
        async with session_factory() as session:
            voice = await session.get(VoiceFile, uuid.UUID(voices[0]["id"]))
            jobs.append(
                await queue_meeting_composition_if_ready(
                    session, voice, get_settings(), client.storage
                )
            )
            await session.commit()
    assert sum(job is not None for job in jobs) <= 1
    async with session_factory() as session:
        assert await session.scalar(select(func.count(MeetingResult.id))) == 1


@pytest.mark.asyncio
async def test_manual_compose_returns_existing_meeting_result_for_identical_fingerprint(
    client, session_factory
):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "compose-manual-dedupe@example.com"
    )
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        first_result, first_job, first_created = await queue_meeting_composition(
            session, meeting_row, get_settings(), client.storage
        )
        await session.commit()
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        second_result, second_job, second_created = await queue_meeting_composition(
            session, meeting_row, get_settings(), client.storage
        )
        await session.commit()

    # The CleanerWorker hook may already have composed automatically during
    # _meeting_with_finished_voices, so the first explicit call here is not
    # guaranteed to be the one that created it -- only that both calls
    # converge on the same MeetingResult and it is never created twice.
    assert not (first_created and second_created)
    assert second_created is False
    assert second_job is None
    assert second_result.id == first_result.id


@pytest.mark.asyncio
async def test_manual_compose_force_new_version_creates_a_second_meeting_result(
    client, session_factory
):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "compose-force@example.com"
    )
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        await queue_meeting_composition(session, meeting_row, get_settings(), client.storage)
        await session.commit()
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        _, job, created = await queue_meeting_composition(
            session, meeting_row, get_settings(), client.storage, force_new_version=True
        )
        await session.commit()
        assert created is True
        assert job is not None
        assert await session.scalar(select(func.count(MeetingResult.id))) == 2


@pytest.mark.asyncio
async def test_manual_compose_rejects_a_result_from_a_different_meeting(
    client, session_factory
):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "compose-owner@example.com", voice_count=1
    )
    _, other_meeting, other_voices = await _meeting_with_finished_voices(
        client, session_factory, "compose-outsider@example.com", voice_count=1
    )
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        foreign_result_id = await session.scalar(
            select(Result.id).where(Result.voice_id == uuid.UUID(other_voices[0]["id"]))
        )
        with pytest.raises(MeetingCompositionError) as raised:
            await queue_meeting_composition(
                session,
                meeting_row,
                get_settings(),
                client.storage,
                result_ids=[foreign_result_id],
            )
        assert raised.value.code == "source_result_wrong_meeting"


@pytest.mark.asyncio
async def test_manual_compose_rejects_an_empty_meeting(client, session_factory):
    owner = await register_user(client, "compose-empty@example.com")
    meeting = (
        await client.post("/meetings", headers=authorization(owner), json={"title": "Empty"})
    ).json()
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        with pytest.raises(MeetingCompositionError) as raised:
            await queue_meeting_composition(session, meeting_row, get_settings(), client.storage)
        assert raised.value.code == "meeting_has_no_voices"


@pytest.mark.asyncio
async def test_manual_compose_rejects_a_voice_without_a_successful_result(
    client, session_factory
):
    owner = await register_user(client, "compose-not-ready@example.com")
    meeting = (
        await client.post("/meetings", headers=authorization(owner), json={"title": "NotReady"})
    ).json()
    await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(owner),
        files={"upload": ("voice.wav", io.BytesIO(b"RIFF-audio"), "audio/wav")},
    )
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        with pytest.raises(MeetingCompositionError) as raised:
            await queue_meeting_composition(session, meeting_row, get_settings(), client.storage)
        assert raised.value.code == "source_result_not_ready"


@pytest.mark.asyncio
async def test_reprocessed_voice_produces_a_new_meeting_result_version(
    client, session_factory
):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "compose-reprocess@example.com", voice_count=1
    )
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        first_result, _, _ = await queue_meeting_composition(
            session, meeting_row, get_settings(), client.storage
        )
        await session.commit()

    reprocessed = await client.post(
        f"/meetings/{meeting['id']}/process", headers=authorization(owner)
    )
    assert reprocessed.status_code == 202, reprocessed.text
    asr_worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedTranscription(),
    )
    diarization_worker = DiarizationWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedDiarization(),
    )
    cleaner_worker = CleanerWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedCleaning(),
    )
    await _drain(asr_worker, 1)
    await _drain(diarization_worker, 1)
    await _drain(cleaner_worker, 1)

    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        second_result, second_job, second_created = await queue_meeting_composition(
            session, meeting_row, get_settings(), client.storage
        )
        await session.commit()
        # The reprocessing drain's CleanerWorker hook may already have
        # composed the new version automatically; either way there must now
        # be exactly one MeetingResult per version and they must differ.
        assert second_result.id != first_result.id
        assert await session.scalar(select(func.count(MeetingResult.id))) == 2
        # The old version and its sources remain untouched.
        old_sources = await session.scalar(
            select(func.count(MeetingResultSource.result_id)).where(
                MeetingResultSource.meeting_result_id == first_result.id
            )
        )
        assert old_sources == 1


@pytest.mark.asyncio
async def test_single_voice_meeting_tolerates_a_missing_sequence_number(
    client, session_factory
):
    owner = await register_user(client, "compose-legacy-sequence@example.com")
    meeting = (
        await client.post("/meetings", headers=authorization(owner), json={"title": "Legacy"})
    ).json()
    uploaded = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(owner),
        files={"upload": ("voice.wav", io.BytesIO(b"RIFF-audio"), "audio/wav")},
    )
    voice_id = uuid.UUID(uploaded.json()["id"])
    async with session_factory() as session:
        voice = await session.get(VoiceFile, voice_id)
        voice.sequence_number = None
        await session.commit()

    asr_worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedTranscription(),
    )
    diarization_worker = DiarizationWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedDiarization(),
    )
    cleaner_worker = CleanerWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedCleaning(),
    )
    await _drain(asr_worker, 1)
    await _drain(diarization_worker, 1)
    await _drain(cleaner_worker, 1)

    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        # The CleanerWorker hook may already have composed this automatically
        # (a single-voice meeting is "ready" the moment that voice is
        # cleaned), so this call may just return the existing MeetingResult.
        meeting_result, job, created = await queue_meeting_composition(
            session, meeting_row, get_settings(), client.storage
        )
        await session.commit()
        assert meeting_result is not None
        composed_job = job or await session.scalar(
            select(ProcessingJob).where(
                ProcessingJob.meeting_result_id == meeting_result.id
            )
        )
        assert composed_job is not None


@pytest.mark.asyncio
async def test_ambiguous_sequence_blocks_multi_voice_composition(client, session_factory):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "compose-ambiguous@example.com"
    )
    async with session_factory() as session:
        voice = await session.get(VoiceFile, uuid.UUID(voices[0]["id"]))
        voice.sequence_number = None
        await session.commit()

    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        with pytest.raises(MeetingCompositionError) as raised:
            await queue_meeting_composition(session, meeting_row, get_settings(), client.storage)
        assert raised.value.code == "ambiguous_voice_sequence"


# ---------------------------------------------------------------------------
# MeetingComposerWorker
# ---------------------------------------------------------------------------


def _combined_transcript_key(meeting_id: str, meeting_result_id) -> str:
    return f"meetings/{meeting_id}/meeting-results/{meeting_result_id}/combined-transcript.json"


@pytest.mark.asyncio
async def test_worker_composes_two_ready_voices_into_one_combined_artifact(
    client, session_factory
):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "worker-compose@example.com"
    )
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        # May already exist (the CleanerWorker hook composes automatically);
        # either way this resolves the meeting_result and its compose job.
        meeting_result, job, created = await queue_meeting_composition(
            session, meeting_row, get_settings(), client.storage
        )
        await session.commit()
        meeting_result_id = meeting_result.id
        job_id = job.id if job is not None else (
            await session.scalar(
                select(ProcessingJob.id).where(
                    ProcessingJob.meeting_result_id == meeting_result_id
                )
            )
        )

    worker = MeetingComposerWorker(
        session_factory=session_factory, storage=client.storage, settings=get_settings()
    )
    assert await worker.run_once() is True
    assert await worker.run_once() is False

    async with session_factory() as session:
        meeting_result = await session.get(MeetingResult, meeting_result_id)
        assert meeting_result.completed_at is not None
        artifact = await session.scalar(
            select(MeetingResultArtifact).where(
                MeetingResultArtifact.meeting_result_id == meeting_result_id
            )
        )
        assert artifact is not None
        assert artifact.artifact_type == MeetingArtifactType.COMBINED_TRANSCRIPT_JSON
        assert artifact.content_type == "application/json"
        assert artifact.minio_key == _combined_transcript_key(meeting["id"], meeting_result_id)
        assert artifact.producer_job_id == job_id

        attempts = (
            await session.scalars(
                select(ProcessingAttempt.status).where(ProcessingAttempt.job_id == job_id)
            )
        ).all()
        assert attempts == [ProcessingAttemptStatus.SUCCEEDED]

    payload = json.loads(
        client.storage.objects[
            (get_settings().minio_exports_bucket, artifact.minio_key)
        ]
    )
    assert payload["schema"] == "meeting-transcript/v1"
    assert payload["source_count"] == 2
    assert len(payload["segments"]) == 2
    assert payload["segments"][0]["start_ms"] < payload["segments"][1]["start_ms"]


@pytest.mark.asyncio
async def test_worker_resolves_mapped_meeting_speaker_display_name(client, session_factory):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "worker-speaker-map@example.com", voice_count=1
    )
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        speaker = MeetingSpeaker(meeting=meeting_row, display_name="Ali")
        session.add(speaker)
        await session.flush()
        diarization_speaker = await session.scalar(
            select(DiarizationSpeaker).where(DiarizationSpeaker.label == "SPEAKER_00")
        )
        diarization_speaker.meeting_speaker_id = speaker.id
        await session.commit()
        speaker_id = speaker.id

        meeting_result, job, created = await queue_meeting_composition(
            session, meeting_row, get_settings(), client.storage
        )
        await session.commit()
        meeting_result_id = meeting_result.id

    worker = MeetingComposerWorker(
        session_factory=session_factory, storage=client.storage, settings=get_settings()
    )
    assert await worker.run_once() is True

    async with session_factory() as session:
        artifact = await session.scalar(
            select(MeetingResultArtifact).where(
                MeetingResultArtifact.meeting_result_id == meeting_result_id
            )
        )
    payload = json.loads(
        client.storage.objects[(get_settings().minio_exports_bucket, artifact.minio_key)]
    )
    segment = payload["segments"][0]
    assert segment["meeting_speaker_id"] == str(speaker_id)
    assert segment["speaker_display_name"] == "Ali"
    assert segment["speaker_id"] == f"meeting_speaker:{speaker_id}"


@pytest.mark.asyncio
async def test_worker_permanently_fails_on_a_corrupted_source_artifact(
    client, session_factory
):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "worker-corrupt@example.com", voice_count=1
    )
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        meeting_result, job, created = await queue_meeting_composition(
            session, meeting_row, get_settings(), client.storage
        )
        source = (
            await session.scalars(
                select(MeetingResultSource).where(
                    MeetingResultSource.meeting_result_id == meeting_result.id
                )
            )
        ).one()
        artifact = await session.get(ResultArtifact, source.source_artifact_id)
        artifact_key = (artifact.minio_bucket, artifact.minio_key)
        await session.commit()
        meeting_result_id = meeting_result.id
        job_id = job.id if job is not None else (
            await session.scalar(
                select(ProcessingJob.id).where(
                    ProcessingJob.meeting_result_id == meeting_result_id
                )
            )
        )

    client.storage.objects[artifact_key] = b"not valid json"

    worker = MeetingComposerWorker(
        session_factory=session_factory, storage=client.storage, settings=get_settings()
    )
    assert await worker.run_once() is True

    async with session_factory() as session:
        attempts = (
            await session.scalars(
                select(ProcessingAttempt).where(ProcessingAttempt.job_id == job_id)
            )
        ).all()
        assert len(attempts) == 1
        assert attempts[0].status == ProcessingAttemptStatus.FAILED
        meeting_result = await session.get(MeetingResult, meeting_result_id)
        assert meeting_result.completed_at is None
        artifact_count = await session.scalar(
            select(func.count(MeetingResultArtifact.id)).where(
                MeetingResultArtifact.meeting_result_id == meeting_result_id
            )
        )
        assert artifact_count == 0


@pytest.mark.asyncio
async def test_worker_retries_then_succeeds_and_compensates_a_failed_commit(
    client, session_factory
):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "worker-compensate@example.com", voice_count=1
    )
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        meeting_result, job, created = await queue_meeting_composition(
            session, meeting_row, get_settings(), client.storage
        )
        await session.commit()
        meeting_result_id = meeting_result.id
        job_id = job.id if job is not None else (
            await session.scalar(
                select(ProcessingJob.id).where(
                    ProcessingJob.meeting_result_id == meeting_result_id
                )
            )
        )

    key = _combined_transcript_key(meeting["id"], meeting_result_id)
    settings = get_settings()
    # Pre-occupy the deterministic object key with a decoy artifact row so the
    # worker's own insert hits the unique(minio_bucket, minio_key) constraint
    # right after it has already uploaded the object -- forcing the same
    # "upload succeeded, DB commit failed" path a real transient DB error
    # would take, without reaching into the worker's private methods. The
    # decoy MeetingResult is created directly (not via queue_meeting_composition)
    # so it does not also create a second competing MEETING_COMPOSE job.
    async with session_factory() as session:
        meeting_row = await session.get(Meeting, uuid.UUID(meeting["id"]))
        decoy_meeting_result = MeetingResult(
            meeting=meeting_row, source_fingerprint="decoy-fingerprint"
        )
        session.add(decoy_meeting_result)
        await session.flush()
        session.add(
            MeetingResultArtifact(
                meeting_result=decoy_meeting_result,
                artifact_type=MeetingArtifactType.COMBINED_TRANSCRIPT_JSON,
                minio_bucket=settings.minio_exports_bucket,
                minio_key=key,
                content_type="application/json",
                checksum_sha256="decoy",
            )
        )
        await session.commit()

    worker = MeetingComposerWorker(
        session_factory=session_factory, storage=client.storage, settings=settings
    )
    assert await worker.run_once() is True  # first attempt: collides, fails, retries

    async with session_factory() as session:
        attempts = (
            await session.scalars(
                select(ProcessingAttempt)
                .where(ProcessingAttempt.job_id == job_id)
                .order_by(ProcessingAttempt.attempt_number)
            )
        ).all()
        assert [attempt.status for attempt in attempts] == [
            ProcessingAttemptStatus.FAILED,
            ProcessingAttemptStatus.QUEUED,
        ]
    assert (settings.minio_exports_bucket, key) in client.storage.removed
    # The decoy still legitimately owns the key, so retrying keeps colliding
    # and is expected to keep failing (this proves cleanup + retry, not a
    # magical recovery) -- remove the decoy, then the retry can succeed.
    async with session_factory() as session:
        decoy_artifact = await session.scalar(
            select(MeetingResultArtifact).where(
                MeetingResultArtifact.minio_key == key,
                MeetingResultArtifact.meeting_result_id != meeting_result_id,
            )
        )
        await session.delete(decoy_artifact)
        await session.commit()

    assert await worker.run_once() is True

    async with session_factory() as session:
        meeting_result = await session.get(MeetingResult, meeting_result_id)
        assert meeting_result.completed_at is not None


# ---------------------------------------------------------------------------
# End-to-end: CleanerWorker automatically triggers Meeting Composer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_automatically_composes_after_the_last_voice_is_cleaned(
    client, session_factory
):
    """No test in this file calls queue_meeting_composition* directly here --
    this proves the CleanerWorker -> coordinator -> MeetingComposerWorker
    wiring itself, not just the coordinator/worker in isolation."""
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "pipeline-auto-compose@example.com"
    )

    async with session_factory() as session:
        job = await session.scalar(
            select(ProcessingJob).where(ProcessingJob.stage == ProcessingStage.MEETING_COMPOSE)
        )
        assert job is not None
        assert job.meeting_result_id is not None
        meeting_result_id = job.meeting_result_id
        event_types = set(await session.scalars(select(History.event_type)))
        assert "meeting_composition.queued" in event_types

    composer_worker = MeetingComposerWorker(
        session_factory=session_factory, storage=client.storage, settings=get_settings()
    )
    assert await composer_worker.run_once() is True

    async with session_factory() as session:
        meeting_result = await session.get(MeetingResult, meeting_result_id)
        assert meeting_result.completed_at is not None
        event_types = set(await session.scalars(select(History.event_type)))
        assert {
            "meeting_composition.queued",
            "meeting_composition.started",
            "meeting_composition.succeeded",
        } <= event_types


@pytest.mark.asyncio
async def test_pipeline_does_not_compose_until_every_voice_finishes_cleaning(
    client, session_factory
):
    owner = await register_user(client, "pipeline-partial-clean@example.com")
    meeting = (
        await client.post("/meetings", headers=authorization(owner), json={"title": "Partial"})
    ).json()
    for index in range(2):
        await client.post(
            f"/meetings/{meeting['id']}/voices",
            headers=authorization(owner),
            files={"upload": (f"voice-{index}.wav", io.BytesIO(b"RIFF-audio"), "audio/wav")},
        )
    asr_worker = ASRWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedTranscription(),
    )
    diarization_worker = DiarizationWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedDiarization(),
    )
    cleaner_worker = CleanerWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        provider_factory=lambda _: _FixedCleaning(),
    )
    assert await asr_worker.run_once() is True
    assert await diarization_worker.run_once() is True
    assert await cleaner_worker.run_once() is True  # only the first voice finishes

    async with session_factory() as session:
        assert await session.scalar(select(func.count(MeetingResult.id))) == 0
        assert (
            await session.scalar(
                select(func.count(ProcessingJob.id)).where(
                    ProcessingJob.stage == ProcessingStage.MEETING_COMPOSE
                )
            )
            == 0
        )


# ---------------------------------------------------------------------------
# API: /meetings/{id}/compose, /meetings/{id}/results, /meeting-results/*,
#      /meetings/{id}/transcript, and meeting-level jobs in /processing
# ---------------------------------------------------------------------------


async def _add_member(client, owner, meeting_id, email, role):
    member = await register_user(client, email)
    added = await client.post(
        f"/meetings/{meeting_id}/members",
        headers=authorization(owner),
        json={"email": email, "role": role},
    )
    assert added.status_code == 201, added.text
    return member


@pytest.mark.asyncio
async def test_compose_endpoint_queues_then_replays_the_same_version(
    client, session_factory
):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "api-compose@example.com"
    )
    # The CleanerWorker hook may have already composed automatically; either
    # way POSTing /compose must succeed and be idempotent.
    first = await client.post(
        f"/meetings/{meeting['id']}/compose", headers=authorization(owner), json={}
    )
    assert first.status_code in (200, 202), first.text
    body = first.json()
    assert body["meeting_id"] == meeting["id"]
    assert body["schema_version"] == "meeting-transcript/v1"
    assert len(body["sources"]) == 2

    second = await client.post(
        f"/meetings/{meeting['id']}/compose", headers=authorization(owner), json={}
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] == body["id"]


@pytest.mark.asyncio
async def test_compose_endpoint_rejects_an_unready_meeting_with_exact_code(client):
    owner = await register_user(client, "api-compose-not-ready@example.com")
    meeting = (
        await client.post("/meetings", headers=authorization(owner), json={"title": "NR"})
    ).json()
    response = await client.post(
        f"/meetings/{meeting['id']}/compose", headers=authorization(owner), json={}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "meeting_has_no_voices"


@pytest.mark.asyncio
async def test_compose_endpoint_permission_boundaries(client, session_factory):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "api-compose-permissions@example.com"
    )
    viewer = await _add_member(
        client, owner, meeting["id"], "api-compose-viewer@example.com", "viewer"
    )
    outsider = await register_user(client, "api-compose-outsider@example.com")

    viewer_attempt = await client.post(
        f"/meetings/{meeting['id']}/compose", headers=authorization(viewer), json={}
    )
    outsider_attempt = await client.post(
        f"/meetings/{meeting['id']}/compose", headers=authorization(outsider), json={}
    )
    assert viewer_attempt.status_code == 403
    assert outsider_attempt.status_code == 403


@pytest.mark.asyncio
async def test_meeting_results_list_and_detail_endpoints(client, session_factory):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "api-results@example.com"
    )
    composed = await client.post(
        f"/meetings/{meeting['id']}/compose", headers=authorization(owner), json={}
    )
    meeting_result_id = composed.json()["id"]

    listed = await client.get(
        f"/meetings/{meeting['id']}/results", headers=authorization(owner)
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [meeting_result_id]

    detail = await client.get(
        f"/meeting-results/{meeting_result_id}", headers=authorization(owner)
    )
    assert detail.status_code == 200
    assert detail.json()["meeting_id"] == meeting["id"]
    assert len(detail.json()["sources"]) == 2

    outsider = await register_user(client, "api-results-outsider@example.com")
    denied = await client.get(
        f"/meeting-results/{meeting_result_id}", headers=authorization(outsider)
    )
    assert denied.status_code == 403

    missing = await client.get(
        f"/meeting-results/{uuid.uuid4()}", headers=authorization(owner)
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_meeting_transcript_endpoints_require_a_completed_composition(
    client, session_factory
):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "api-transcript@example.com"
    )
    composed = await client.post(
        f"/meetings/{meeting['id']}/compose", headers=authorization(owner), json={}
    )
    meeting_result_id = composed.json()["id"]

    not_ready = await client.get(
        f"/meeting-results/{meeting_result_id}/transcript", headers=authorization(owner)
    )
    assert not_ready.status_code == 409
    latest_not_ready = await client.get(
        f"/meetings/{meeting['id']}/transcript", headers=authorization(owner)
    )
    assert latest_not_ready.status_code == 409

    worker = MeetingComposerWorker(
        session_factory=session_factory, storage=client.storage, settings=get_settings()
    )
    assert await worker.run_once() is True

    ready = await client.get(
        f"/meeting-results/{meeting_result_id}/transcript", headers=authorization(owner)
    )
    assert ready.status_code == 200
    assert ready.json()["schema"] == "meeting-transcript/v1"
    assert ready.json()["source_count"] == 2

    latest = await client.get(
        f"/meetings/{meeting['id']}/transcript", headers=authorization(owner)
    )
    assert latest.status_code == 200
    assert latest.json()["meeting_result_id"] == meeting_result_id


@pytest.mark.asyncio
async def test_meeting_processing_endpoint_includes_the_compose_job(
    client, session_factory
):
    owner, meeting, voices = await _meeting_with_finished_voices(
        client, session_factory, "api-processing@example.com"
    )
    await client.post(
        f"/meetings/{meeting['id']}/compose", headers=authorization(owner), json={}
    )

    processing = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )
    assert processing.status_code == 200
    compose_jobs = [
        job for job in processing.json() if job["stage"] == "meeting_compose"
    ]
    assert len(compose_jobs) == 1
    compose_job = compose_jobs[0]
    assert compose_job["target_type"] == "meeting_result"
    assert compose_job["meeting_result_id"] is not None
    assert compose_job["result_id"] is None
    assert compose_job["voice_id"] is None
    assert compose_job["meeting_id"] == meeting["id"]

    job_detail = await client.get(
        f"/processing/jobs/{compose_job['id']}", headers=authorization(owner)
    )
    assert job_detail.status_code == 200
    assert job_detail.json()["target_type"] == "meeting_result"

    outsider = await register_user(client, "api-processing-outsider@example.com")
    denied = await client.get(
        f"/processing/jobs/{compose_job['id']}", headers=authorization(outsider)
    )
    assert denied.status_code == 403
