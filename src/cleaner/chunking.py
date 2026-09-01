import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CleanerChunk:
    segments: list[dict[str, Any]]
    core_ids: frozenset[str]


def dynamic_overlap_segments(
    segments: list[dict[str, Any]],
    *,
    minimum: int,
    maximum: int,
) -> int:
    if minimum < 0 or maximum < minimum:
        raise ValueError("Cleaner overlap bounds are invalid")
    durations = [
        int(segment["end_ms"]) - int(segment["start_ms"])
        for segment in segments
        if segment.get("start_ms") is not None
        and segment.get("end_ms") is not None
        and int(segment["end_ms"]) > int(segment["start_ms"])
    ]
    if not durations:
        return minimum
    average_duration = sum(durations) / len(durations)
    desired = round(20_000 / average_duration)
    return max(minimum, min(maximum, desired))


def _segment_size(segment: dict[str, Any]) -> int:
    return len(json.dumps(segment, ensure_ascii=False, separators=(",", ":")))


def build_cleaner_chunks(
    segments: list[dict[str, Any]],
    *,
    max_chars: int,
    overlap_min_segments: int,
    overlap_max_segments: int,
) -> list[CleanerChunk]:
    if max_chars < 1:
        raise ValueError("Cleaner chunk size must be positive")
    if not segments:
        return []
    ids = [str(segment.get("id")) for segment in segments]
    if len(ids) != len(set(ids)):
        raise ValueError("Cleaner input segment IDs must be unique")

    overlap = dynamic_overlap_segments(
        segments,
        minimum=overlap_min_segments,
        maximum=overlap_max_segments,
    )
    core_ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(segments):
        end = start
        size = 0
        while end < len(segments):
            candidate_size = _segment_size(segments[end])
            if end > start and size + candidate_size > max_chars:
                break
            size += candidate_size
            end += 1
        core_ranges.append((start, end))
        start = end

    chunks = []
    for core_start, core_end in core_ranges:
        context_start = max(0, core_start - overlap)
        context_end = min(len(segments), core_end + overlap)
        chunks.append(
            CleanerChunk(
                segments=[dict(item) for item in segments[context_start:context_end]],
                core_ids=frozenset(ids[core_start:core_end]),
            )
        )
    return chunks


def merge_cleaned_chunks(
    original_segments: list[dict[str, Any]],
    cleaned_chunks: list[tuple[CleanerChunk, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    corrected_text: dict[str, str] = {}
    for chunk, cleaned_segments in cleaned_chunks:
        for segment in cleaned_segments:
            segment_id = str(segment["id"])
            if segment_id in chunk.core_ids:
                corrected_text[segment_id] = str(segment["text"])
    missing = [
        str(item["id"])
        for item in original_segments
        if str(item["id"]) not in corrected_text
    ]
    if missing:
        raise ValueError(f"Cleaner output is missing core segments: {', '.join(missing)}")
    return [
        {**segment, "text": corrected_text[str(segment["id"])]}
        for segment in original_segments
    ]
