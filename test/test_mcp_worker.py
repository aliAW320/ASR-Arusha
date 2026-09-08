import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import pymupdf
import pytest
from sqlalchemy import select

from pydantic import SecretStr

from app.config import get_settings
from app.models import (
    MeetingArtifactType,
    MeetingPublication,
    MeetingPublicationStatus,
    MeetingResult,
    MeetingResultArtifact,
    ProcessingAttempt,
    ProcessingAttemptStatus,
    ProcessingJob,
    ProcessingStage,
    PublicationVisionStatus,
)
from conftest import authorization, register_user

import mcp_worker.worker as mcp_worker_module
from mcp_worker.outline import OutlineDestination, OutlineMCPClient, OutlineMCPError
from mcp_worker.summary import (
    OpenAICompatibleSummaryProvider,
    SummaryError,
    SummaryImage,
    SummaryResponse,
)
from mcp_worker.worker import MCPWorker


# ---------------------------------------------------------------------------
# OutlineMCPClient -- real HTTP layer, mocked transport
# ---------------------------------------------------------------------------


def _rpc_result(payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": payload}


def _tool_ok(structured: object = None, content: list | None = None) -> dict:
    return _rpc_result(
        {"isError": False, "structuredContent": structured, "content": content or []}
    )


@pytest.mark.asyncio
async def test_resolve_destination_returns_collection_only_for_a_single_segment_path():
    async def handler(request: httpx.Request):
        body = json.loads(request.content)
        assert body["params"]["name"] == "list_collections"
        return httpx.Response(
            200,
            json=_tool_ok([{"id": "col-1", "name": "پروژه‌های کارآموزی"}]),
        )

    client = OutlineMCPClient(
        base_url="https://kb.example",
        api_key="secret",
        timeout_seconds=30,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    destination = await client.resolve_destination("پروژه‌های کارآموزی")
    assert destination == OutlineDestination("col-1", None, None)
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_destination_walks_nested_document_titles():
    calls = []

    async def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body["params"]["name"])
        if body["params"]["name"] == "list_collections":
            return httpx.Response(
                200, json=_tool_ok([{"id": "col-1", "name": "پروژه‌های کارآموزی"}])
            )
        return httpx.Response(
            200,
            json=_tool_ok(
                [{"id": "doc-1", "title": "ASR test", "children": [{"id": "doc-2", "title": "خلاصه"}]}]
            ),
        )

    client = OutlineMCPClient(
        base_url="https://kb.example",
        api_key="secret",
        timeout_seconds=30,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    destination = await client.resolve_destination("پروژه‌های کارآموزی/ASR test")
    assert calls == ["list_collections", "list_collection_documents"]
    assert destination.collection_id == "col-1"
    assert destination.parent_document_id == "doc-1"
    assert OutlineMCPClient.find_child(destination.node, "خلاصه") == "doc-2"
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_destination_fails_clearly_when_a_path_component_is_missing():
    async def handler(request: httpx.Request):
        return httpx.Response(200, json=_tool_ok([{"id": "col-1", "name": "Other"}]))

    client = OutlineMCPClient(
        base_url="https://kb.example",
        api_key="secret",
        timeout_seconds=30,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(OutlineMCPError) as raised:
        await client.resolve_destination("پروژه‌های کارآموزی/ASR test")
    assert raised.value.code == "destination_not_found"
    assert raised.value.retryable is False
    await client.aclose()


@pytest.mark.asyncio
async def test_upsert_document_creates_without_id_and_updates_with_id():
    captured = []

    async def handler(request: httpx.Request):
        body = json.loads(request.content)
        captured.append(body["params"])
        if body["params"]["name"] == "create_document":
            return httpx.Response(
                200, json=_tool_ok({"id": "doc-new", "title": "خلاصه", "url": "/doc/doc-new"})
            )
        return httpx.Response(
            200, json=_tool_ok({"id": "doc-existing", "title": "خلاصه", "url": "/doc/doc-existing"})
        )

    client = OutlineMCPClient(
        base_url="https://kb.example",
        api_key="secret",
        timeout_seconds=30,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    created_id = await client.upsert_document(
        document_id=None, title="خلاصه", text="متن", parent_document_id="doc-parent"
    )
    updated_id = await client.upsert_document(
        document_id="doc-existing", title="خلاصه", text="متن جدید"
    )
    assert created_id == "doc-new"
    assert updated_id == "doc-existing"
    assert captured[0]["name"] == "create_document"
    assert captured[0]["arguments"]["parentDocumentId"] == "doc-parent"
    assert captured[1]["name"] == "update_document"
    assert captured[1]["arguments"]["id"] == "doc-existing"
    await client.aclose()


@pytest.mark.asyncio
async def test_upload_attachment_posts_multipart_when_outline_returns_form_fields():
    async def handler(request: httpx.Request):
        if str(request.url) == "https://kb.example/mcp":
            return httpx.Response(
                200,
                json=_tool_ok(
                    {
                        "uploadUrl": "https://upload.example/put",
                        "form": {"key": "meetings/x.png"},
                        "attachment": {"id": "att-1", "url": "https://kb.example/attachments/att-1"},
                    }
                ),
            )
        assert str(request.url) == "https://upload.example/put"
        assert b"meetings/x.png" in request.content
        return httpx.Response(204)

    client = OutlineMCPClient(
        base_url="https://kb.example",
        api_key="secret",
        timeout_seconds=30,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    url = await client.upload_attachment(
        name="diagram.png", content_type="image/png", content=b"\x89PNGdata"
    )
    assert url == "https://kb.example/attachments/att-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_upload_attachment_puts_raw_bytes_when_outline_returns_no_form():
    async def handler(request: httpx.Request):
        if str(request.url) == "https://kb.example/mcp":
            return httpx.Response(
                200,
                json=_tool_ok(
                    {
                        "uploadUrl": "https://upload.example/put",
                        "attachment": {"id": "att-2", "url": "https://kb.example/attachments/att-2"},
                    }
                ),
            )
        assert request.method == "PUT"
        assert request.content == b"\x89PNGdata"
        return httpx.Response(200)

    client = OutlineMCPClient(
        base_url="https://kb.example",
        api_key="secret",
        timeout_seconds=30,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    url = await client.upload_attachment(
        name="diagram.png", content_type="image/png", content=b"\x89PNGdata"
    )
    assert url == "https://kb.example/attachments/att-2"
    await client.aclose()


@pytest.mark.asyncio
async def test_call_tool_raises_when_outline_reports_a_tool_error():
    async def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json=_rpc_result(
                {"isError": True, "content": [{"type": "text", "text": "collection missing"}]}
            ),
        )

    client = OutlineMCPClient(
        base_url="https://kb.example",
        api_key="secret",
        timeout_seconds=30,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(OutlineMCPError) as raised:
        await client.call_tool("list_collections", {})
    assert raised.value.retryable is False
    assert "collection missing" in str(raised.value)
    await client.aclose()


# ---------------------------------------------------------------------------
# OpenAICompatibleSummaryProvider -- SSE contract + vision fallback
# ---------------------------------------------------------------------------


def _sse_response(content_chunks: list[str], *, status_code: int = 200, headers=None) -> httpx.Response:
    if status_code >= 400:
        return httpx.Response(status_code, headers=headers or {}, content="".join(content_chunks).encode())
    body = "".join(
        f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n"
        for piece in content_chunks
    )
    body += "data: [DONE]\n\n"
    return httpx.Response(200, headers=headers or {}, content=body.encode("utf-8"))


@pytest.mark.asyncio
async def test_summary_provider_sends_text_only_content_when_no_images():
    captured = {}

    async def handler(request: httpx.Request):
        captured["payload"] = json.loads(request.content)
        return _sse_response([json.dumps({"ok": True})])

    provider = OpenAICompatibleSummaryProvider(
        base_url="https://llm.example/v1",
        api_key="secret",
        timeout_seconds=30,
        temperature=0.2,
        max_tokens=1024,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    response = await provider.summarize(transcript="متن جلسه", images=[], model="m")
    assert isinstance(captured["payload"]["messages"][1]["content"], str)
    assert response.vision_used is False
    await provider.aclose()


@pytest.mark.asyncio
async def test_summary_provider_sends_image_url_blocks_when_images_present():
    captured = {}
    image = SummaryImage(name="a.png", content_type="image/png", data_url="data:image/png;base64,AAAA")

    async def handler(request: httpx.Request):
        captured["payload"] = json.loads(request.content)
        return _sse_response(["خلاصه"])

    provider = OpenAICompatibleSummaryProvider(
        base_url="https://llm.example/v1",
        api_key="secret",
        timeout_seconds=30,
        temperature=0.2,
        max_tokens=1024,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    response = await provider.summarize(transcript="متن جلسه", images=[image], model="m")
    content = captured["payload"]["messages"][1]["content"]
    assert content[0]["type"] == "text"
    assert content[1] == {"type": "image_url", "image_url": {"url": image.data_url}}
    assert response.vision_used is True
    assert response.text == "خلاصه"
    await provider.aclose()


@pytest.mark.asyncio
async def test_summary_provider_falls_back_to_text_only_when_vision_is_unsupported():
    image = SummaryImage(name="a.png", content_type="image/png", data_url="data:image/png;base64,AAAA")
    calls = []

    async def handler(request: httpx.Request):
        payload = json.loads(request.content)
        has_images = isinstance(payload["messages"][1]["content"], list)
        calls.append(has_images)
        if has_images:
            return httpx.Response(
                415, content=b'{"error":"image input is not supported by this model"}'
            )
        return _sse_response(["فقط متن"])

    provider = OpenAICompatibleSummaryProvider(
        base_url="https://llm.example/v1",
        api_key="secret",
        timeout_seconds=30,
        temperature=0.2,
        max_tokens=1024,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    response = await provider.summarize(transcript="متن جلسه", images=[image], model="m")
    assert calls == [True, False]
    assert response.vision_used is False
    assert response.text == "فقط متن"
    await provider.aclose()


@pytest.mark.asyncio
async def test_summary_provider_surfaces_a_5xx_as_retryable_without_masking_it_as_vision_unsupported():
    async def handler(request: httpx.Request):
        return httpx.Response(503, content=b"upstream unavailable")

    provider = OpenAICompatibleSummaryProvider(
        base_url="https://llm.example/v1",
        api_key="secret",
        timeout_seconds=30,
        temperature=0.2,
        max_tokens=1024,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SummaryError) as raised:
        await provider.summarize(transcript="متن", images=[], model="m")
    assert raised.value.code == "summary_http_503"
    assert raised.value.retryable is True
    await provider.aclose()


# ---------------------------------------------------------------------------
# PDF -> page image conversion (real PyMuPDF, not mocked)
# ---------------------------------------------------------------------------


def _minimal_pdf_bytes(page_count: int = 2) -> bytes:
    document = pymupdf.open()
    for index in range(page_count):
        page = document.new_page(width=300, height=300)
        page.insert_text((20, 100), f"page {index + 1}")
    try:
        return document.tobytes()
    finally:
        document.close()


def test_pdf_pages_render_to_one_valid_png_per_page_at_the_configured_dpi():
    content = _minimal_pdf_bytes(page_count=2)
    document = pymupdf.open(stream=content, filetype="pdf")
    try:
        pages = [page.get_pixmap(dpi=200).tobytes("png") for page in document]
    finally:
        document.close()
    assert len(pages) == 2
    assert all(page.startswith(b"\x89PNG\r\n\x1a\n") for page in pages)
    assert pages[0] != pages[1]


# ---------------------------------------------------------------------------
# MCPWorker orchestration -- real DB/HTTP-facing routes, faked Outline/LLM
# ---------------------------------------------------------------------------


@dataclass
class FakeOutlineMCPClient:
    resolve_result: OutlineDestination
    upload_url_prefix: str = "https://kb.example/attachments/"
    fail_with: OutlineMCPError | None = None
    resolve_calls: list = field(default_factory=list)
    upsert_calls: list = field(default_factory=list)
    upload_calls: list = field(default_factory=list)
    _next_id: int = 0

    def __call__(self, *, base_url, api_key, timeout_seconds):
        return self

    async def resolve_destination(self, path):
        self.resolve_calls.append(path)
        if self.fail_with is not None:
            raise self.fail_with
        return self.resolve_result

    async def upsert_document(self, *, document_id, title, text, collection_id=None, parent_document_id=None):
        self.upsert_calls.append(
            {
                "document_id": document_id,
                "title": title,
                "text": text,
                "collection_id": collection_id,
                "parent_document_id": parent_document_id,
            }
        )
        if document_id:
            return document_id
        self._next_id += 1
        return f"doc-{title}-{self._next_id}"

    async def upload_attachment(self, *, name, content_type, content):
        self.upload_calls.append({"name": name, "content_type": content_type, "content": content})
        return f"{self.upload_url_prefix}{name}"

    async def aclose(self):
        pass


@dataclass
class FakeSummaryProvider:
    text: str = "این خلاصه آزمایشی جلسه است."
    vision_used: bool | None = None
    fail_with: SummaryError | None = None
    calls: list = field(default_factory=list)

    def __call__(self, *, base_url, api_key, timeout_seconds, temperature, max_tokens):
        return self

    async def summarize(self, *, transcript, images, model):
        self.calls.append({"transcript": transcript, "images": images, "model": model})
        if self.fail_with is not None:
            raise self.fail_with
        used = bool(images) if self.vision_used is None else self.vision_used
        return SummaryResponse(text=self.text, external_request_id="req-1", vision_used=used)

    async def aclose(self):
        pass


async def _meeting_with_completed_result(client, session_factory, email: str):
    owner = await register_user(client, email)
    meeting = (
        await client.post("/meetings", headers=authorization(owner), json={"title": "جلسه آزمون MCP"})
    ).json()
    payload = {
        "schema": "meeting-transcript/v1",
        "meeting_id": meeting["id"],
        "meeting_result_id": "pending",
        "generated_at": "2026-09-08T00:00:00Z",
        "duration_ms": 2000,
        "source_count": 1,
        "speaker_count": 1,
        "text": "سلام دنیا",
        "sources": [],
        "speakers": [],
        "segments": [
            {"start_ms": 0, "end_ms": 1000, "speaker_label": "SPEAKER_00", "text": "سلام"},
            {"start_ms": 1000, "end_ms": 2000, "speaker_label": "SPEAKER_00", "text": "دنیا"},
        ],
    }
    async with session_factory() as session:
        result = MeetingResult(
            meeting_id=uuid.UUID(meeting["id"]),
            source_fingerprint="a" * 64,
            completed_at=datetime.now(timezone.utc),
        )
        session.add(result)
        await session.flush()
        payload["meeting_result_id"] = str(result.id)
        content = json.dumps(payload, ensure_ascii=False).encode()
        session.add(
            MeetingResultArtifact(
                meeting_result=result,
                artifact_type=MeetingArtifactType.COMBINED_TRANSCRIPT_JSON,
                minio_bucket="test-exports",
                minio_key=f"meeting-results/{result.id}/transcript.json",
                content_type="application/json",
            )
        )
        await session.commit()
        result_id = result.id
    client.storage.objects[("test-exports", f"meeting-results/{result_id}/transcript.json")] = content
    return owner, meeting, result_id


async def _approve(client, meeting_id, headers, *, path="پروژه‌های کارآموزی/ASR test"):
    response = await client.post(
        f"/meetings/{meeting_id}/publication/approve",
        headers=headers,
        json={"destination_path": path},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _build_worker(session_factory, storage):
    # transcript_api_key/kb_api_key default to None unless a real .env sets
    # them; the worker requires both configured before it even reaches the
    # (faked) provider/client, so force dummy values here rather than relying
    # on whatever secrets happen to be in the local environment.
    settings = get_settings().model_copy(
        update={
            "transcript_api_key": SecretStr("test-transcript-key"),
            "kb_api_key": SecretStr("test-kb-key"),
        }
    )
    return MCPWorker(session_factory=session_factory, storage=storage, settings=settings)


@pytest.mark.asyncio
async def test_mcp_worker_publishes_and_excludes_pdf_pages_from_kb_uploads(
    client, session_factory, monkeypatch
):
    owner, meeting, _ = await _meeting_with_completed_result(
        client, session_factory, "mcp-happy@example.com"
    )
    headers = authorization(owner)
    png = await client.post(
        f"/meetings/{meeting['id']}/attachments",
        headers=headers,
        files={"upload": ("context.png", b"\x89PNG\r\n\x1a\nvalid", "image/png")},
    )
    assert png.status_code == 201
    pdf_bytes = _minimal_pdf_bytes(page_count=2)
    pdf = await client.post(
        f"/meetings/{meeting['id']}/attachments",
        headers=headers,
        files={"upload": ("slides.pdf", pdf_bytes, "application/pdf")},
    )
    assert pdf.status_code == 201

    await _approve(client, meeting["id"], headers)

    fake_outline = FakeOutlineMCPClient(
        resolve_result=OutlineDestination("col-1", None, None)
    )
    fake_summary = FakeSummaryProvider()
    monkeypatch.setattr(mcp_worker_module, "OutlineMCPClient", fake_outline)
    monkeypatch.setattr(mcp_worker_module, "OpenAICompatibleSummaryProvider", fake_summary)

    worker = _build_worker(session_factory, client.storage)
    assert await worker.run_once() is True

    # The LLM gets every image (direct upload + both PDF pages) for context...
    assert len(fake_summary.calls) == 1
    assert len(fake_summary.calls[0]["images"]) == 3
    # ...but only the directly-uploaded PNG may ever reach the KB.
    assert len(fake_outline.upload_calls) == 1
    assert fake_outline.upload_calls[0]["name"] == "context.png"

    async with session_factory() as session:
        publication = await session.scalar(select(MeetingPublication))
        assert publication.status == MeetingPublicationStatus.PUBLISHED
        assert publication.vision_status == PublicationVisionStatus.USED
        assert publication.outline_parent_document_id
        assert publication.outline_summary_document_id
        assert publication.outline_transcript_document_id
        artifact_types = set(
            await session.scalars(
                select(MeetingResultArtifact.artifact_type).where(
                    MeetingResultArtifact.meeting_result_id == publication.meeting_result_id
                )
            )
        )
        assert MeetingArtifactType.SUMMARY in artifact_types
        assert MeetingArtifactType.MCP_RESPONSE in artifact_types

    # Summary document body embeds the uploaded image link, never a PDF page.
    summary_call = next(call for call in fake_outline.upsert_calls if call["title"] == "خلاصه")
    assert "context.png" in summary_call["text"]
    assert "slides" not in summary_call["text"]


@pytest.mark.asyncio
async def test_mcp_worker_reuses_stored_document_ids_on_idempotent_republish(
    client, session_factory, monkeypatch
):
    owner, meeting, _ = await _meeting_with_completed_result(
        client, session_factory, "mcp-idempotent@example.com"
    )
    headers = authorization(owner)
    await _approve(client, meeting["id"], headers)

    fake_outline = FakeOutlineMCPClient(resolve_result=OutlineDestination("col-1", None, None))
    fake_summary = FakeSummaryProvider()
    monkeypatch.setattr(mcp_worker_module, "OutlineMCPClient", fake_outline)
    monkeypatch.setattr(mcp_worker_module, "OpenAICompatibleSummaryProvider", fake_summary)
    worker = _build_worker(session_factory, client.storage)
    assert await worker.run_once() is True

    async with session_factory() as session:
        publication = await session.scalar(select(MeetingPublication))
        first_ids = (
            publication.outline_parent_document_id,
            publication.outline_summary_document_id,
            publication.outline_transcript_document_id,
        )
    assert all(first_ids)
    first_create_calls = len(fake_outline.upsert_calls)
    assert first_create_calls == 3  # parent + summary + transcript, all newly created

    # Re-approve the same (unmodified) content and publish again.
    await _approve(client, meeting["id"], headers)
    assert await worker.run_once() is True

    async with session_factory() as session:
        publication = await session.scalar(select(MeetingPublication))
        second_ids = (
            publication.outline_parent_document_id,
            publication.outline_summary_document_id,
            publication.outline_transcript_document_id,
        )
    assert second_ids == first_ids
    second_run_calls = fake_outline.upsert_calls[first_create_calls:]
    assert len(second_run_calls) == 3
    assert {call["document_id"] for call in second_run_calls} == set(first_ids)


@pytest.mark.asyncio
async def test_mcp_worker_marks_vision_unsupported_and_still_publishes_text(
    client, session_factory, monkeypatch
):
    owner, meeting, _ = await _meeting_with_completed_result(
        client, session_factory, "mcp-vision-unsupported@example.com"
    )
    headers = authorization(owner)
    await client.post(
        f"/meetings/{meeting['id']}/attachments",
        headers=headers,
        files={"upload": ("context.png", b"\x89PNG\r\n\x1a\nvalid", "image/png")},
    )
    await _approve(client, meeting["id"], headers)

    fake_outline = FakeOutlineMCPClient(resolve_result=OutlineDestination("col-1", None, None))
    fake_summary = FakeSummaryProvider(vision_used=False)
    monkeypatch.setattr(mcp_worker_module, "OutlineMCPClient", fake_outline)
    monkeypatch.setattr(mcp_worker_module, "OpenAICompatibleSummaryProvider", fake_summary)
    worker = _build_worker(session_factory, client.storage)
    assert await worker.run_once() is True

    async with session_factory() as session:
        publication = await session.scalar(select(MeetingPublication))
        assert publication.status == MeetingPublicationStatus.PUBLISHED
        assert publication.vision_status == PublicationVisionStatus.UNSUPPORTED


@pytest.mark.asyncio
async def test_mcp_worker_retries_a_retryable_kb_failure_and_keeps_publication_queued(
    client, session_factory, monkeypatch
):
    owner, meeting, _ = await _meeting_with_completed_result(
        client, session_factory, "mcp-retry@example.com"
    )
    headers = authorization(owner)
    await _approve(client, meeting["id"], headers)

    fake_outline = FakeOutlineMCPClient(
        resolve_result=OutlineDestination("col-1", None, None),
        fail_with=OutlineMCPError("temporary", code="mcp_http_503", retryable=True),
    )
    fake_summary = FakeSummaryProvider()
    monkeypatch.setattr(mcp_worker_module, "OutlineMCPClient", fake_outline)
    monkeypatch.setattr(mcp_worker_module, "OpenAICompatibleSummaryProvider", fake_summary)
    worker = _build_worker(session_factory, client.storage)
    assert await worker.run_once() is True

    async with session_factory() as session:
        publication = await session.scalar(select(MeetingPublication))
        assert publication.status == MeetingPublicationStatus.QUEUED
        assert publication.error_code == "mcp_http_503"
        attempts = (
            await session.scalars(
                select(ProcessingAttempt)
                .join(ProcessingAttempt.job)
                .where(ProcessingJob.stage == ProcessingStage.MINUTES_GENERATION)
            )
        ).all()
        assert len(attempts) == 2
        assert attempts[0].status == ProcessingAttemptStatus.FAILED
        assert attempts[1].status == ProcessingAttemptStatus.QUEUED


@pytest.mark.asyncio
async def test_mcp_worker_fails_permanently_on_a_non_retryable_kb_error(
    client, session_factory, monkeypatch
):
    owner, meeting, _ = await _meeting_with_completed_result(
        client, session_factory, "mcp-permanent-failure@example.com"
    )
    headers = authorization(owner)
    await _approve(client, meeting["id"], headers)

    fake_outline = FakeOutlineMCPClient(
        resolve_result=OutlineDestination("col-1", None, None),
        fail_with=OutlineMCPError(
            "missing", code="destination_not_found", retryable=False
        ),
    )
    fake_summary = FakeSummaryProvider()
    monkeypatch.setattr(mcp_worker_module, "OutlineMCPClient", fake_outline)
    monkeypatch.setattr(mcp_worker_module, "OpenAICompatibleSummaryProvider", fake_summary)
    worker = _build_worker(session_factory, client.storage)
    assert await worker.run_once() is True

    async with session_factory() as session:
        publication = await session.scalar(select(MeetingPublication))
        assert publication.status == MeetingPublicationStatus.FAILED
        assert publication.error_code == "destination_not_found"
        attempts = (
            await session.scalars(
                select(ProcessingAttempt)
                .join(ProcessingAttempt.job)
                .where(ProcessingJob.stage == ProcessingStage.MINUTES_GENERATION)
            )
        ).all()
        assert len(attempts) == 1
        assert attempts[0].status == ProcessingAttemptStatus.FAILED
