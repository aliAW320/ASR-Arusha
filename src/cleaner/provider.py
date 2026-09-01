import json
from dataclasses import dataclass
from typing import Any

import httpx

from .prompt import SYSTEM_PROMPT


class CleanerError(Exception):
    def __init__(self, message: str, *, code: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class CleanerResponse:
    segments: list[dict[str, Any]]
    external_request_id: str | None = None


class OpenAICompatibleCleanerProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: int = 900,
        temperature: float = 0.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self.api_key = api_key
        self.temperature = temperature
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

    @staticmethod
    def _model_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": str(segment["id"]),
                "start_ms": int(segment["start_ms"]),
                "end_ms": int(segment["end_ms"]),
                "speaker_id": str(segment.get("speaker_id") or ""),
                "speaker_ids": [
                    str(item) for item in segment.get("speaker_ids") or []
                ],
                "text": str(segment.get("text") or ""),
            }
            for segment in segments
        ]

    @staticmethod
    def _validate_output(
        source: list[dict[str, Any]], payload: Any
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
            raise CleanerError(
                "Cleaner response must be a JSON object containing segments",
                code="cleaner_invalid_response",
                retryable=True,
            )
        cleaned = payload["segments"]
        if len(cleaned) != len(source):
            raise CleanerError(
                "Cleaner changed the number of transcript segments",
                code="cleaner_structure_changed",
                retryable=True,
            )
        immutable = ("id", "start_ms", "end_ms", "speaker_id", "speaker_ids")
        validated = []
        for index, (before, after) in enumerate(zip(source, cleaned, strict=True)):
            if not isinstance(after, dict) or not isinstance(after.get("text"), str):
                raise CleanerError(
                    f"Cleaner segment at index {index} is invalid",
                    code="cleaner_invalid_response",
                    retryable=True,
                )
            if any(after.get(field) != before[field] for field in immutable):
                raise CleanerError(
                    f"Cleaner changed immutable metadata at segment index {index}",
                    code="cleaner_structure_changed",
                    retryable=True,
                )
            validated.append({**before, "text": after["text"]})
        return validated

    async def clean(
        self,
        segments: list[dict[str, Any]],
        *,
        model: str,
    ) -> CleanerResponse:
        source = self._model_segments(segments)
        try:
            response = await self.client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": model,
                    "temperature": self.temperature,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"segments": source},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    ],
                },
            )
        except httpx.TimeoutException as error:
            raise CleanerError(
                "Cleaner request timed out", code="cleaner_timeout", retryable=True
            ) from error
        except httpx.TransportError as error:
            raise CleanerError(
                "Cleaner network request failed",
                code="cleaner_network_error",
                retryable=True,
            ) from error

        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            code = (
                "cleaner_authentication_failed"
                if response.status_code in {401, 403}
                else f"cleaner_http_{response.status_code}"
            )
            raise CleanerError(
                f"Cleaner API returned HTTP {response.status_code}: {response.text[:500]}",
                code=code,
                retryable=retryable,
            )
        try:
            response_payload = response.json()
            content = response_payload["choices"][0]["message"]["content"]
            output_payload = json.loads(content)
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise CleanerError(
                "Cleaner API returned invalid chat completion JSON",
                code="cleaner_invalid_response",
                retryable=True,
            ) from error
        return CleanerResponse(
            segments=self._validate_output(source, output_payload),
            external_request_id=response.headers.get("x-request-id"),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
