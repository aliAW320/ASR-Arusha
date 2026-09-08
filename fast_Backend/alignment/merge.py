from typing import Any

from .types import DiarizationError, DiarizationTurn


# Label used when a run deliberately had no diarization. Kept distinct from the
# SPEAKER_00/SPEAKER_01 labels a real diarizer emits, so "we did not measure
# who spoke" is never mistaken for "one speaker was measured".
UNKNOWN_SPEAKER = "SPEAKER_UNKNOWN"


def _validated_segments(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    source_segments = transcript.get("segments") or []
    if transcript.get("text", "").strip() and not source_segments:
        raise DiarizationError(
            "ASR response did not include timed segments required for speaker alignment",
            code="missing_segment_timestamps",
            retryable=False,
        )

    segments = []
    for index, segment in enumerate(source_segments):
        start_ms = segment.get("start_ms")
        end_ms = segment.get("end_ms")
        if start_ms is None or end_ms is None or end_ms < start_ms:
            raise DiarizationError(
                f"ASR segment timestamp at index {index} is missing or invalid",
                code="invalid_segment_timestamps",
                retryable=False,
            )
        segments.append(
            {
                "id": str(segment.get("id", index)),
                "start_ms": int(start_ms),
                "end_ms": int(end_ms),
                "text": str(segment.get("text") or ""),
            }
        )
    return segments


def _overlap_ms(start_ms: int, end_ms: int, turn: DiarizationTurn) -> int:
    return max(0, min(end_ms, turn.end_ms) - max(start_ms, turn.start_ms))


def _distance_ms(start_ms: int, end_ms: int, turn: DiarizationTurn) -> int:
    if turn.end_ms < start_ms:
        return start_ms - turn.end_ms
    if end_ms < turn.start_ms:
        return turn.start_ms - end_ms
    return 0


def _best_speaker(start_ms: int, end_ms: int, turns: list[DiarizationTurn]) -> str:
    """Greatest-overlap speaker for [start_ms, end_ms); nearest interval if none overlap.

    Shared by both the word-level and legacy segment-level alignment paths so
    the tie-break rules (most overlap, then earliest turn, then label) stay
    identical everywhere a timed interval needs a speaker.
    """
    overlap_by_speaker: dict[str, int] = {}
    first_turn_by_speaker: dict[str, int] = {}
    for turn in turns:
        overlap = _overlap_ms(start_ms, end_ms, turn)
        if overlap <= 0:
            continue
        overlap_by_speaker[turn.speaker_id] = overlap_by_speaker.get(turn.speaker_id, 0) + overlap
        first_turn_by_speaker.setdefault(turn.speaker_id, turn.start_ms)
    if overlap_by_speaker:
        return min(
            overlap_by_speaker,
            key=lambda speaker_id: (
                -overlap_by_speaker[speaker_id],
                first_turn_by_speaker[speaker_id],
                speaker_id,
            ),
        )
    nearest = min(
        turns,
        key=lambda turn: (_distance_ms(start_ms, end_ms, turn), turn.start_ms, turn.end_ms),
    )
    return nearest.speaker_id


def _matching_speakers(segment: dict[str, Any], turns: list[DiarizationTurn]) -> list[str]:
    return [_best_speaker(segment["start_ms"], segment["end_ms"], turns)]


def _has_valid_timestamp(word: dict[str, Any]) -> bool:
    start_ms = word.get("start_ms")
    end_ms = word.get("end_ms")
    return start_ms is not None and end_ms is not None and end_ms >= start_ms


def _resolve_word_speakers(
    words: list[dict[str, Any]], turns: list[DiarizationTurn]
) -> list[str]:
    """One speaker label per word, in order. Never leaves a word unresolved.

    Words with a valid [start_ms, end_ms) get the greatest-overlap (else
    nearest) speaker. A word with a missing/invalid timestamp inherits the
    nearest timestamped neighbor's speaker (preferring the previous word, so
    a mid-sentence gap stays attached to whoever was already talking); if no
    word in the whole transcript has a usable timestamp, every word falls
    back to the earliest diarization turn so the merge still produces a
    single, deterministic result instead of dropping anything.
    """
    resolved: list[str | None] = [
        _best_speaker(word["start_ms"], word["end_ms"], turns) if _has_valid_timestamp(word) else None
        for word in words
    ]

    last_seen: str | None = None
    for index, speaker_id in enumerate(resolved):
        if speaker_id is not None:
            last_seen = speaker_id
        elif last_seen is not None:
            resolved[index] = last_seen

    next_seen: str | None = None
    for index in range(len(resolved) - 1, -1, -1):
        if resolved[index] is not None:
            next_seen = resolved[index]
        elif next_seen is not None:
            resolved[index] = next_seen

    if turns and any(speaker_id is None for speaker_id in resolved):
        fallback = min(turns, key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker_id)).speaker_id
        resolved = [speaker_id or fallback for speaker_id in resolved]

    return resolved


