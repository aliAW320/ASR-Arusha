import asyncio
import json
import uuid

import httpx
import pytest

from app.config import Settings
from cleaner.chunking import (
    CleanerChunk,
    build_cleaner_chunks,
    dynamic_overlap_segments,
)
from cleaner.prompt import SYSTEM_PROMPT
from cleaner.provider import CleanerError, CleanerResponse, OpenAICompatibleCleanerProvider
from cleaner.worker import CleanerWorker, WorkItem


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


def test_chunking_ignores_word_timestamps_that_are_not_sent_to_cleaner():
    lean = [_segment(index, text_size=120) for index in range(6)]
    verbose = [
        {
            **segment,
            "words": [
                {"word": "نمونه", "start": item / 10, "end": (item + 1) / 10}
                for item in range(500)
            ],
        }
        for segment in lean
    ]

    lean_chunks = build_cleaner_chunks(
        lean, max_chars=500, overlap_min_segments=1, overlap_max_segments=2
    )
    verbose_chunks = build_cleaner_chunks(
        verbose, max_chars=500, overlap_min_segments=1, overlap_max_segments=2
    )

    assert [chunk.core_ids for chunk in verbose_chunks] == [
        chunk.core_ids for chunk in lean_chunks
    ]


def _sse_response(content_chunks: list[str], *, headers: dict | None = None) -> httpx.Response:
    """Build a fake OpenAI-compatible streaming chat-completion body.

    The real provider now always sends stream=true (see provider.py's clean()
    docstring for why: the LLM gateway's reverse proxy kills long-running
    non-streaming requests with a 504 before a realistic-size cleaner chunk
    ever finishes generating), so provider tests mock the streamed SSE
    contract instead of a single JSON body.
    """
    body = "".join(
        f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]}, ensure_ascii=False)}\n\n"
        for piece in content_chunks
    )
    body += "data: [DONE]\n\n"
    return httpx.Response(200, headers=headers or {}, content=body.encode("utf-8"))


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
        return _sse_response(
            [json.dumps(cleaned, ensure_ascii=False)],
            headers={"X-Request-ID": "cleaner-request"},
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
    assert captured["payload"]["chat_template_kwargs"] == {
        "enable_thinking": False
    }
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert response.segments[0]["text"] == "متن اصلاح‌شده"
    assert response.segments[0]["speaker_id"] == source[0]["speaker_id"]
    assert response.external_request_id == "cleaner-request"


@pytest.mark.asyncio
async def test_cleaner_worker_limits_parallel_chunk_requests_to_configured_concurrency():
    class TrackingProvider:
        def __init__(self):
            self.active = 0
            self.peak = 0

        async def clean(self, segments, *, model):
            assert model == "cleaner-model"
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.02)
            self.active -= 1
            return CleanerResponse(segments=segments)

    settings = Settings(
        _env_file=None,
        cleaner_max_concurrency=2,
        processing_cancellation_poll_interval_seconds=10,
    )
    worker = CleanerWorker(
        session_factory=None,
        storage=None,
        settings=settings,
    )
    item = WorkItem(
        attempt_id=uuid.uuid4(),
        attempt_number=1,
        job_id=uuid.uuid4(),
        result_id=uuid.uuid4(),
        voice_id=uuid.uuid4(),
        meeting_id=None,
        transcript_bucket="bucket",
        transcript_key="key",
        model_name="cleaner-model",
        base_url="https://llm.example/v1",
    )
    chunks = [
        CleanerChunk([_segment(index)], frozenset({str(index)}))
        for index in range(5)
    ]
    provider = TrackingProvider()

    cleaned, _ = await worker._clean_chunks(chunks, provider, item)

    assert provider.peak == 2
    assert [next(iter(chunk.core_ids)) for chunk, _ in cleaned] == [
        str(index) for index in range(5)
    ]


@pytest.mark.asyncio
async def test_cleaner_provider_streams_the_request_to_survive_the_gateways_idle_timeout():
    # Regression test for a real production 504: the LLM gateway's reverse
    # proxy killed non-streaming requests after ~90s of silence, which any
    # realistic-size (100+ segment) chunk reliably exceeded. Reproduced
    # directly against the live endpoint and confirmed streaming fixes it
    # (200 in 164.8s vs. 504 at 91.7s for an identical payload) before this
    # test was written. This asserts stream=true is actually sent and that a
    # response arriving as many small SSE chunks -- not one JSON blob -- is
    # still assembled and parsed correctly.
    source = [_segment(0)]
    captured = {}

    async def handler(request: httpx.Request):
        captured["payload"] = json.loads(await request.aread())
        cleaned = json.dumps(
            {"segments": [{**source[0], "text": "متن جریانی"}]}, ensure_ascii=False
        )
        # Split the JSON body across many tiny SSE events, one character (or
        # a few) at a time, mirroring real token-by-token streaming.
        pieces = [cleaned[i : i + 3] for i in range(0, len(cleaned), 3)]
        return _sse_response(pieces)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleCleanerProvider(
            base_url="https://llm.example/v1", api_key="secret", client=client
        )
        response = await provider.clean(source, model="model")

    assert captured["payload"]["stream"] is True
    assert response.segments[0]["text"] == "متن جریانی"


@pytest.mark.asyncio
async def test_cleaner_provider_rejects_changed_speaker_metadata():
    source = [_segment(0)]

    async def handler(_request: httpx.Request):
        changed = {"segments": [{**source[0], "speaker_id": "SPEAKER_99"}]}
        return _sse_response([json.dumps(changed)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleCleanerProvider(
            base_url="https://llm.example/v1",
            api_key="secret",
            client=client,
        )
        with pytest.raises(CleanerError) as raised:
            await provider.clean(source, model="model")

    assert raised.value.code == "cleaner_structure_changed"


@pytest.mark.asyncio
async def test_cleaner_provider_surfaces_gateway_504_as_a_retryable_error():
    async def handler(_request: httpx.Request):
        return httpx.Response(
            504,
            content=(
                b"<html><head><title>504 Gateway Time-out</title></head>"
                b"<body><center><h1>504 Gateway Time-out</h1></center>"
                b"<hr><center>openresty</center></body></html>"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleCleanerProvider(
            base_url="https://llm.example/v1", api_key="secret", client=client
        )
        with pytest.raises(CleanerError) as raised:
            await provider.clean([_segment(0)], model="model")

    assert raised.value.code == "cleaner_http_504"
    assert raised.value.retryable is True
