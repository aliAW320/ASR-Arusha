from typing import Any

from .provider import DiarizationError, DiarizationTurn


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


def _overlap_ms(segment: dict[str, Any], turn: DiarizationTurn) -> int:
    return max(
        0,
        min(segment["end_ms"], turn.end_ms)
        - max(segment["start_ms"], turn.start_ms),
    )


def _distance_ms(segment: dict[str, Any], turn: DiarizationTurn) -> int:
    if turn.end_ms < segment["start_ms"]:
        return segment["start_ms"] - turn.end_ms
    if segment["end_ms"] < turn.start_ms:
        return turn.start_ms - segment["end_ms"]
    return 0


def _matching_speakers(
    segment: dict[str, Any], turns: list[DiarizationTurn]
) -> list[str]:
    overlap_by_speaker: dict[str, int] = {}
    first_turn_by_speaker: dict[str, int] = {}
    for turn in turns:
        overlap = _overlap_ms(segment, turn)
        if overlap <= 0:
            continue
        overlap_by_speaker[turn.speaker_id] = (
            overlap_by_speaker.get(turn.speaker_id, 0) + overlap
        )
        first_turn_by_speaker.setdefault(turn.speaker_id, turn.start_ms)
    if overlap_by_speaker:
        selected = min(
            overlap_by_speaker,
            key=lambda speaker_id: (
                -overlap_by_speaker[speaker_id],
                first_turn_by_speaker[speaker_id],
                speaker_id,
            ),
        )
        return [selected]
    nearest = min(
        turns,
        key=lambda turn: (_distance_ms(segment, turn), turn.start_ms, turn.end_ms),
    )
    return [nearest.speaker_id]


def align_transcript_to_speakers(
    transcript: dict[str, Any],
    turns: list[DiarizationTurn],
    *,
    diarization_model: str,
) -> dict[str, Any]:
    """Assign each timed ASR segment to every overlapping Pyannote speaker."""
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
