from pathlib import Path

import pytest

from diarization.alignment import align_transcript_to_speakers
from diarization.provider import (
    DiarizationError,
    DiarizationTurn,
    PyannoteLocalProvider,
)


def _transcript(words):
    return {
        "schema_version": "canonical-transcript/v1",
        "language": "fa",
        "source_id": "voice-1",
        "model": "whisper",
        "text": "سلام دنیا",
        "words": words,
        "segments": [],
        "metrics": {},
    }


def test_alignment_uses_diarization_boundaries_and_assigns_each_word_once():
    aligned = align_transcript_to_speakers(
        _transcript(
            [
                {"text": "سلام", "start_ms": 100, "end_ms": 700},
                {"text": "دنیا", "start_ms": 1100, "end_ms": 1500},
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
        (0, 1000),
        (1000, 2000),
    ]
    assert [item["speaker_id"] for item in aligned["segments"]] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]
    assert [item["text"] for item in aligned["segments"]] == ["سلام", "دنیا"]
    assert sum(len(item["words"]) for item in aligned["segments"]) == 2


def test_alignment_resolves_overlapping_speakers_by_largest_word_overlap():
    aligned = align_transcript_to_speakers(
        _transcript([{"text": "مرزی", "start_ms": 800, "end_ms": 1400}]),
        [
            DiarizationTurn(0, 1000, "SPEAKER_00"),
            DiarizationTurn(900, 2000, "SPEAKER_01"),
        ],
        diarization_model="model",
    )

    assert aligned["segments"][0]["words"] == []
    assert aligned["segments"][1]["text"] == "مرزی"


@pytest.mark.parametrize(
    ("words", "code"),
    [
        ([], "missing_word_timestamps"),
        ([{"text": "سلام", "start_ms": None, "end_ms": 100}], "invalid_word_timestamps"),
    ],
)
def test_alignment_stops_with_explicit_error_for_missing_or_invalid_timestamps(words, code):
    with pytest.raises(DiarizationError) as raised:
        align_transcript_to_speakers(
            _transcript(words),
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
