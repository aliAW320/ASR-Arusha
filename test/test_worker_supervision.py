import asyncio

import pytest

from app.config import get_settings
from app.main import _run_background_worker


@pytest.mark.asyncio
async def test_background_worker_is_restarted_after_transient_startup_failure():
    second_run_started = asyncio.Event()
    calls = 0

    async def unstable_worker(_settings):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("broker is still starting")
        second_run_started.set()
        await asyncio.Future()

    settings = get_settings().model_copy(
        update={"rabbitmq_retry_delay_seconds": 0.0}
    )
    supervisor = asyncio.create_task(
        _run_background_worker("test-worker", unstable_worker, settings)
    )

    try:
        await asyncio.wait_for(second_run_started.wait(), timeout=1)
        assert calls == 2
        assert not supervisor.done()
    finally:
        supervisor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await supervisor


@pytest.mark.asyncio
async def test_background_worker_cancellation_does_not_restart_it():
    started = asyncio.Event()
    calls = 0

    async def worker(_settings):
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.Future()

    supervisor = asyncio.create_task(
        _run_background_worker("test-worker", worker, get_settings())
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    supervisor.cancel()

    with pytest.raises(asyncio.CancelledError):
        await supervisor
    assert calls == 1
