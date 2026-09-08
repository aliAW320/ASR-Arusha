import asyncio
import json
from datetime import datetime, timedelta, timezone

import aio_pika
from aio_pika import DeliveryMode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..database import SessionFactory, close_database
from ..models import BrokerOutboxMessage
from ..observability.logging import configure_logging, get_logger
from .topology import PROCESSING_EXCHANGE, declare_topology


logger = get_logger(__name__)


async def _next_message(session: AsyncSession) -> BrokerOutboxMessage | None:
    return await session.scalar(
        select(BrokerOutboxMessage)
        .where(
            BrokerOutboxMessage.published_at.is_(None),
            BrokerOutboxMessage.available_at <= datetime.now(timezone.utc),
        )
        .order_by(BrokerOutboxMessage.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )


async def _publish_next(
    exchange: aio_pika.abc.AbstractExchange,
) -> bool:
    """Publish one available outbox row while retaining its database lock.

    Publisher confirmation happens before the row is marked published. If the
    process dies between those operations RabbitMQ may deliver a duplicate;
    consumers tolerate that by claiming only QUEUED attempts.
    """
    async with SessionFactory() as session:
        async with session.begin():
            outbox = await _next_message(session)
            if outbox is None:
                return False
            try:
                await exchange.publish(
                    aio_pika.Message(
                        body=json.dumps(
                            outbox.payload, separators=(",", ":")
                        ).encode(),
                        content_type="application/json",
                        delivery_mode=DeliveryMode.PERSISTENT,
                        message_id=str(outbox.id),
                        headers={"x-deduplication-key": outbox.deduplication_key},
                    ),
                    routing_key=outbox.queue_name,
                    mandatory=True,
                )
            except Exception as error:
                outbox.publish_attempts += 1
                outbox.last_error = str(error)[:2000]
                outbox.available_at = datetime.now(timezone.utc) + timedelta(seconds=1)
                logger.exception(
                    "broker_publish_failed",
                    "RabbitMQ outbox publish failed",
                    error=error,
                    outbox_id=str(outbox.id),
                    queue=outbox.queue_name,
                )
                return True

            outbox.published_at = datetime.now(timezone.utc)
            outbox.publish_attempts += 1
            outbox.last_error = None
            return True


async def run_forever() -> None:
    settings = get_settings()
    configure_logging(
        service="broker-dispatcher",
        environment=settings.app_env,
        level=settings.log_level,
        json_output=settings.json_logs_enabled,
    )
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    try:
        async with connection:
            channel = await connection.channel(publisher_confirms=True)
            await declare_topology(channel, settings)
            exchange = await channel.get_exchange(PROCESSING_EXCHANGE)
            while True:
                if not await _publish_next(exchange):
                    await asyncio.sleep(0.25)
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(run_forever())
