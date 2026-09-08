import json
from dataclasses import dataclass

import httpx

from .prompt import SUMMARY_SYSTEM_PROMPT


class SummaryError(Exception):
    def __init__(self, message: str, *, code: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class SummaryImage:
    name: str
    content_type: str
    data_url: str
    # True for a page image rendered from a PDF attachment. These exist only
    # to give the LLM visual context and must never be uploaded to the KB
    # (see mcp_worker.worker._publish, which filters them out before upload).
    is_pdf_page: bool = False


@dataclass(frozen=True)
class SummaryResponse:
    text: str
    external_request_id: str | None
    vision_used: bool


class OpenAICompatibleSummaryProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: int,
        temperature: float,
        max_tokens: int,
        client: httpx.AsyncClient | None = None,
    ):
        self.endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
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
    def _delta_content(event: dict) -> str | None:
        choices = event.get("choices") or [{}]
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        return content if isinstance(content, str) else None

    async def _request(
        self,
        *,
        transcript: str,
        images: list[SummaryImage],
        model: str,
    ) -> tuple[str, str | None]:
        user_content: str | list[dict]
        instruction = (
            "خلاصه جلسه زیر را تولید کن. برچسب و ترتیب گویندگان را در تحلیل حفظ کن.\n\n"
            + transcript
        )
        if images:
            user_content = [{"type": "text", "text": instruction}]
            user_content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": image.data_url},
                }
                for image in images
            )
        else:
            user_content = instruction
        payload = {
            "model": model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "stream": True,
            "messages": [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        parts: list[str] = []
        request_id = None
        try:
            async with self.client.stream(
                "POST",
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    body = response.text[:1000]
                    error = SummaryError(
                        f"Summary API returned HTTP {response.status_code}: {body}",
                        code=f"summary_http_{response.status_code}",
                        retryable=response.status_code == 429
                        or response.status_code >= 500,
                    )
                    error.response_body = body
                    error.status_code = response.status_code
                    raise error
                request_id = response.headers.get("x-request-id")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        piece = self._delta_content(json.loads(data))
                    except ValueError:
                        continue
                    if piece:
                        parts.append(piece)
        except httpx.TimeoutException as error:
            raise SummaryError(
                "Summary request timed out", code="summary_timeout", retryable=True
            ) from error
        except httpx.TransportError as error:
            raise SummaryError(
                "Summary network request failed",
                code="summary_network_error",
                retryable=True,
            ) from error
        text = "".join(parts).strip()
        if not text:
            raise SummaryError(
                "Summary API returned an empty response",
                code="summary_empty_response",
                retryable=True,
            )
        return text, request_id

    @staticmethod
    def _vision_is_unsupported(error: SummaryError) -> bool:
        body = str(getattr(error, "response_body", "")).lower()
        return getattr(error, "status_code", None) in {400, 415, 422} and any(
            marker in body
            for marker in ("image", "vision", "multimodal", "image_url")
        )

    async def summarize(
        self,
        *,
        transcript: str,
        images: list[SummaryImage],
        model: str,
    ) -> SummaryResponse:
        try:
            text, request_id = await self._request(
                transcript=transcript, images=images, model=model
            )
            return SummaryResponse(text, request_id, bool(images))
        except SummaryError as error:
            if not images or not self._vision_is_unsupported(error):
                raise
            text, request_id = await self._request(
                transcript=transcript, images=[], model=model
            )
            return SummaryResponse(text, request_id, False)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
