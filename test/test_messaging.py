import json
import uuid
from datetime import datetime, timezone

import pytest
from aio_pika import DeliveryMode, ExchangeType
from sqlalchemy import select

from app.config import get_settings
from app.messaging import dispatcher
from app.messaging.consumer import handle_attempt_message
from app.messaging.topology import (
    DEAD_LETTER_EXCHANGE,
    DEAD_LETTER_ROUTING_KEY,
    PROCESSING_EXCHANGE,
    QueueNames,
    declare_topology,
)
from app.models import (
    BrokerOutboxMessage,
    ProcessingAttempt,
    ProcessingAttemptStatus,
    ProcessingJob,
    ProcessingStage,
)
from conftest import authorization, register_user


class FakeQueue:
    def __init__(self, name, *, durable, arguments):
        self.name = name
        self.durable = durable
        self.arguments = arguments
        self.bindings = []

    async def bind(self, exchange, *, routing_key):
        self.bindings.append((exchange.name, routing_key))


class FakeExchange:
    def __init__(self, name, *, exchange_type=None, durable=None):
        self.name = name
        self.exchange_type = exchange_type
        self.durable = durable
        self.published = []

    async def publish(self, message, *, routing_key, mandatory):
        self.published.append((message, routing_key, mandatory))


class FakeChannel:
    def __init__(self):
        self.exchanges = {}
        self.queues = {}

    async def declare_exchange(self, name, exchange_type, *, durable):
        exchange = FakeExchange(name, exchange_type=exchange_type, durable=durable)
        self.exchanges[name] = exchange
        return exchange

    async def declare_queue(self, name, *, durable, arguments=None):
        queue = FakeQueue(name, durable=durable, arguments=arguments)
        self.queues[name] = queue
        return queue


class FakeIncomingMessage:
    def __init__(self, body):
        self.body = body
        self.message_id = "test-delivery"
        self.acked = 0
        self.rejected = []
        self.nacked = []

    async def ack(self):
        self.acked += 1

    async def reject(self, *, requeue):
        self.rejected.append(requeue)

    async def nack(self, *, requeue):
        self.nacked.append(requeue)


async def _uploaded_attempt(client, session_factory):
    owner = await register_user(client, "broker-test@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Broker"}
        )
    ).json()
    uploaded = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(owner),
        files={"upload": ("broker.wav", b"RIFF-audio", "audio/wav")},
    )
    assert uploaded.status_code == 201, uploaded.text
    async with session_factory() as session:
        return await session.scalar(
            select(ProcessingAttempt)
            .join(ProcessingAttempt.job)
            .where(ProcessingJob.stage == ProcessingStage.PREPROCESS)
        )


@pytest.mark.asyncio
async def test_topology_declares_four_durable_work_queues_and_shared_dlq():
    settings = get_settings()
    names = QueueNames.from_settings(settings)
    channel = FakeChannel()

    queues = await declare_topology(channel, settings)

    assert set(queues) == {*names.work_queues, names.dead_letter}
    assert channel.exchanges[PROCESSING_EXCHANGE].exchange_type == ExchangeType.DIRECT
    assert channel.exchanges[PROCESSING_EXCHANGE].durable is True
    assert channel.exchanges[DEAD_LETTER_EXCHANGE].durable is True
    for name in names.work_queues:
        assert channel.queues[name].durable is True
        assert channel.queues[name].arguments == {
            "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
            "x-dead-letter-routing-key": DEAD_LETTER_ROUTING_KEY,
        }
        assert channel.queues[name].bindings == [(PROCESSING_EXCHANGE, name)]
    assert channel.queues[names.dead_letter].bindings == [
        (DEAD_LETTER_EXCHANGE, DEAD_LETTER_ROUTING_KEY)
    ]


@pytest.mark.asyncio
async def test_dispatcher_publishes_persistent_message_and_marks_outbox(
    client, session_factory, monkeypatch
):
    attempt = await _uploaded_attempt(client, session_factory)
    assert attempt.queue_task_id is not None
    monkeypatch.setattr(dispatcher, "SessionFactory", session_factory)
    exchange = FakeExchange(PROCESSING_EXCHANGE)

    assert await dispatcher._publish_next(exchange) is True

    message, routing_key, mandatory = exchange.published[0]
    assert routing_key == "preprocess.queue"
    assert mandatory is True
    assert message.delivery_mode == DeliveryMode.PERSISTENT
    payload = json.loads(message.body)
    assert message.message_id == attempt.queue_task_id
    assert routing_key == {
        "preprocess": "preprocess.queue",
        "transcription": "asr.queue",
        "diarization": "diar.queue",
    }[payload["stage"]]
    attempt_id = uuid.UUID(payload["attempt_id"])
    async with session_factory() as session:
        outbox = await session.scalar(
            select(BrokerOutboxMessage).where(
                BrokerOutboxMessage.id == uuid.UUID(message.message_id)
            )
        )
        stored_attempt = await session.get(ProcessingAttempt, attempt_id)
        assert outbox.published_at is not None
        assert outbox.publish_attempts == 1
        assert outbox.last_error is None
        assert stored_attempt.queue_task_id == message.message_id


