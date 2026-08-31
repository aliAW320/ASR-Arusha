import io
import json

import httpx
import pytest

from asr.canonical import canonical_transcript
from asr.normalization import normalize_persian
from asr.provider import (
    OpenAICompatibleTranscriptionProvider,
    TranscriptionError,
    TranscriptionResponse,
)


def test_normalize_persian_applies_the_approved_canonicalization_rules():
    assert normalize_persian("كِتاب\u200cهاي ۱۲،٣!\ufeff") == "کتاب های 12 3"
    assert normalize_persian("ۀ ئ ى ة") == "ه ی ی ه"


def test_canonical_transcript_preserves_segments_words_and_metrics():
    payload = canonical_transcript(
        TranscriptionResponse(
            text="سلام دنیا",
            language="fa",
            segments=[
                {
                    "id": 7,
                    "start": 0.5,
                    "end": 1.75,
                    "text": "سلام دنیا",
                    "words": [{"word": "سلام", "start": 0.5, "end": 0.9}],
                }
            ],
            raw_response={"text": "سلام دنیا"},
        ),
        source_id="voice-id",
        model_name="model-name",
        inference_duration_seconds=2.0,
        audio_duration_seconds=4.0,
    )

    assert payload["schema_version"] == "canonical-transcript/v1"
    assert payload["segments"][0]["start_ms"] == 500
    assert payload["segments"][0]["end_ms"] == 1750
    assert payload["segments"][0]["words"][0]["end_ms"] == 900
    assert payload["metrics"]["real_time_factor"] == 0.5


@pytest.mark.asyncio
async def test_openai_compatible_provider_sends_multipart_contract_and_parses_response():
    captured = {}

    async def handler(request: httpx.Request):
        captured["request"] = request
        captured["body"] = await request.aread()
        return httpx.Response(
            200,
            headers={"X-Request-ID": "remote-request"},
            json={
                "text": "متن پاسخ",
                "language": "fa",
                "segments": [{"id": 0, "start": 0, "end": 1, "text": "متن پاسخ"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleTranscriptionProvider(
            base_url="https://asr.example/v1/",
            api_key="secret-key",
            client=client,
        )
        response = await provider.transcribe(
            io.BytesIO(b"RIFF-audio"),
            filename="sample.wav",
            content_type="audio/wav",
            model="persian-model",
        )

    request = captured["request"]
    body = captured["body"]
    assert str(request.url) == "https://asr.example/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer secret-key"
    assert b'form-data; name="model"' in body and b"persian-model" in body
    assert b'form-data; name="extra_body[use_beam_search]"' in body
    assert b'form-data; name="extra_body[num_beams]"' in body
    assert b"num_beams" in body and b"10" in body
    assert b'filename="sample.wav"' in body
    assert response.text == "متن پاسخ"
    assert response.external_request_id == "remote-request"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "retryable", "code"),
    [(401, False, "asr_authentication_failed"), (429, True, "asr_http_429"), (503, True, "asr_http_503")],
)
async def test_provider_classifies_retryable_and_permanent_http_errors(
    status_code, retryable, code
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status_code, json={"error": "x"}))
    ) as client:
        provider = OpenAICompatibleTranscriptionProvider(
            base_url="https://asr.example/v1",
            api_key="secret-key",
            client=client,
        )
        with pytest.raises(TranscriptionError) as raised:
            await provider.transcribe(
                io.BytesIO(b"audio"),
                filename="sample.wav",
                content_type="audio/wav",
                model="model",
            )

    assert raised.value.retryable is retryable
    assert raised.value.code == code


@pytest.mark.asyncio
async def test_provider_classifies_transport_failure_as_retryable():
    async def handler(request: httpx.Request):
        raise httpx.RemoteProtocolError("connection closed", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleTranscriptionProvider(
            base_url="https://asr.example/v1",
            api_key="secret-key",
            client=client,
        )
        with pytest.raises(TranscriptionError) as raised:
            await provider.transcribe(
                io.BytesIO(b"audio"),
                filename="sample.wav",
                content_type="audio/wav",
                model="model",
            )

    assert raised.value.retryable is True
    assert raised.value.code == "asr_network_error"


def test_canonical_transcript_is_json_serializable_without_raw_provider_payload():
    response = TranscriptionResponse(
        text="متن",
        language="fa",
        segments=[],
        raw_response={"provider_only": object()},
    )
    payload = canonical_transcript(
        response,
        source_id="voice",
        model_name="model",
        inference_duration_seconds=1,
        audio_duration_seconds=None,
    )

    assert json.loads(json.dumps(payload, ensure_ascii=False))["text"] == "متن"
    assert "provider_only" not in payload
