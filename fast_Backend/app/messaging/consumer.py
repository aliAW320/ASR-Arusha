import json
import uuid
from collections.abc import Awaitable, Callable

import aio_pika
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..models import ProcessingAttempt, ProcessingAttemptStatus
from ..observability.logging import get_logger
from .topology import declare_topology


logger = get_logger(__name__)
AttemptHandler = Callable[[uuid.UUID], Awaitable[bool]]


async def _attempt_has_retry(
    session_factory: async_sessionmaker[AsyncSession],
    attempt: ProcessingAttempt,
) -> bool:
    async with session_factory() as session:
        return (
            await session.scalar(
                select(ProcessingAttempt.id).where(
                    ProcessingAttempt.job_id == attempt.job_id,
                    ProcessingAttempt.attempt_number > attempt.attempt_number,
                    ProcessingAttempt.status == ProcessingAttemptStatus.QUEUED,
                )
            )
        ) is not None


async def handle_attempt_message(
    *,
    message: aio_pika.abc.AbstractIncomingMessage,
    queue_name: str,
    session_factory: async_sessionmaker[AsyncSession],
    handler: AttemptHandler,
) -> None:
    """Handle one delivery and acknowledge only after a durable worker outcome."""
    try:
        payload = json.loads(message.body)
        attempt_id = uuid.UUID(str(payload["attempt_id"]))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        logger.error(
            "broker_message_invalid",
            "RabbitMQ message payload is invalid",
            queue=queue_name,
            message_id=message.message_id,
        )
        await message.reject(requeue=False)
        return

    try:
        worked = await handler(attempt_id)
        if not worked:
            # A redelivery after the attempt transaction committed is safe.
            await message.ack()
            return
        async with session_factory() as session:
            attempt = await session.get(ProcessingAttempt, attempt_id)
        if attempt is None:
            await message.ack()
        elif (
            attempt.status == ProcessingAttemptStatus.FAILED
            and not await _attempt_has_retry(session_factory, attempt)
        ):
            # Work queues all point at the shared DLX, so this is retained for
            # investigation while the exact error remains queryable from DB/UI.
            await message.reject(requeue=False)
        else:
            await message.ack()
    except Exception as error:
        logger.exception(
            "broker_message_processing_failed",
            "RabbitMQ delivery failed before a durable outcome",
            error=error,
            queue=queue_name,
            attempt_id=str(attempt_id),
        )
        await message.nack(requeue=True)


async def consume_attempt_queue(
    *,
    settings: Settings,
    queue_name: str,
    session_factory: async_sessionmaker[AsyncSession],
    handler: AttemptHandler,
) -> None:
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=settings.rabbitmq_prefetch_count)
        queues = await declare_topology(channel, settings)
        queue = queues[queue_name]
        async with queue.iterator() as messages:
            async for message in messages:
                await handle_attempt_message(
                    message=message,
                    queue_name=queue_name,
                    session_factory=session_factory,
                    handler=handler,
                )
