import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import BrokerOutboxMessage, ProcessingAttempt, ProcessingStage
from .topology import QueueNames


def queue_for_stage(stage: ProcessingStage, settings: Settings) -> str:
    names = QueueNames.from_settings(settings)
    mapping = {
        ProcessingStage.TRANSCRIPTION: names.asr,
        ProcessingStage.DIARIZATION: names.diar,
        ProcessingStage.CLEANING: names.cleaning,
        ProcessingStage.MINUTES_GENERATION: names.mcp,
    }
    try:
        return mapping[stage]
    except KeyError as error:
        raise ValueError(f"Processing stage {stage.value} has no RabbitMQ queue") from error


def enqueue_message(
    session: AsyncSession,
    *,
    queue_name: str,
    payload: dict[str, Any],
    deduplication_key: str,
    delay_seconds: float = 0,
) -> BrokerOutboxMessage:
    message = BrokerOutboxMessage(
        id=uuid.uuid4(),
        queue_name=queue_name,
        payload=payload,
        deduplication_key=deduplication_key,
        available_at=datetime.now(timezone.utc) + timedelta(seconds=delay_seconds),
    )
    session.add(message)
    return message


def enqueue_attempt(
    session: AsyncSession,
    attempt: ProcessingAttempt,
    stage: ProcessingStage,
    settings: Settings,
    *,
    retry: bool = False,
) -> BrokerOutboxMessage:
    if attempt.id is None or attempt.job_id is None:
        raise ValueError("Attempt must be flushed before it can be queued")
    message = enqueue_message(
        session,
        queue_name=queue_for_stage(stage, settings),
        payload={
            "attempt_id": str(attempt.id),
            "job_id": str(attempt.job_id),
            "stage": stage.value,
        },
        deduplication_key=f"attempt:{attempt.id}",
        delay_seconds=settings.rabbitmq_retry_delay_seconds if retry else 0,
    )
    # Record the durable message identity in the same transaction that creates
    # the attempt/outbox row. The dispatcher must not lock the attempt after
    # publishing: RabbitMQ can deliver before that transaction commits, causing
    # a SKIP LOCKED worker claim to miss the attempt and lose the delivery.
    attempt.queue_task_id = str(message.id)
    return message
