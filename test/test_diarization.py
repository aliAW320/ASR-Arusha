from pathlib import Path

import pytest

from diarization.alignment import align_transcript_to_speakers
from diarization.bootstrap import preload_model
from diarization.provider import (
    DiarizationError,
    DiarizationTurn,
    PyannoteLocalProvider,
)


def _transcript(segments, words=None):
    return {
        "schema_version": "canonical-transcript/v1",
        "language": "fa",
        "source_id": "voice-1",
        "model": "whisper",
        "text": "سلام دنیا",
        "words": words if words is not None else [],
        "segments": segments,
        "metrics": {},
    }


def _word(text, start_ms, end_ms):
    return {"text": text, "start_ms": start_ms, "end_ms": end_ms}


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


# ---------------------------------------------------------------------------
# Word-level alignment (used whenever the canonical transcript carries
# word timestamps -- the primary path now that ASR requests them).
# ---------------------------------------------------------------------------


def _flat_words(aligned):
    return [(w["text"], w["speaker_id"]) for w in aligned["words"]]


def test_word_alignment_assigns_normal_overlap_to_the_containing_speaker():
    aligned = align_transcript_to_speakers(
        _transcript(
            [{"id": "a", "text": "سلام دنیا", "start_ms": 0, "end_ms": 1000}],
            words=[_word("سلام", 100, 400), _word("دنیا", 500, 900)],
        ),
        [DiarizationTurn(0, 1000, "SPEAKER_00")],
        diarization_model="model",
    )
    assert aligned["schema_version"] == "speaker-transcript/v1"
    assert _flat_words(aligned) == [("سلام", "SPEAKER_00"), ("دنیا", "SPEAKER_00")]
    assert len(aligned["words"]) == 2  # no word lost


def test_word_alignment_prefers_the_speaker_with_greatest_overlap_when_a_word_spans_two_turns():
    # Word [900, 1300) overlaps SPEAKER_00 for 100ms and SPEAKER_01 for 300ms.
    aligned = align_transcript_to_speakers(
        _transcript(
            [{"id": "a", "text": "میان‌مرزی", "start_ms": 900, "end_ms": 1300}],
            words=[_word("میان‌مرزی", 900, 1300)],
        ),
        [
            DiarizationTurn(0, 1000, "SPEAKER_00"),
            DiarizationTurn(1000, 2000, "SPEAKER_01"),
        ],
        diarization_model="model",
    )
    assert aligned["words"][0]["speaker_id"] == "SPEAKER_01"


def test_word_alignment_assigns_a_word_in_a_gap_to_the_nearest_interval():
    aligned = align_transcript_to_speakers(
        _transcript(
            [{"id": "a", "text": "خلا", "start_ms": 1050, "end_ms": 1100}],
            words=[_word("خلا", 1050, 1100)],
        ),
        [
            DiarizationTurn(0, 1000, "SPEAKER_00"),
            DiarizationTurn(1400, 2000, "SPEAKER_01"),
        ],
        diarization_model="model",
    )
    # 50ms from SPEAKER_00's end vs 300ms from SPEAKER_01's start.
    assert aligned["words"][0]["speaker_id"] == "SPEAKER_00"


def test_word_alignment_fills_null_timestamps_from_neighboring_words():
    aligned = align_transcript_to_speakers(
        _transcript(
            [{"id": "a", "text": "سلام ؟؟؟ دنیا", "start_ms": 0, "end_ms": 1900}],
            words=[
                _word("سلام", 0, 400),
                _word("؟؟؟", None, None),
                _word("دنیا", 1500, 1900),
            ],
        ),
        [
            DiarizationTurn(0, 1000, "SPEAKER_00"),
            DiarizationTurn(1000, 2000, "SPEAKER_01"),
        ],
        diarization_model="model",
    )
    assert len(aligned["words"]) == 3  # the untimed word is never dropped
    assert aligned["words"][1]["text"] == "؟؟؟"
    assert aligned["words"][1]["speaker_id"] in ("SPEAKER_00", "SPEAKER_01")
    assert aligned["words"][1]["start_ms"] is None
    assert aligned["words"][1]["end_ms"] is None


