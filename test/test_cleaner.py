import json

import httpx
import pytest

from cleaner.chunking import build_cleaner_chunks, dynamic_overlap_segments
from cleaner.provider import CleanerError, OpenAICompatibleCleanerProvider


def _segment(index: int, *, duration_ms: int = 5_000, text_size: int = 20):
    return {
        "id": str(index),
        "start_ms": index * duration_ms,
        "end_ms": (index + 1) * duration_ms,
        "speaker_id": f"SPEAKER_{index % 2:02d}",
        "speaker_ids": [f"SPEAKER_{index % 2:02d}"],
        "text": "م" * text_size,
        "words": [],
    }


def test_dynamic_overlap_is_clamped_by_env_style_bounds_from_segment_duration():
    assert dynamic_overlap_segments(
        [_segment(0, duration_ms=2_000)], minimum=1, maximum=5
    ) == 5
    assert dynamic_overlap_segments(
        [_segment(0, duration_ms=30_000)], minimum=1, maximum=5
    ) == 1


def test_chunking_adds_context_overlap_but_owns_each_core_segment_once():
    segments = [_segment(index, text_size=120) for index in range(8)]
    chunks = build_cleaner_chunks(
        segments,
        max_chars=500,
        overlap_min_segments=1,
        overlap_max_segments=2,
    )

    owned = [segment_id for chunk in chunks for segment_id in chunk.core_ids]
    assert sorted(owned) == [str(index) for index in range(8)]
    assert len(chunks) > 1
    assert set(item["id"] for item in chunks[0].segments) & set(
        item["id"] for item in chunks[1].segments
    )


@pytest.mark.asyncio
async def test_cleaner_provider_sends_chat_contract_and_accepts_text_only_changes():
    captured = {}
    source = [_segment(0)]

    async def handler(request: httpx.Request):
        captured["request"] = request
        payload = json.loads(await request.aread())
        captured["payload"] = payload
        cleaned = json.loads(payload["messages"][1]["content"])
        cleaned["segments"][0]["text"] = "متن اصلاح‌شده"
        return httpx.Response(
            200,
            headers={"X-Request-ID": "cleaner-request"},
            json={"choices": [{"message": {"content": json.dumps(cleaned, ensure_ascii=False)}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleCleanerProvider(
            base_url="https://llm.example/v1/",
            api_key="secret",
            temperature=0,
            client=client,
        )
        response = await provider.clean(source, model="openai/Qwen3.8-27B")

    assert str(captured["request"].url) == "https://llm.example/v1/chat/completions"
    assert captured["payload"]["model"] == "openai/Qwen3.8-27B"
    assert captured["payload"]["temperature"] == 0
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert response.segments[0]["text"] == "متن اصلاح‌شده"
    assert response.segments[0]["speaker_id"] == source[0]["speaker_id"]
    assert response.external_request_id == "cleaner-request"


@pytest.mark.asyncio
async def test_cleaner_provider_rejects_changed_speaker_metadata():
    source = [_segment(0)]

    async def handler(_request: httpx.Request):
        changed = {"segments": [{**source[0], "speaker_id": "SPEAKER_99"}]}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(changed)}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleCleanerProvider(
            base_url="https://llm.example/v1",
            api_key="secret",
            client=client,
        )
        with pytest.raises(CleanerError) as raised:
            await provider.clean(source, model="model")

    assert raised.value.code == "cleaner_structure_changed"