def _group_words_into_turns(
    words: list[dict[str, Any]], speaker_ids: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group consecutive same-speaker words into turns.

    Returns (turns, flat_words) -- flat_words preserves the exact input
    order with each word's resolved speaker attached, independent of
    grouping; turns are the reconstructed speaker segments used for display.
    """
    flat_words: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for word, speaker_id in zip(words, speaker_ids):
        word_out = {
            "text": str(word.get("text") or ""),
            "start_ms": word.get("start_ms"),
            "end_ms": word.get("end_ms"),
            "speaker_id": speaker_id,
        }
        flat_words.append(word_out)

        if current is None or current["speaker_id"] != speaker_id:
            current = {
                "id": str(len(turns)),
                "speaker_id": speaker_id,
                "speaker_ids": [speaker_id],
                "start_ms": word_out["start_ms"],
                "end_ms": word_out["end_ms"],
                "text": "",
                "words": [],
            }
            turns.append(current)

        current["words"].append(word_out)
        if current["start_ms"] is None:
            current["start_ms"] = word_out["start_ms"]
        if word_out["end_ms"] is not None:
            current["end_ms"] = word_out["end_ms"]

    for turn in turns:
        turn["text"] = " ".join(word["text"] for word in turn["words"] if word["text"])

    return turns, flat_words


def _align_words_to_speakers(
    transcript: dict[str, Any],
    words: list[dict[str, Any]],
    turns: list[DiarizationTurn],
    *,
    diarization_model: str,
) -> dict[str, Any]:
    if not turns:
        raise DiarizationError(
            "Pyannote did not detect any speaker turns for timed ASR words",
            code="no_speakers_detected",
            retryable=False,
        )

    speaker_ids = _resolve_word_speakers(words, turns)
    segments, flat_words = _group_words_into_turns(words, speaker_ids)

    return {
        "schema_version": "speaker-transcript/v1",
        "language": transcript.get("language", "fa"),
        "source_id": transcript.get("source_id"),
        "model": transcript.get("model"),
        "diarization_model": diarization_model,
        "text": transcript.get("text", ""),
        "words": flat_words,
        "segments": segments,
        "metrics": transcript.get("metrics") or {},
    }


def align_transcript_to_speakers(
    transcript: dict[str, Any],
    turns: list[DiarizationTurn],
    *,
    diarization_model: str,
) -> dict[str, Any]:
    """Assign each ASR word (or, lacking word timestamps, each ASR segment)
    to the speaker with the greatest temporal overlap, falling back to the
    nearest speaker interval when there is none.

    Word-level alignment is the primary path now that the ASR request always
    asks for `timestamp_granularities[]=word`. The segment-level path stays
    as a fallback for any canonical transcript that predates word timestamps
    or whose provider genuinely returned none.
    """
    words = transcript.get("words") or []
    if words:
        return _align_words_to_speakers(transcript, words, turns, diarization_model=diarization_model)

    source_segments = _validated_segments(transcript)
    if source_segments and not turns:
        raise DiarizationError(
            "Pyannote did not detect any speaker turns for timed ASR segments",
            code="no_speakers_detected",
            retryable=False,
        )

    segments = []
    for segment in source_segments:
        speaker_ids = _matching_speakers(segment, turns)
        segments.append(
            {
                **segment,
                "speaker_id": " + ".join(speaker_ids),
                "speaker_ids": speaker_ids,
                "words": [],
            }
        )

    return {
        "schema_version": "speaker-transcript/v1",
        "language": transcript.get("language", "fa"),
        "source_id": transcript.get("source_id"),
        "model": transcript.get("model"),
        "diarization_model": diarization_model,
        "text": transcript.get("text", ""),
        "words": [],
        "segments": segments,
        "metrics": transcript.get("metrics") or {},
    }


def build_unattributed_transcript(transcript: dict[str, Any]) -> dict[str, Any]:
    """Shape a plain ASR transcript into the same speaker-transcript/v1 slot,
    with every segment attributed to one explicit unknown speaker.

    Used for runs where diarization was declined or unavailable, so cleaning,
    meeting composition and publication keep consuming a single artifact type
    instead of branching on whether speakers were measured. Unlike the
    alignment path this never raises for missing timestamps: without
    diarization there is nothing to align against, so a segment with no
    timeline is still perfectly usable text and is simply given a
    zero-length one.
    """
    source_segments = transcript.get("segments") or []
    segments = []
    for index, segment in enumerate(source_segments):
        start_ms = segment.get("start_ms")
        end_ms = segment.get("end_ms")
        start_ms = int(start_ms) if start_ms is not None else 0
        end_ms = int(end_ms) if end_ms is not None else start_ms
        segments.append(
            {
                "id": str(segment.get("id", index)),
                "start_ms": start_ms,
                "end_ms": max(start_ms, end_ms),
                "speaker_id": UNKNOWN_SPEAKER,
                "speaker_ids": [UNKNOWN_SPEAKER],
                "text": str(segment.get("text") or ""),
                "words": [],
            }
        )

    words = [
        {**word, "speaker_id": UNKNOWN_SPEAKER}
        for word in (transcript.get("words") or [])
    ]
    return {
        "schema_version": "speaker-transcript/v1",
        "language": transcript.get("language", "fa"),
        "source_id": transcript.get("source_id"),
        "model": transcript.get("model"),
        "diarization_model": None,
        "text": transcript.get("text", ""),
        "words": words,
        "segments": segments,
        "metrics": transcript.get("metrics") or {},
    }
