import asyncio
import base64
import hashlib
import io
import json
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.config import Settings, get_settings
from app.database import SessionFactory, close_database
from app.messaging.consumer import consume_attempt_queue
from app.messaging.outbox import enqueue_attempt
from app.messaging.topology import QueueNames
from app.models import (
    Meeting,
    MeetingArtifactType,
    MeetingAttachment,
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
from app.observability.logging import configure_logging, get_logger
from app.services.audit import add_history_event
from app.services.cancellation import ProcessingCancelled, attempt_is_cancelled
from app.storage.base import ObjectStorage
from app.storage.minio import get_object_storage

from .outline import OutlineMCPClient, OutlineMCPError
from .prompt import render_speaker_transcript
from .summary import (
    OpenAICompatibleSummaryProvider,
    SummaryError,
    SummaryImage,
)


logger = get_logger(__name__)


@dataclass(frozen=True)
class AttachmentInput:
    id: uuid.UUID
    bucket: str
    key: str
    filename: str
    content_type: str


@dataclass(frozen=True)
class WorkItem:
    attempt_id: uuid.UUID
    attempt_number: int
    job_id: uuid.UUID
    publication_id: uuid.UUID
    meeting_id: uuid.UUID
    meeting_result_id: uuid.UUID
    meeting_title: str
    destination_path: str
    transcript_bucket: str
    transcript_key: str
    model_name: str
    parent_document_id: str | None
    summary_document_id: str | None
    transcript_document_id: str | None
    attachments: list[AttachmentInput]


class PublicationError(Exception):
    def __init__(self, message: str, *, code: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MCPWorker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        settings: Settings,
    ):
        self.session_factory = session_factory
        self.storage = storage
        self.settings = settings
        self.worker_name = f"{settings.mcp_worker_name}@{socket.gethostname()}"

    async def claim_next(self, attempt_id: uuid.UUID | None = None) -> uuid.UUID | None:
        async with self.session_factory() as session:
            statement = (
                select(ProcessingAttempt)
                .join(ProcessingAttempt.job)
                .where(
                    ProcessingAttempt.status == ProcessingAttemptStatus.QUEUED,
                    ProcessingJob.stage == ProcessingStage.MINUTES_GENERATION,
                )
                .order_by(ProcessingJob.priority.desc(), ProcessingAttempt.queued_at)
                .with_for_update(skip_locked=True, of=ProcessingAttempt)
                .limit(1)
            )
            if attempt_id is not None:
                statement = statement.where(ProcessingAttempt.id == attempt_id)
            attempt = await session.scalar(statement)
            if attempt is None:
                return None
            publication = await session.scalar(
                select(MeetingPublication).where(
                    MeetingPublication.current_job_id == attempt.job_id
                )
            )
            if publication is None:
                attempt.status = ProcessingAttemptStatus.CANCELLED
                attempt.finished_at = datetime.now(timezone.utc)
                attempt.error_code = "publication_superseded"
                attempt.error_message = "Publication approval was replaced"
                await session.commit()
                return None
            attempt.status = ProcessingAttemptStatus.RUNNING
            attempt.started_at = datetime.now(timezone.utc)
            attempt.worker_name = self.worker_name
            publication.status = MeetingPublicationStatus.RUNNING
            meeting = await session.get(Meeting, publication.meeting_id)
            add_history_event(
                session,
                event_type="meeting_publication.started",
                description="Meeting summary and knowledge-base publication started",
                event_data={
                    "publication_id": str(publication.id),
                    "job_id": str(attempt.job_id),
                    "attempt_number": attempt.attempt_number,
                    "destination_path": publication.destination_path,
                },
                affected_meetings=[meeting] if meeting else [],
            )
            await session.commit()
            return attempt.id

    async def _load_work_item(self, attempt_id: uuid.UUID) -> WorkItem:
        async with self.session_factory() as session:
            attempt = await session.scalar(
                select(ProcessingAttempt)
                .options(
                    joinedload(ProcessingAttempt.job)
                    .joinedload(ProcessingJob.meeting_result)
                    .joinedload(MeetingResult.meeting),
                    joinedload(ProcessingAttempt.job).joinedload(ProcessingJob.model),
                )
                .where(ProcessingAttempt.id == attempt_id)
            )
            job = attempt.job if attempt else None
            meeting_result = job.meeting_result if job else None
            if attempt is None or job is None or meeting_result is None or job.model is None:
                raise PublicationError(
                    "Queued publication job is incomplete",
                    code="publication_job_invalid",
                    retryable=False,
                )
            publication = await session.scalar(
                select(MeetingPublication).where(
                    MeetingPublication.current_job_id == job.id,
                    MeetingPublication.meeting_result_id == meeting_result.id,
                )
            )
            if publication is None or publication.approved_at is None:
                raise ProcessingCancelled("Publication approval is no longer current")
            transcript = await session.scalar(
                select(MeetingResultArtifact)
                .where(
                    MeetingResultArtifact.meeting_result_id == meeting_result.id,
                    MeetingResultArtifact.artifact_type
                    == MeetingArtifactType.COMBINED_TRANSCRIPT_JSON,
                )
                .order_by(MeetingResultArtifact.created_at.desc())
            )
            if transcript is None:
                raise PublicationError(
                    "Approved meeting transcript artifact is missing",
                    code="meeting_transcript_missing",
                    retryable=False,
                )
            ids = [uuid.UUID(value) for value in publication.attachment_ids]
            attachments_by_id = {
                item.id: item
                for item in await session.scalars(
                    select(MeetingAttachment).where(MeetingAttachment.id.in_(ids))
                )
            }
            missing = [str(value) for value in ids if value not in attachments_by_id]
            if missing:
                raise PublicationError(
                    f"Approved meeting attachments are missing: {', '.join(missing)}",
                    code="meeting_attachment_missing",
                    retryable=False,
                )
            return WorkItem(
                attempt_id=attempt.id,
                attempt_number=attempt.attempt_number,
                job_id=job.id,
                publication_id=publication.id,
                meeting_id=meeting_result.meeting_id,
                meeting_result_id=meeting_result.id,
                meeting_title=meeting_result.meeting.title,
                destination_path=publication.destination_path,
                transcript_bucket=transcript.minio_bucket,
                transcript_key=transcript.minio_key,
                model_name=job.model.name,
                parent_document_id=publication.outline_parent_document_id,
                summary_document_id=publication.outline_summary_document_id,
                transcript_document_id=publication.outline_transcript_document_id,
                attachments=[
                    AttachmentInput(
                        id=item.id,
                        bucket=item.minio_bucket,
                        key=item.minio_key,
                        filename=item.original_filename,
                        content_type=item.content_type,
                    )
                    for item in (attachments_by_id[value] for value in ids)
                ],
            )

    async def _load_transcript(self, item: WorkItem) -> dict:
        buffer = io.BytesIO()
        await self.storage.download_object(
            item.transcript_bucket, item.transcript_key, buffer
        )
        try:
            payload = json.loads(buffer.getvalue().decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise PublicationError(
                "Meeting transcript is not valid UTF-8 JSON",
                code="meeting_transcript_invalid",
                retryable=False,
            ) from error
        if not isinstance(payload.get("segments"), list):
            raise PublicationError(
                "Meeting transcript has no segments array",
                code="meeting_transcript_invalid",
                retryable=False,
            )
        return payload

    async def _load_images(self, item: WorkItem) -> list[SummaryImage]:
        images: list[SummaryImage] = []
        for attachment in item.attachments:
            if await attempt_is_cancelled(self.session_factory, item.attempt_id):
                raise ProcessingCancelled("Publication was cancelled")
            buffer = io.BytesIO()
            await self.storage.download_object(attachment.bucket, attachment.key, buffer)
            content = buffer.getvalue()
            if attachment.content_type == "application/pdf":
                try:
                    import pymupdf

                    document = pymupdf.open(stream=content, filetype="pdf")
                    try:
                        for index, page in enumerate(document):
                            png = page.get_pixmap(dpi=self.settings.summary_pdf_dpi).tobytes(
                                "png"
                            )
                            images.append(
                                self._summary_image(
                                    f"{Path(attachment.filename).stem}-page-{index + 1}.png",
                                    "image/png",
                                    png,
                                    is_pdf_page=True,
                                )
                            )
                    finally:
                        document.close()
                except Exception as error:
                    raise PublicationError(
                        f"PDF conversion failed for {attachment.filename}: {error}",
                        code="pdf_conversion_failed",
                        retryable=False,
                    ) from error
            else:
                images.append(
                    self._summary_image(
                        attachment.filename, attachment.content_type, content
                    )
                )
        return images

    @staticmethod
    def _summary_image(
        name: str, content_type: str, content: bytes, *, is_pdf_page: bool = False
    ) -> SummaryImage:
        return SummaryImage(
            name=name,
            content_type=content_type,
            data_url=f"data:{content_type};base64,{base64.b64encode(content).decode()}",
            is_pdf_page=is_pdf_page,
        )

    async def _generate_summary(
        self, transcript: str, images: list[SummaryImage], model: str
    ) -> tuple[str, str | None, PublicationVisionStatus]:
        if self.settings.transcript_api_key is None:
            raise PublicationError(
                "TRANSCRIPT_API_KEY is not configured for summary generation",
                code="summary_configuration_error",
                retryable=False,
            )
        provider = OpenAICompatibleSummaryProvider(
            base_url=self.settings.base_url,
            api_key=self.settings.transcript_api_key.get_secret_value(),
            timeout_seconds=self.settings.summary_request_timeout_seconds,
            temperature=self.settings.summary_temperature,
            max_tokens=self.settings.summary_max_tokens,
        )
        requested_images = images if self.settings.summary_multimodal_enabled else []
        try:
            response = await provider.summarize(
                transcript=transcript, images=requested_images, model=model
            )
            if not requested_images:
                vision_status = PublicationVisionStatus.NOT_REQUESTED
            elif response.vision_used:
                vision_status = PublicationVisionStatus.USED
            else:
                vision_status = PublicationVisionStatus.UNSUPPORTED
            return response.text, response.external_request_id, vision_status
        finally:
            await provider.aclose()

    @staticmethod
    def _find_named_child(node: dict | None, title: str) -> tuple[str | None, dict | None]:
        child = next(
            (value for value in (node or {}).get("children", []) if value.get("title") == title),
            None,
        )
        return (str(child["id"]), child) if child else (None, None)

    async def _publish(
        self,
        item: WorkItem,
        summary: str,
        transcript: str,
        images: list[SummaryImage],
    ) -> tuple[str, str, str, list[str]]:
        if self.settings.kb_api_key is None:
            raise PublicationError(
                "KB_API_KEY is not configured",
                code="mcp_configuration_error",
                retryable=False,
            )
        client = OutlineMCPClient(
            base_url=self.settings.kb_base_url,
            api_key=self.settings.kb_api_key.get_secret_value(),
            timeout_seconds=self.settings.mcp_request_timeout_seconds,
        )
        try:
            destination = await client.resolve_destination(item.destination_path)
            discovered_parent_id, parent_node = self._find_named_child(
                destination.node, item.meeting_title
            )
            parent_id = item.parent_document_id or discovered_parent_id
            parent_id = await client.upsert_document(
                document_id=parent_id,
                title=item.meeting_title,
                text=(
                    "خروجی تأییدشده جلسه شامل خلاصه، پیوست‌ها و متن کامل "
                    "تفکیک‌شده بر اساس گوینده است."
                ),
                collection_id=(
                    destination.collection_id
                    if destination.parent_document_id is None
                    else None
                ),
                parent_document_id=destination.parent_document_id,
            )
            if parent_node is None and discovered_parent_id:
                parent_node = {"id": discovered_parent_id, "children": []}
            discovered_summary_id, _ = self._find_named_child(parent_node, "خلاصه")
            discovered_transcript_id, _ = self._find_named_child(parent_node, "صحبت‌ها")

            # PDF-generated page images are LLM-only context; only images the
            # user directly uploaded as PNG/JPEG may ever reach the KB.
            uploadable_images = [image for image in images if not image.is_pdf_page]
            uploaded_urls = []
            for image in uploadable_images:
                encoded = image.data_url.split(",", 1)[1]
                uploaded_urls.append(
                    await client.upload_attachment(
                        name=image.name,
                        content_type=image.content_type,
                        content=base64.b64decode(encoded),
                    )
                )
            summary_body = summary
            if uploaded_urls:
                embeds = "\n\n".join(
                    f"![{image.name}]({url})"
                    for image, url in zip(uploadable_images, uploaded_urls, strict=True)
                )
                summary_body = f"{summary}\n\n## پیوست‌ها\n\n{embeds}"

            summary_id = await client.upsert_document(
                document_id=item.summary_document_id or discovered_summary_id,
                title="خلاصه",
                text=summary_body,
                parent_document_id=parent_id,
            )
            transcript_id = await client.upsert_document(
                document_id=item.transcript_document_id or discovered_transcript_id,
                title="صحبت‌ها",
                text=transcript,
                parent_document_id=parent_id,
            )
            return parent_id, summary_id, transcript_id, uploaded_urls
        finally:
            await client.aclose()

    async def _finish_success(
        self,
        item: WorkItem,
        *,
        summary: str,
        request_id: str | None,
        vision_status: PublicationVisionStatus,
        document_ids: tuple[str, str, str],
        attachment_urls: list[str],
    ) -> None:
        summary_bytes = summary.encode("utf-8")
        response_bytes = json.dumps(
            {
                "publication_id": str(item.publication_id),
                "destination_path": item.destination_path,
                "parent_document_id": document_ids[0],
                "summary_document_id": document_ids[1],
                "transcript_document_id": document_ids[2],
                "attachment_urls": attachment_urls,
                "vision_status": vision_status.value,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        prefix = (
            f"meetings/{item.meeting_id}/meeting-results/{item.meeting_result_id}/"
            f"publication/{item.job_id}"
        )
        objects = [
            (f"{prefix}/summary.md", summary_bytes, "text/markdown"),
            (f"{prefix}/mcp-response.json", response_bytes, "application/json"),
        ]
        for key, content, content_type in objects:
            await self.storage.put_object(
                self.settings.minio_exports_bucket,
                key,
                io.BytesIO(content),
                len(content),
                content_type,
            )
        try:
            async with self.session_factory() as session:
                attempt = await session.get(ProcessingAttempt, item.attempt_id)
                job = await session.get(ProcessingJob, item.job_id)
                publication = await session.get(MeetingPublication, item.publication_id)
                meeting_result = await session.get(MeetingResult, item.meeting_result_id)
                meeting = await session.get(Meeting, item.meeting_id)
                if (
                    attempt is None
                    or job is None
                    or publication is None
                    or meeting_result is None
                    or attempt.status == ProcessingAttemptStatus.CANCELLED
                    or publication.current_job_id != job.id
                ):
                    raise ProcessingCancelled("Publication was cancelled before completion")
                session.add_all(
                    [
                        MeetingResultArtifact(
                            meeting_result=meeting_result,
                            artifact_type=artifact_type,
                            minio_bucket=self.settings.minio_exports_bucket,
                            minio_key=key,
                            content_type=content_type,
                            checksum_sha256=hashlib.sha256(content).hexdigest(),
                            producer_job=job,
                        )
                        for (key, content, content_type), artifact_type in zip(
                            objects,
                            (MeetingArtifactType.SUMMARY, MeetingArtifactType.MCP_RESPONSE),
                            strict=True,
                        )
                    ]
                )
                now = datetime.now(timezone.utc)
                attempt.status = ProcessingAttemptStatus.SUCCEEDED
                attempt.external_request_id = request_id
                attempt.finished_at = now
                publication.status = MeetingPublicationStatus.PUBLISHED
                publication.vision_status = vision_status
                publication.outline_parent_document_id = document_ids[0]
                publication.outline_summary_document_id = document_ids[1]
                publication.outline_transcript_document_id = document_ids[2]
                publication.error_code = None
                publication.error_message = None
                publication.completed_at = now
                add_history_event(
                    session,
                    event_type="meeting_publication.succeeded",
                    description="Meeting published to the knowledge base",
                    event_data={
                        "publication_id": str(publication.id),
                        "job_id": str(job.id),
                        "destination_path": publication.destination_path,
                        "vision_status": vision_status.value,
                        "attachment_count": len(attachment_urls),
                    },
                    affected_meetings=[meeting] if meeting else [],
                    affected_meeting_results=[meeting_result],
                )
                await session.commit()
        except Exception:
            for key, _, _ in objects:
                try:
                    await self.storage.remove_object(
                        self.settings.minio_exports_bucket, key
                    )
                except Exception:
                    pass
            raise

    async def _finish_failure(
        self, item: WorkItem, *, code: str, message: str, retryable: bool
    ) -> None:
        async with self.session_factory() as session:
            attempt = await session.get(ProcessingAttempt, item.attempt_id)
            job = await session.get(ProcessingJob, item.job_id)
            publication = await session.get(MeetingPublication, item.publication_id)
            meeting = await session.get(Meeting, item.meeting_id)
            meeting_result = await session.get(MeetingResult, item.meeting_result_id)
            if attempt is None or job is None or publication is None:
                return
            attempt.status = ProcessingAttemptStatus.FAILED
            attempt.error_code = code[:100]
            attempt.error_message = message[:2000]
            attempt.finished_at = datetime.now(timezone.utc)
            will_retry = retryable and attempt.attempt_number < self.settings.mcp_max_attempts
            if will_retry:
                next_attempt = ProcessingAttempt(
                    job=job,
                    attempt_number=attempt.attempt_number + 1,
                    status=ProcessingAttemptStatus.QUEUED,
                )
                session.add(next_attempt)
                await session.flush()
                enqueue_attempt(
                    session,
                    next_attempt,
                    ProcessingStage.MINUTES_GENERATION,
                    self.settings,
                    retry=True,
                )
                publication.status = MeetingPublicationStatus.QUEUED
            else:
                publication.status = MeetingPublicationStatus.FAILED
            publication.error_code = code[:100]
            publication.error_message = message[:2000]
            add_history_event(
                session,
                event_type=(
                    "meeting_publication.retry_scheduled"
                    if will_retry
                    else "meeting_publication.failed"
                ),
                description=(
                    "Meeting publication retry scheduled"
                    if will_retry
                    else "Meeting publication failed"
                ),
                event_data={
                    "publication_id": str(publication.id),
                    "job_id": str(job.id),
                    "attempt_number": attempt.attempt_number,
                    "error_code": code,
                    "error_message": message[:500],
                    "will_retry": will_retry,
                },
                affected_meetings=[meeting] if meeting else [],
                affected_meeting_results=[meeting_result] if meeting_result else [],
            )
            await session.commit()

    async def run_once(self, attempt_id: uuid.UUID | None = None) -> bool:
        claimed = await self.claim_next(attempt_id)
        if claimed is None:
            return False
        try:
            item = await self._load_work_item(claimed)
        except ProcessingCancelled:
            return True
        try:
            payload = await self._load_transcript(item)
            transcript = render_speaker_transcript(payload)
            images = await self._load_images(item)
            summary, request_id, vision_status = await self._generate_summary(
                transcript, images, item.model_name
            )
            parent_id, summary_id, transcript_id, urls = await self._publish(
                item, summary, transcript, images
            )
            await self._finish_success(
                item,
                summary=summary,
                request_id=request_id,
                vision_status=vision_status,
                document_ids=(parent_id, summary_id, transcript_id),
                attachment_urls=urls,
            )
        except ProcessingCancelled:
            return True
        except (PublicationError, SummaryError, OutlineMCPError) as error:
            await self._finish_failure(
                item,
                code=error.code,
                message=str(error),
                retryable=error.retryable,
            )
        except Exception as error:
            logger.exception(
                "meeting_publication_unexpected_error",
                "Unexpected meeting publication failure",
                error=error,
                publication_id=str(item.publication_id),
            )
            await self._finish_failure(
                item,
                code="meeting_publication_unexpected_error",
                message=str(error),
                retryable=True,
            )
        return True


async def run(settings: Settings) -> None:
    """Run the MCP publication consume loop until cancelled.

    Does not touch global logging config or the shared DB engine, so it can
    be embedded as a background task in another process (e.g. the API
    process) alongside other workers that share the same engine.
    """
    worker = MCPWorker(
        session_factory=SessionFactory,
        storage=get_object_storage(),
        settings=settings,
    )
    queue_name = QueueNames.from_settings(settings).mcp
    logger.info(
        "mcp_worker_started",
        "MCP publication worker started",
        worker_name=worker.worker_name,
        queue=queue_name,
    )
    await consume_attempt_queue(
        settings=settings,
        queue_name=queue_name,
        session_factory=SessionFactory,
        handler=lambda attempt_id: worker.run_once(attempt_id),
    )


async def run_forever() -> None:
    """Standalone entrypoint: owns logging setup and DB engine teardown."""
    settings = get_settings()
    configure_logging(
        service="mcp-worker",
        environment=settings.app_env,
        level=settings.log_level,
        json_output=settings.json_logs_enabled,
    )
    try:
        await run(settings)
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(run_forever())
