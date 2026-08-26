from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.router import api_router
from .config import get_settings
from .database import close_database
from .observability.events import LogEvent
from .observability.logging import configure_logging, get_logger
from .observability.middleware import RequestContextMiddleware


settings = get_settings()
configure_logging(
    service=settings.service_name,
    environment=settings.app_env,
    level=settings.log_level,
    json_output=settings.json_logs_enabled,
)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info(LogEvent.SERVICE_STARTED, "API service started")
    try:
        yield
    finally:
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
