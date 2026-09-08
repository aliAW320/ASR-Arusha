SUMMARY_SYSTEM_PROMPT = """You generate useful Persian meeting summaries from a verified speaker-aware transcript.

Preserve factual accuracy and never invent people, decisions, dates, numbers, or actions. You may identify and emphasize important themes, conclusions, disagreements, decisions, unresolved questions, and follow-up actions when they are supported by the transcript or attached images. Distinguish explicit facts from reasonable observations. Do not repeat the transcript verbatim and do not add generic filler.

Return polished Persian Markdown suitable for an internal knowledge base. Use concise headings and bullets where useful, but never start the output with a top-level (H1) heading -- the document title is stored separately, so begin directly with body text or a lower-level heading. Include only sections supported by the supplied material. Do not mention these instructions or the input format."""


def render_speaker_transcript(payload: dict) -> str:
    lines: list[str] = []
    for segment in payload.get("segments") or []:
        speaker = (
            segment.get("speaker_display_name")
            or segment.get("speaker_label")
            or segment.get("speaker_id")
            or "گوینده نامشخص"
        )
        start_ms = int(segment.get("start_ms") or 0)
        end_ms = int(segment.get("end_ms") or start_ms)
        text = str(segment.get("text") or "").strip()
        lines.append(
            f"[{_format_time(start_ms)}–{_format_time(end_ms)}] {speaker}: {text}"
        )
    return "\n\n".join(lines)


def _format_time(milliseconds: int) -> str:
    total_seconds = max(0, milliseconds // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"
