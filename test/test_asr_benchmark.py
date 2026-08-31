import json
import os
from pathlib import Path

import pytest

from app.config import get_settings
from asr.normalization import normalize_persian
from asr.provider import OpenAICompatibleTranscriptionProvider, TranscriptionError


pytestmark = [
    pytest.mark.asr_benchmark,
    pytest.mark.skipif(
        os.getenv("RUN_ASR_BENCHMARK") != "1",
        reason="Set RUN_ASR_BENCHMARK=1 to run the live 50-sample ASR benchmark",
    ),
]

SAMPLES = Path(__file__).parent / "audio_test" / "first_50"


def _edit_distance(reference, hypothesis) -> int:
    previous = list(range(len(hypothesis) + 1))
    for reference_index, reference_item in enumerate(reference, start=1):
        current = [reference_index]
        for hypothesis_index, hypothesis_item in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[hypothesis_index] + 1,
                    previous[hypothesis_index - 1]
                    + (reference_item != hypothesis_item),
                )
            )
        previous = current
    return previous[-1]


def _corpus_error_rate(references, hypotheses, tokenizer) -> float:
    edits = 0
    reference_units = 0
    for reference, hypothesis in zip(references, hypotheses, strict=True):
        reference_tokens = tokenizer(normalize_persian(reference))
        hypothesis_tokens = tokenizer(normalize_persian(hypothesis))
        edits += _edit_distance(reference_tokens, hypothesis_tokens)
        reference_units += len(reference_tokens)
    if reference_units == 0:
        raise AssertionError("Benchmark references contain no comparable units")
    return edits / reference_units * 100


async def _transcribe_with_retries(provider, audio_path, model, max_attempts):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            with audio_path.open("rb") as audio:
                return await provider.transcribe(
                    audio,
                    filename=audio_path.name,
                    content_type="audio/wav",
                    model=model,
                )
        except TranscriptionError as error:
            last_error = error
            if not error.retryable or attempt == max_attempts:
                raise
    raise last_error  # pragma: no cover


@pytest.mark.asyncio
async def test_first_50_samples_meet_asr_quality_thresholds():
    settings = get_settings()
    if settings.transcript_api_key is None:
        pytest.fail("TRANSCRIPT_API_KEY must be configured for the live benchmark")
    metadata_path = SAMPLES / "metadata.jsonl"
    if not metadata_path.is_file():
        pytest.fail(f"Benchmark metadata is missing: {metadata_path}")
    metadata = [
        json.loads(line)
        for line in metadata_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(metadata) == 50, "Benchmark must run on exactly the first 50 samples"

    provider = OpenAICompatibleTranscriptionProvider(
        base_url=settings.base_url,
        api_key=settings.transcript_api_key.get_secret_value(),
        timeout_seconds=settings.asr_request_timeout_seconds,
    )
    hypotheses = []
    try:
        for sample in metadata:
            response = await _transcribe_with_retries(
                provider,
                SAMPLES / sample["filename"],
                settings.transcript_model_name,
                settings.asr_max_attempts,
            )
            hypotheses.append(response.text)
    finally:
        await provider.aclose()

    references = [sample["transcription"] for sample in metadata]
    wer = _corpus_error_rate(references, hypotheses, str.split)
    cer = _corpus_error_rate(references, hypotheses, list)
    report = {
        "sample_count": 50,
        "model": settings.transcript_model_name,
        "wer_percent": wer,
        "cer_percent": cer,
        "thresholds": {"wer_percent": 25, "cer_percent": 8},
    }
    (SAMPLES / "asr_benchmark_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    assert wer < 25, report
    assert cer < 8, report
