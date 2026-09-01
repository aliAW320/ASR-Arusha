from pathlib import Path

import pytest

from diarization.alignment import align_transcript_to_speakers
from diarization.bootstrap import preload_model
from diarization.provider import (
    DiarizationError,
    DiarizationTurn,
    PyannoteLocalProvider,
)


def _transcript(segments):
    return {
        "schema_version": "canonical-transcript/v1",
        "language": "fa",
        "source_id": "voice-1",
        "model": "whisper",
        "text": "سلام دنیا",
        "words": [],
        "segments": segments,
        "metrics": {},
    }


def test_alignment_assigns_asr_segments_by_temporal_overlap():
    aligned = align_transcript_to_speakers(
        _transcript(
            [
                {"id": "a", "text": "سلام", "start_ms": 100, "end_ms": 700},
                {"id": "b", "text": "دنیا", "start_ms": 1100, "end_ms": 1500},
            ]
        ),
        [
            DiarizationTurn(0, 1000, "SPEAKER_00"),
            DiarizationTurn(1000, 2000, "SPEAKER_01"),
        ],
        diarization_model="pyannote/speaker-diarization-3.1",
    )

    assert aligned["schema_version"] == "speaker-transcript/v1"
    assert [(item["start_ms"], item["end_ms"]) for item in aligned["segments"]] == [
        (100, 700),
        (1100, 1500),
    ]
    assert [item["speaker_id"] for item in aligned["segments"]] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]
    assert [item["speaker_ids"] for item in aligned["segments"]] == [
        ["SPEAKER_00"],
        ["SPEAKER_01"],
    ]
    assert [item["text"] for item in aligned["segments"]] == ["سلام", "دنیا"]
    assert aligned["words"] == []


def test_alignment_selects_speaker_with_largest_total_segment_overlap():
    aligned = align_transcript_to_speakers(
        _transcript([{"text": "مشترک", "start_ms": 800, "end_ms": 1400}]),
        [
            DiarizationTurn(0, 1000, "SPEAKER_00"),
            DiarizationTurn(900, 2000, "SPEAKER_01"),
        ],
        diarization_model="model",
    )

    segment = aligned["segments"][0]
    assert segment["speaker_ids"] == ["SPEAKER_01"]
    assert segment["speaker_id"] == "SPEAKER_01"
    assert segment["text"] == "مشترک"


@pytest.mark.parametrize(
    ("segments", "code"),
    [
        ([], "missing_segment_timestamps"),
        ([{"text": "سلام", "start_ms": None, "end_ms": 100}], "invalid_segment_timestamps"),
    ],
)
def test_alignment_stops_for_missing_or_invalid_segment_timestamps(segments, code):
    with pytest.raises(DiarizationError) as raised:
        align_transcript_to_speakers(
            _transcript(segments),
            [DiarizationTurn(0, 1000, "SPEAKER_00")],
            diarization_model="model",
        )

    assert raised.value.code == code
    assert raised.value.retryable is False


def test_alignment_stops_when_timestamped_text_has_no_detected_speaker():
    with pytest.raises(DiarizationError) as raised:
        align_transcript_to_speakers(
            _transcript([{"text": "سلام", "start_ms": 0, "end_ms": 100}]),
            [],
            diarization_model="model",
        )

    assert raised.value.code == "no_speakers_detected"


def test_alignment_assigns_non_overlapping_segment_to_nearest_speaker():
    aligned = align_transcript_to_speakers(
        _transcript([{"text": "میان سکوت", "start_ms": 1200, "end_ms": 1300}]),
        [
            DiarizationTurn(0, 1000, "SPEAKER_00"),
            DiarizationTurn(1600, 2000, "SPEAKER_01"),
        ],
        diarization_model="model",
    )

    assert aligned["segments"][0]["speaker_ids"] == ["SPEAKER_00"]


@pytest.mark.asyncio
async def test_local_provider_parses_default_pyannote_speaker_labels_without_remote_api():
    captured = {}

    class Turn:
        def __init__(self, start, end):
            self.start = start
            self.end = end

    class Annotation:
        def itertracks(self, *, yield_label):
            assert yield_label is True
            return iter(
                [
                    (Turn(1.5, 2.0), "track-b", "SPEAKER_01"),
                    (Turn(0.0, 1.25), "track-a", "SPEAKER_00"),
                ]
            )

    class Pipeline:
        def __call__(self, path):
            captured["path"] = path
            return Annotation()

    def factory(model, token):
        captured.update(model=model, token=token)
        return Pipeline()

    provider = PyannoteLocalProvider(
        model_name="pyannote/speaker-diarization-3.1",
        token="hf-test",
        pipeline_factory=factory,
    )
    turns = await provider.diarize(Path("sample.wav"))

    assert captured == {
        "model": "pyannote/speaker-diarization-3.1",
        "token": "hf-test",
        "path": "sample.wav",
    }
    assert turns == [
        DiarizationTurn(0, 1250, "SPEAKER_00"),
        DiarizationTurn(1500, 2000, "SPEAKER_01"),
    ]


def test_local_provider_rejects_missing_huggingface_token_before_model_loading():
    with pytest.raises(DiarizationError) as raised:
        PyannoteLocalProvider(model_name="model", token="")

    assert raised.value.code == "diarization_configuration_error"
    assert raised.value.retryable is False


@pytest.mark.asyncio
async def test_diarization_bootstrap_preloads_model_before_worker_start():
    class Provider:
        model_name = "pyannote/speaker-diarization-3.1"
        device = "cpu"

        def __init__(self):
            self.loaded = False

        async def preload(self):
            self.loaded = True

    provider = Provider()
    await preload_model(provider)

    assert provider.loaded is True


@pytest.mark.asyncio
async def test_diarization_bootstrap_propagates_download_failure():
    class Provider:
        model_name = "pyannote/speaker-diarization-3.1"
        device = "cpu"

        async def preload(self):
            raise DiarizationError(
                "download timed out",
                code="diarization_model_load_failed",
                retryable=False,
            )

    with pytest.raises(DiarizationError, match="download timed out"):
        await preload_model(Provider())
