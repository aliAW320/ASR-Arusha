import asyncio
import wave
from dataclasses import dataclass
from pathlib import Path


class AudioPreprocessingError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class NormalizedAudio:
    duration_ms: int
    size_bytes: int


async def normalize_audio(source: Path, destination: Path) -> NormalizedAudio:
    """Convert any FFmpeg-readable audio to PCM s16le, 16 kHz, mono WAV."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map_metadata",
        "-1",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(destination),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await process.communicate()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise AudioPreprocessingError(
            detail or "FFmpeg could not decode the uploaded audio",
            code="audio_decode_failed",
            retryable=False,
        )
    try:
        with wave.open(str(destination), "rb") as audio:
            if (
                audio.getnchannels() != 1
                or audio.getframerate() != 16000
                or audio.getsampwidth() != 2
            ):
                raise ValueError("unexpected WAV format")
            duration_ms = round(audio.getnframes() * 1000 / audio.getframerate())
    except (OSError, EOFError, wave.Error, ValueError) as error:
        raise AudioPreprocessingError(
            "FFmpeg produced an invalid normalized WAV",
            code="normalized_audio_invalid",
            retryable=True,
        ) from error
    return NormalizedAudio(duration_ms=max(duration_ms, 1), size_bytes=destination.stat().st_size)