@pytest.mark.asyncio
async def test_consumer_rejects_invalid_payload_to_dead_letter_queue(session_factory):
    message = FakeIncomingMessage(b"not-json")

    await handle_attempt_message(
        message=message,
        queue_name="preprocess.queue",
        session_factory=session_factory,
        handler=lambda _: None,
    )

    assert message.rejected == [False]
    assert message.acked == 0
    assert message.nacked == []


@pytest.mark.asyncio
async def test_consumer_requeues_delivery_when_queued_attempt_was_not_claimed(
    client, session_factory
):
    attempt = await _uploaded_attempt(client, session_factory)
    message = FakeIncomingMessage(
        json.dumps({"attempt_id": str(attempt.id)}).encode()
    )

    await handle_attempt_message(
        message=message,
        queue_name="preprocess.queue",
        session_factory=session_factory,
        handler=lambda _: _false(),
    )

    assert message.acked == 0
    assert message.rejected == []
    assert message.nacked == [True]


@pytest.mark.asyncio
async def test_consumer_acks_duplicate_delivery_after_attempt_completed(
    client, session_factory
):
    attempt = await _uploaded_attempt(client, session_factory)
    async with session_factory() as session:
        current = await session.get(ProcessingAttempt, attempt.id)
        current.status = ProcessingAttemptStatus.SUCCEEDED
        current.finished_at = datetime.now(timezone.utc)
        await session.commit()
    message = FakeIncomingMessage(
        json.dumps({"attempt_id": str(attempt.id)}).encode()
    )

    await handle_attempt_message(
        message=message,
        queue_name="preprocess.queue",
        session_factory=session_factory,
        handler=lambda _: _false(),
    )

    assert message.acked == 1
    assert message.rejected == []
    assert message.nacked == []


async def _false():
    return False


@pytest.mark.asyncio
async def test_consumer_dead_letters_terminal_failure_and_acks_scheduled_retry(
    client, session_factory
):
    attempt = await _uploaded_attempt(client, session_factory)

    async def fail_without_retry(attempt_id):
        async with session_factory() as session:
            current = await session.get(ProcessingAttempt, attempt_id)
            current.status = ProcessingAttemptStatus.FAILED
            current.finished_at = datetime.now(timezone.utc)
            await session.commit()
        return True

    terminal = FakeIncomingMessage(
        json.dumps({"attempt_id": str(attempt.id)}).encode()
    )
    await handle_attempt_message(
        message=terminal,
        queue_name="preprocess.queue",
        session_factory=session_factory,
        handler=fail_without_retry,
    )
    assert terminal.rejected == [False]

    async with session_factory() as session:
        retry = ProcessingAttempt(
            job_id=attempt.job_id,
            attempt_number=2,
            status=ProcessingAttemptStatus.QUEUED,
        )
        session.add(retry)
        await session.commit()

    retry_scheduled = FakeIncomingMessage(
        json.dumps({"attempt_id": str(attempt.id)}).encode()
    )
    await handle_attempt_message(
        message=retry_scheduled,
        queue_name="preprocess.queue",
        session_factory=session_factory,
        handler=lambda _: _true(),
    )
    assert retry_scheduled.acked == 1
    assert retry_scheduled.rejected == []


async def _true():
    return True


@pytest.mark.asyncio
async def test_consumer_nacks_uncommitted_unexpected_failure(client, session_factory):
    attempt = await _uploaded_attempt(client, session_factory)
    message = FakeIncomingMessage(
        json.dumps({"attempt_id": str(attempt.id)}).encode()
    )

    async def crash(_):
        raise RuntimeError("database connection lost")

    await handle_attempt_message(
        message=message,
        queue_name="preprocess.queue",
        session_factory=session_factory,
        handler=crash,
    )

    assert message.nacked == [True]
    assert message.acked == 0
    assert message.rejected == []
