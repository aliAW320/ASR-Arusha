from typing import Any

from .provider import TranscriptionResponse


def _milliseconds(value: Any) -> int | None:
    if value is None:
        return None
    return max(0, round(float(value) * 1000))


def canonical_transcript(
    response: TranscriptionResponse,
    *,
    source_id: str,
    model_name: str,
    inference_duration_seconds: float,
    audio_duration_seconds: float | None,
) -> dict[str, Any]:
    source_segments = response.segments or [{"id": 0, "text": response.text}]
    segments = []
    for index, segment in enumerate(source_segments):
        segments.append(
            {
                "id": str(segment.get("id", index)),
                "start_ms": _milliseconds(segment.get("start")),
                "end_ms": _milliseconds(segment.get("end")),
                "speaker_id": None,
                "text": str(segment.get("text") or ""),
                "words": [],
            }
        )
    rtf = (
        inference_duration_seconds / audio_duration_seconds
        if audio_duration_seconds and audio_duration_seconds > 0
        else None
    )
    return {
        "schema_version": "canonical-transcript/v1",
        "language": response.language,
        "source_id": source_id,
        "model": model_name,
        "text": response.text,
        "words": [],
        "segments": segments,
        "metrics": {
            "inference_duration_seconds": inference_duration_seconds,
            "audio_duration_seconds": audio_duration_seconds,
            "real_time_factor": rtf,
        },
    }