def test_word_alignment_handles_a_transcript_with_only_missing_timestamps():
    aligned = align_transcript_to_speakers(
        _transcript(
            [{"id": "a", "text": "ناشناخته", "start_ms": None, "end_ms": None}],
            words=[_word("ناشناخته", None, None)],
        ),
        [DiarizationTurn(0, 1000, "SPEAKER_00")],
        diarization_model="model",
    )
    # Still never dropped -- falls back to the only available speaker interval.
    assert len(aligned["words"]) == 1
    assert aligned["words"][0]["speaker_id"] == "SPEAKER_00"


def test_word_alignment_groups_consecutive_same_speaker_words_into_one_turn():
    aligned = align_transcript_to_speakers(
        _transcript(
            [{"id": "a", "text": "سلام دوست من", "start_ms": 0, "end_ms": 1500}],
            words=[
                _word("سلام", 0, 300),
                _word("دوست", 400, 700),
                _word("من", 800, 1100),
            ],
        ),
        [DiarizationTurn(0, 2000, "SPEAKER_00")],
        diarization_model="model",
    )
    assert len(aligned["segments"]) == 1
    turn = aligned["segments"][0]
    assert turn["speaker_id"] == "SPEAKER_00"
    assert turn["start_ms"] == 0
    assert turn["end_ms"] == 1100
    assert turn["text"] == "سلام دوست من"
    assert [w["text"] for w in turn["words"]] == ["سلام", "دوست", "من"]


def test_word_alignment_starts_a_new_turn_on_speaker_change():
    aligned = align_transcript_to_speakers(
        _transcript(
            [{"id": "a", "text": "سلام خداحافظ", "start_ms": 0, "end_ms": 1500}],
            words=[_word("سلام", 100, 400), _word("خداحافظ", 1100, 1400)],
        ),
        [
            DiarizationTurn(0, 1000, "SPEAKER_00"),
            DiarizationTurn(1000, 2000, "SPEAKER_01"),
        ],
        diarization_model="model",
    )
    assert len(aligned["segments"]) == 2
    assert [segment["speaker_id"] for segment in aligned["segments"]] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]
    assert [segment["text"] for segment in aligned["segments"]] == ["سلام", "خداحافظ"]


def test_word_alignment_handles_overlapping_speaker_turns_cross_talk():
    # Two speakers talk over each other; a word landing in the overlap should
    # deterministically resolve to the speaker with more total overlap.
    aligned = align_transcript_to_speakers(
        _transcript(
            [{"id": "a", "text": "قطع", "start_ms": 400, "end_ms": 900}],
            words=[_word("قطع", 400, 900)],
        ),
        [
            DiarizationTurn(0, 800, "SPEAKER_00"),
            DiarizationTurn(300, 1000, "SPEAKER_01"),
        ],
        diarization_model="model",
    )
    # Overlap with SPEAKER_00: [400,800) = 400ms. Overlap with SPEAKER_01: [400,900) = 500ms.
    assert aligned["words"][0]["speaker_id"] == "SPEAKER_01"


def test_word_alignment_handles_very_short_diarization_and_word_intervals():
    aligned = align_transcript_to_speakers(
        _transcript(
            [{"id": "a", "text": "خ", "start_ms": 500, "end_ms": 501}],
            words=[_word("خ", 500, 501)],
        ),
        [
            DiarizationTurn(0, 500, "SPEAKER_00"),
            DiarizationTurn(500, 501, "SPEAKER_01"),
            DiarizationTurn(501, 1000, "SPEAKER_00"),
        ],
        diarization_model="model",
    )
    assert len(aligned["words"]) == 1
    assert aligned["words"][0]["speaker_id"] == "SPEAKER_01"


def test_word_alignment_never_drops_words_across_a_realistic_multi_turn_transcript():
    words = [
        _word("سلام", 0, 300),
        _word("دوست", 400, 700),
        _word("خوبی", 800, 1100),
        _word("؟", None, None),
        _word("بله", 1600, 1900),
        _word("ممنون", 2000, 2300),
    ]
    aligned = align_transcript_to_speakers(
        _transcript(
            [{"id": "a", "text": "سلام دوست خوبی؟ بله ممنون", "start_ms": 0, "end_ms": 2300}],
            words=words,
        ),
        [
            DiarizationTurn(0, 1200, "SPEAKER_00"),
            DiarizationTurn(1500, 2400, "SPEAKER_01"),
        ],
        diarization_model="model",
    )
    assert len(aligned["words"]) == len(words)
    assert [w["text"] for w in aligned["words"]] == [w["text"] for w in words]
    assert sum(len(segment["words"]) for segment in aligned["segments"]) == len(words)
