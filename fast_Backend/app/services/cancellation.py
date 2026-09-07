import asyncio
import uuid
from collections.abc import Awaitable
from contextlib import suppress
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import ProcessingAttempt, ProcessingAttemptStatus


T = TypeVar("T")


class ProcessingCancelled(Exception):
    """Raised when the durable processing attempt was cancelled or deleted."""


async def attempt_is_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
    attempt_id: uuid.UUID,
) -> bool:
    async with session_factory() as session:
        attempt = await session.get(ProcessingAttempt, attempt_id)
        return attempt is None or attempt.status == ProcessingAttemptStatus.CANCELLED


async def run_cancellable(
    operation: Awaitable[T],
    *,
    session_factory: async_sessionmaker[AsyncSession],
    attempt_id: uuid.UUID,
    poll_interval_seconds: float,
) -> T:
    """Run work while treating PostgreSQL as the cancellation source of truth."""
    task = asyncio.ensure_future(operation)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll_interval_seconds)
            if done:
                return task.result()
            if await attempt_is_cancelled(session_factory, attempt_id):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise ProcessingCancelled("Processing attempt was cancelled")
    except BaseException:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        raise
