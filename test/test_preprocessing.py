import asyncio
import io
import wave

import pytest

from app.config import get_settings
from app.models import ResultArtifactType
from conftest import authorization, register_user
from preprocessing.audio import NormalizedAudio, normalize_audio
from preprocessing.worker import PreprocessingWorker


@pytest.mark.asyncio
async def test_ffmpeg_normalizes_audio_to_pcm_16khz_mono_wav(tmp_path):
    source = tmp_path / "stereo.wav"
    destination = tmp_path / "normalized.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        audio.writeframes(b"\x00\x00" * 2 * 4410)

    metadata = await normalize_audio(source, destination)

    with wave.open(str(destination), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getframerate() == 16000
        assert audio.getsampwidth() == 2
    assert 95 <= metadata.duration_ms <= 105
    assert metadata.size_bytes == destination.stat().st_size


@pytest.mark.asyncio
async def test_preprocessing_stores_one_normalized_artifact_then_queues_asr(
    client, session_factory
):
    owner = await register_user(client, "preprocess@example.com")
    print("registered")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Normalize"}
        )
    ).json()
    print("meeting")
    uploaded = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(owner),
        files={"upload": ("source.mp3", io.BytesIO(b"source-audio"), "audio/mpeg")},
    )
    assert uploaded.status_code == 201, uploaded.text
    print("uploaded")
    voice = uploaded.json()

    async def fake_normalizer(source, destination):
        assert source.read_bytes() == b"source-audio"
        destination.write_bytes(b"RIFF-normalized-16k-mono")
        return NormalizedAudio(duration_ms=1234, size_bytes=24)

    worker = PreprocessingWorker(
        session_factory=session_factory,
        storage=client.storage,
        settings=get_settings(),
        normalizer=fake_normalizer,
    )
    print("worker")
    assert await asyncio.wait_for(worker.run_once(), timeout=2) is True

    processing = await client.get(
        f"/meetings/{meeting['id']}/processing", headers=authorization(owner)
    )
    assert {job["stage"] for job in processing.json()} == {
        "preprocess",
        "transcription",
    }
    preprocess = next(job for job in processing.json() if job["stage"] == "preprocess")
    assert preprocess["status"] == "succeeded"

    results = await client.get(
        f"/voices/{voice['id']}/results", headers=authorization(owner)
    )
    normalized = next(
        artifact
        for artifact in results.json()[0]["artifacts"]
        if artifact["artifact_type"] == ResultArtifactType.NORMALIZED_AUDIO.value
    )
    assert normalized["content_type"] == "audio/wav"
    assert client.storage.objects[
        (normalized["minio_bucket"], normalized["minio_key"])
    ] == b"RIFF-normalized-16k-mono"

    voices = await client.get(
        f"/meetings/{meeting['id']}/voices", headers=authorization(owner)
    )
    stored = voices.json()[0]
    assert stored["duration_ms"] == 1234
    assert stored["codec"] == "pcm_s16le"
    assert stored["sample_rate_hz"] == 16000
    assert stored["channels"] == 1
