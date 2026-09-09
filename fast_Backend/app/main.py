import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.router import api_router
from .config import Settings, get_settings
from .database import close_database
from .messaging import dispatcher
from .observability.events import LogEvent
from .observability.logging import configure_logging, get_logger
from .observability.middleware import RequestContextMiddleware

import asr.worker as asr_worker
import cleaner.worker as cleaner_worker
import mcp_worker.worker as mcp_worker_worker
import meeting_composer.worker as meeting_composer_worker
import preprocessing.worker as preprocessing_worker


settings = get_settings()
configure_logging(
    service=settings.service_name,
    environment=settings.app_env,
    level=settings.log_level,
    json_output=settings.json_logs_enabled,
)
logger = get_logger(__name__)

# Every processing worker except diarization (heavy, separate ML deps) runs
# as a background task inside this same process instead of its own
# container/Dockerfile. Each `run` coroutine owns no process-wide state
# (logging config, DB engine) of its own -- that stays centralized here so
# one worker's lifecycle can't tear down state the others still depend on.
WorkerRunner = Callable[[Settings], Awaitable[None]]
BACKGROUND_WORKERS: tuple[tuple[str, WorkerRunner], ...] = (
    ("broker-dispatcher", dispatcher.run),
    ("preprocess-worker", preprocessing_worker.run),
    ("asr-worker", asr_worker.run),
    ("cleaner-worker", cleaner_worker.run),
    ("meeting-composer-worker", meeting_composer_worker.run),
    ("mcp-worker", mcp_worker_worker.run),
)


async def _run_background_worker(name: str, run: WorkerRunner, settings: Settings) -> None:
    restart_count = 0
    while True:
        try:
            await run(settings)
            logger.warning(
                "background_worker_stopped",
                "Background worker stopped unexpectedly and will be restarted",
                worker=name,
                restart_count=restart_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception(
                "background_worker_crashed",
                "Background worker task crashed and will be restarted",
                error=error,
                worker=name,
                restart_count=restart_count,
                restart_delay_seconds=settings.rabbitmq_retry_delay_seconds,
            )

        restart_count += 1
        await asyncio.sleep(settings.rabbitmq_retry_delay_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info(LogEvent.SERVICE_STARTED, "API service started")
    tasks = [
        asyncio.create_task(_run_background_worker(name, run, settings), name=name)
        for name, run in BACKGROUND_WORKERS
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_database()
        logger.info(LogEvent.SERVICE_STOPPED, "API service stopped")


app = FastAPI(
    title="Persian Meeting Audio Processing API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(RequestContextMiddleware)
app.include_router(api_router)


@app.get("/", tags=["root"])
async def root():
    pass
