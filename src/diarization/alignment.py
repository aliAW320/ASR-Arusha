from typing import Any

from .provider import DiarizationError, DiarizationTurn


def _validated_words(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    source_words = transcript.get("words") or []
    if not source_words:
        source_words = [
            word
            for segment in transcript.get("segments") or []
            for word in segment.get("words") or []
        ]
    if transcript.get("text", "").strip() and not source_words:
        raise DiarizationError(
            "ASR response did not include word timestamps required for speaker alignment",
            code="missing_word_timestamps",
            retryable=False,
        )

    words = []
    for index, word in enumerate(source_words):
        start_ms = word.get("start_ms")
        end_ms = word.get("end_ms")
        if start_ms is None or end_ms is None or end_ms < start_ms:
            raise DiarizationError(
                f"ASR word timestamp at index {index} is missing or invalid",
                code="invalid_word_timestamps",
                retryable=False,
            )
        words.append(
            {
                "text": str(word.get("text") or ""),
                "start_ms": int(start_ms),
                "end_ms": int(end_ms),
            }
        )
    return words


def _overlap_ms(word: dict[str, Any], turn: DiarizationTurn) -> int:
    return max(0, min(word["end_ms"], turn.end_ms) - max(word["start_ms"], turn.start_ms))


def align_transcript_to_speakers(
    transcript: dict[str, Any],
    turns: list[DiarizationTurn],
    *,
    diarization_model: str,
) -> dict[str, Any]:
    """Use Pyannote turns as primary segments and place each ASR word exactly once."""
    words = _validated_words(transcript)
    if words and not turns:
        raise DiarizationError(
            "Pyannote did not detect any speaker turns for timestamped ASR text",
            code="no_speakers_detected",
            retryable=False,
        )

    assigned: list[list[dict[str, Any]]] = [[] for _ in turns]
    for index, word in enumerate(words):
        overlaps = [_overlap_ms(word, turn) for turn in turns]
        best_overlap = max(overlaps, default=0)
        if best_overlap <= 0:
            midpoint = (word["start_ms"] + word["end_ms"]) / 2
            candidates = [
                turn_index
                for turn_index, turn in enumerate(turns)
                if turn.start_ms <= midpoint <= turn.end_ms
            ]
            if not candidates:
                raise DiarizationError(
                    f"ASR word timestamp at index {index} does not overlap any speaker turn",
                    code="unaligned_word_timestamp",
                    retryable=False,
                )
            selected = candidates[0]
        else:
            selected = overlaps.index(best_overlap)
        assigned[selected].append(word)

    segments = []
    for index, (turn, turn_words) in enumerate(zip(turns, assigned, strict=True)):
        segments.append(
            {
                "id": str(index),
                "start_ms": turn.start_ms,
                "end_ms": turn.end_ms,
                "speaker_id": turn.speaker_id,
                "text": " ".join(
                    word["text"].strip() for word in turn_words if word["text"].strip()
                ),
                "words": turn_words,
            }
        )

    return {
        "schema_version": "speaker-transcript/v1",
        "language": transcript.get("language", "fa"),
        "source_id": transcript.get("source_id"),
        "model": transcript.get("model"),
        "diarization_model": diarization_model,
        "text": transcript.get("text", ""),
        "words": words,
        "segments": segments,
        "metrics": transcript.get("metrics") or {},
    }
