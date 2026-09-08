from typing import Any


SCHEMA_VERSION = "meeting-transcript/v1"

_REQUIRED_TOP_LEVEL_FIELDS = (
    "schema",
    "meeting_id",
    "meeting_result_id",
    "generated_at",
    "duration_ms",
    "source_count",
    "speaker_count",
    "text",
    "sources",
    "speakers",
    "segments",
)

_REQUIRED_SEGMENT_FIELDS = (
    "id",
    "position",
    "source_result_id",
    "source_voice_id",
    "source_segment_id",
    "source_start_ms",
    "source_end_ms",
    "start_ms",
    "end_ms",
    "speaker_id",
    "speaker_label",
    "meeting_speaker_id",
    "speaker_display_name",
    "text",
)


class ComposerError(Exception):
    def __init__(self, message: str, *, code: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def parse_source_segments(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and normalize the `segments` array of a source transcript
    artifact (cleaned-speaker-transcript/v1 or speaker-transcript/v1).

    Returns a list of plain dicts: id, start_ms, end_ms, label, text.
    `label` is the segment's primary diarization label, preferring the first
    entry of `speaker_ids` and falling back to `speaker_id`.
    """
    raw_segments = transcript.get("segments")
    if not isinstance(raw_segments, list):
        raise ComposerError(
            "Source transcript artifact is missing a segments array",
            code="source_artifact_invalid",
        )

    segments: list[dict[str, Any]] = []
    for index, segment in enumerate(raw_segments):
        if not isinstance(segment, dict):
            raise ComposerError(
                f"Source segment at index {index} is not an object",
                code="source_artifact_invalid",
            )
        start_ms = segment.get("start_ms")
        end_ms = segment.get("end_ms")
        if (
            len(raw_segments) == 1
            and (start_ms is None or end_ms is None or int(end_ms) <= int(start_ms))
        ):
            timed_words = [
                word
                for word in (transcript.get("words") or [])
                if word.get("start_ms") is not None
                and word.get("end_ms") is not None
                and int(word["end_ms"]) > int(word["start_ms"])
            ]
            if timed_words:
                start_ms = min(int(word["start_ms"]) for word in timed_words)
                end_ms = max(int(word["end_ms"]) for word in timed_words)
        if start_ms is None or end_ms is None or int(end_ms) <= int(start_ms):
            raise ComposerError(
                f"Source segment at index {index} has a missing or invalid timeline",
                code="source_timeline_invalid",
            )
        speaker_ids = segment.get("speaker_ids") or []
        label = speaker_ids[0] if speaker_ids else segment.get("speaker_id")
        if not label:
            raise ComposerError(
                f"Source segment at index {index} has no speaker label",
                code="source_timeline_invalid",
            )
        segments.append(
            {
                "id": str(segment.get("id", index)),
                "start_ms": int(start_ms),
                "end_ms": int(end_ms),
                "label": str(label),
                "text": str(segment.get("text") or ""),
            }
        )
    return segments


def validate_meeting_transcript(payload: dict[str, Any]) -> None:
    """Structural self-check of an assembled meeting-transcript/v1 payload.

    Defense in depth: compose_meeting_transcript() already builds a
    conforming payload, but this lets the worker (and tests) re-verify the
    contract right before upload without duplicating the rules.
    """
    missing = [field for field in _REQUIRED_TOP_LEVEL_FIELDS if field not in payload]
    if missing:
        raise ComposerError(
            f"Meeting transcript payload is missing fields: {', '.join(missing)}",
            code="schema_invalid",
        )
    if payload["schema"] != SCHEMA_VERSION:
        raise ComposerError(
            f"Unexpected schema version: {payload['schema']!r}",
            code="schema_invalid",
        )
    if not isinstance(payload["duration_ms"], int) or payload["duration_ms"] < 0:
        raise ComposerError(
            "Meeting transcript duration_ms must be a non-negative integer",
            code="schema_invalid",
        )
    if payload["source_count"] != len(payload["sources"]):
        raise ComposerError(
            "source_count does not match the number of sources",
            code="schema_invalid",
        )

    previous_start = -1
    for index, segment in enumerate(payload["segments"]):
        missing_segment_fields = [
            field for field in _REQUIRED_SEGMENT_FIELDS if field not in segment
        ]
        if missing_segment_fields:
            raise ComposerError(
                f"Segment at index {index} is missing fields: "
                f"{', '.join(missing_segment_fields)}",
                code="schema_invalid",
            )
        if segment["start_ms"] < 0 or segment["end_ms"] < segment["start_ms"]:
            raise ComposerError(
                f"Segment at index {index} has an invalid meeting timeline",
                code="schema_invalid",
            )
        if segment["start_ms"] < previous_start:
            raise ComposerError(
                "Segments are not sorted by meeting start time",
                code="schema_invalid",
            )
        previous_start = segment["start_ms"]
        if (segment["meeting_speaker_id"] is None) == segment["speaker_id"].startswith(
            "meeting_speaker:"
        ):
            raise ComposerError(
                f"Segment at index {index} has an inconsistent speaker mapping",
                code="schema_invalid",
            )
