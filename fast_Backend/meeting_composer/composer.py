import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .schema import SCHEMA_VERSION, ComposerError, validate_meeting_transcript


# Bumped whenever the offset/fingerprint algorithm changes in a way that
# should invalidate previously-computed fingerprints (forces recomposition
# instead of silently reusing a MeetingResult computed under the old rules).
FINGERPRINT_POLICY_VERSION = "1"


@dataclass(frozen=True)
class SourceInput:
    """Everything the pure composer needs from one voice-level source.

    Callers (the coordinator, at queue time, and the worker, at compose time)
    resolve this from the database and downloaded artifacts; this module never
    touches a database or object store itself.
    """

    position: int
    voice_id: str
    result_id: str
    source_artifact_id: str
    source_artifact_type: str
    source_checksum_sha256: str
    original_filename: str | None
    voice_sequence_number: int | None
    duration_ms: int
    segments: list[dict[str, Any]]
    # Diarization label -> (meeting_speaker_id, display_name), mapped labels only.
    speaker_names: dict[str, tuple[str, str]]


def compute_offsets(durations_ms: list[int], *, gap_ms: int = 0) -> list[int]:
    """Cumulative meeting-relative start offset for each source, in order.

    The first source always starts at 0; every later source starts right
    after the previous one's window (duration) plus the configured gap.
    """
    offsets: list[int] = []
    running_total = 0
    for index, duration_ms in enumerate(durations_ms):
        if index > 0:
            running_total += gap_ms
        offsets.append(running_total)
        running_total += duration_ms
    return offsets


def compute_source_fingerprint(
    entries: list[dict[str, Any]], *, policy_version: str = FINGERPRINT_POLICY_VERSION
) -> str:
    """Deterministic SHA-256 fingerprint of a source selection.

    Used to detect an identical composition request (idempotency) regardless
    of the order entries were passed in -- only their content and each
    entry's own `position` field determine the result.
    """
    ordered = sorted(entries, key=lambda entry: entry["position"])
    canonical = json.dumps(
        {"policy_version": policy_version, "sources": ordered},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_speaker(
    label: str, *, position: int, result_id: str, speaker_names: dict[str, tuple[str, str]]
) -> tuple[str, str | None, str]:
    mapped = speaker_names.get(label)
    if mapped is not None:
        meeting_speaker_id, display_name = mapped
        return f"meeting_speaker:{meeting_speaker_id}", meeting_speaker_id, display_name
    return f"result:{result_id}:{label}", None, f"File {position} - {label}"


def compose_meeting_transcript(
    *,
    meeting_id: str,
    meeting_result_id: str,
    generated_at: str,
    sources: list[SourceInput],
    gap_ms: int = 0,
    max_segments: int | None = None,
) -> dict[str, Any]:
    """Deterministically build a meeting-transcript/v1 payload.

    Pure function: no database access, no object storage, no wall-clock reads
    (the caller supplies `generated_at`). Given the same sources it always
    produces byte-identical output.
    """
    ordered_sources = sorted(sources, key=lambda source: source.position)
    offsets = compute_offsets(
        [source.duration_ms for source in ordered_sources], gap_ms=gap_ms
    )

    sources_payload: list[dict[str, Any]] = []
    sortable_segments: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
    speakers_by_id: dict[str, dict[str, Any]] = {}
    total_segments = 0

    for source, offset_ms in zip(ordered_sources, offsets):
        sources_payload.append(
            {
                "position": source.position,
                "voice_id": source.voice_id,
                "result_id": source.result_id,
                "source_artifact_id": source.source_artifact_id,
                "source_artifact_type": source.source_artifact_type,
                "source_checksum_sha256": source.source_checksum_sha256,
                "original_filename": source.original_filename,
                "voice_sequence_number": source.voice_sequence_number,
                "offset_ms": offset_ms,
                "duration_ms": source.duration_ms,
            }
        )
        for index, segment in enumerate(source.segments):
            total_segments += 1
            if max_segments is not None and total_segments > max_segments:
                raise ComposerError(
                    f"Composition exceeds the configured segment limit "
                    f"({max_segments})",
                    code="composition_limit_exceeded",
                )
            meeting_start_ms = offset_ms + segment["start_ms"]
            meeting_end_ms = offset_ms + segment["end_ms"]
            speaker_id, meeting_speaker_id, display_name = _resolve_speaker(
                segment["label"],
                position=source.position,
                result_id=source.result_id,
                speaker_names=source.speaker_names,
            )
            speakers_by_id.setdefault(
                speaker_id,
                {
                    "speaker_id": speaker_id,
                    "speaker_label": segment["label"],
                    "meeting_speaker_id": meeting_speaker_id,
                    "speaker_display_name": display_name,
                },
            )
            composed_segment = {
                "id": f"{meeting_result_id}:{source.result_id}:{segment['id']}",
                "position": source.position,
                "source_result_id": source.result_id,
                "source_voice_id": source.voice_id,
                "source_segment_id": segment["id"],
                "source_start_ms": segment["start_ms"],
                "source_end_ms": segment["end_ms"],
                "start_ms": meeting_start_ms,
                "end_ms": meeting_end_ms,
                "speaker_id": speaker_id,
                "speaker_label": segment["label"],
                "meeting_speaker_id": meeting_speaker_id,
                "speaker_display_name": display_name,
                "text": segment["text"],
            }
            sort_key = (meeting_start_ms, meeting_end_ms, source.position, index)
            sortable_segments.append((sort_key, composed_segment))

    sortable_segments.sort(key=lambda item: item[0])
    segments = [segment for _, segment in sortable_segments]

    text_parts: list[str] = []
    current_position: int | None = None
    current_parts: list[str] = []
    for segment in segments:
        if segment["position"] != current_position:
            if current_parts:
                text_parts.append(" ".join(current_parts))
            current_position = segment["position"]
            current_parts = []
        if segment["text"].strip():
            current_parts.append(segment["text"])
    if current_parts:
        text_parts.append(" ".join(current_parts))

    duration_ms = (offsets[-1] + ordered_sources[-1].duration_ms) if ordered_sources else 0

    payload = {
        "schema": SCHEMA_VERSION,
        "meeting_id": meeting_id,
        "meeting_result_id": meeting_result_id,
        "generated_at": generated_at,
        "duration_ms": duration_ms,
        "source_count": len(sources_payload),
        "speaker_count": len(speakers_by_id),
        "text": "\n\n".join(text_parts),
        "sources": sources_payload,
        "speakers": list(speakers_by_id.values()),
        "segments": segments,
    }
    validate_meeting_transcript(payload)
    return payload
