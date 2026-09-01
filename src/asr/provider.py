from dataclasses import dataclass
from typing import Any, BinaryIO

import httpx


class TranscriptionError(Exception):
    def __init__(self, message: str, *, code: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class TranscriptionResponse:
    text: str
    language: str
    segments: list[dict[str, Any]]
    raw_response: dict[str, Any]
    external_request_id: str | None = None


class OpenAICompatibleTranscriptionProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: int = 600,
        num_beams: int = 5,
        client: httpx.AsyncClient | None = None,
    ):
        self.endpoint = f"{base_url.rstrip('/')}/audio/transcriptions"
        self.api_key = api_key
        self.num_beams = num_beams
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout_seconds,
                connect=30,
                read=timeout_seconds,
                write=timeout_seconds,
                pool=30,
            )
        )

    async def transcribe(
        self,
        audio: BinaryIO,
        *,
        filename: str,
        content_type: str,
        model: str,
    ) -> TranscriptionResponse:
        audio.seek(0)
        try:
            response = await self.client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                data={
                    "model": model,
                    "language": "fa",
                    "response_format": "verbose_json",
                    "extra_body[use_beam_search]": "true",
                    "extra_body[num_beams]": str(self.num_beams),
                },
                files={"file": (filename, audio, content_type)},
            )
        except httpx.TimeoutException as error:
            raise TranscriptionError(
                "ASR request timed out", code="asr_timeout", retryable=True
            ) from error
        except httpx.TransportError as error:
            raise TranscriptionError(
                "ASR network request failed", code="asr_network_error", retryable=True
            ) from error

        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            code = (
                "asr_authentication_failed"
                if response.status_code in {401, 403}
                else f"asr_http_{response.status_code}"
            )
            raise TranscriptionError(
                f"ASR API returned HTTP {response.status_code}: {response.text[:500]}",
                code=code,
                retryable=retryable,
            )

        try:
            payload = response.json()
        except ValueError as error:
            raise TranscriptionError(
                "ASR API returned invalid JSON",
                code="asr_invalid_response",
                retryable=False,
            ) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise TranscriptionError(
                "ASR API response does not contain text",
                code="asr_invalid_response",
                retryable=False,
            )
        segments = payload.get("segments")
        return TranscriptionResponse(
            text=payload["text"],
            language=str(payload.get("language") or "fa"),
            segments=segments if isinstance(segments, list) else [],
            raw_response=payload,
            external_request_id=response.headers.get("x-request-id"),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
