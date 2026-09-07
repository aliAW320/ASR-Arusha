from dataclasses import dataclass

import aio_pika
from aio_pika import ExchangeType

from ..config import Settings


PROCESSING_EXCHANGE = "meeting.processing"
DEAD_LETTER_EXCHANGE = "meeting.processing.dlx"
DEAD_LETTER_ROUTING_KEY = "processing.failed"


@dataclass(frozen=True)
class QueueNames:
    asr: str
    diar: str
    cleaning: str
    mcp: str
    dead_letter: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "QueueNames":
        return cls(
            asr=settings.rabbitmq_asr_queue,
            diar=settings.rabbitmq_diar_queue,
            cleaning=settings.rabbitmq_cleaning_queue,
            mcp=settings.rabbitmq_mcp_queue,
            dead_letter=settings.rabbitmq_dead_letter_queue,
        )

    @property
    def work_queues(self) -> tuple[str, ...]:
        return self.asr, self.diar, self.cleaning, self.mcp


async def declare_topology(
    channel: aio_pika.abc.AbstractChannel,
    settings: Settings,
) -> dict[str, aio_pika.abc.AbstractQueue]:
    names = QueueNames.from_settings(settings)
    exchange = await channel.declare_exchange(
        PROCESSING_EXCHANGE, ExchangeType.DIRECT, durable=True
    )
    dead_exchange = await channel.declare_exchange(
        DEAD_LETTER_EXCHANGE, ExchangeType.DIRECT, durable=True
    )
    dead_queue = await channel.declare_queue(names.dead_letter, durable=True)
    await dead_queue.bind(dead_exchange, routing_key=DEAD_LETTER_ROUTING_KEY)

    queues: dict[str, aio_pika.abc.AbstractQueue] = {}
    arguments = {
        "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
        "x-dead-letter-routing-key": DEAD_LETTER_ROUTING_KEY,
    }
    for queue_name in names.work_queues:
        queue = await channel.declare_queue(
            queue_name,
            durable=True,
            arguments=arguments,
        )
        await queue.bind(exchange, routing_key=queue_name)
        queues[queue_name] = queue
    queues[names.dead_letter] = dead_queue
    return queues
