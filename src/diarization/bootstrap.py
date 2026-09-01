import asyncio

from app.config import Settings, get_settings
from app.observability.logging import configure_logging, get_logger

from .provider import DiarizationError, PyannoteLocalProvider
from .worker import run_forever


logger = get_logger(__name__)


def build_provider(settings: Settings) -> PyannoteLocalProvider:
    token = settings.huggingface_token
    return PyannoteLocalProvider(
        model_name=settings.diarization_model_name,
        token=token.get_secret_value() if token else "",
        device=settings.diarization_device,
    )


async def preload_model(provider: PyannoteLocalProvider) -> None:
    logger.info(
        "diarization_model_download_started",
        "Downloading and loading the Pyannote diarization model",
        model=provider.model_name,
        device=provider.device,
    )
    try:
        await provider.preload()
    except DiarizationError as error:
        logger.exception(
            "diarization_model_download_failed",
            "Pyannote model download or loading failed",
            error=error,
            error_code=error.code,
            model=provider.model_name,
        )
        raise
    logger.info(
        "diarization_model_download_succeeded",
        "Pyannote diarization model is ready",
        model=provider.model_name,
        device=provider.device,
    )


async def main() -> None:
    settings = get_settings()
    configure_logging(
        service="diarization-worker",
        environment=settings.app_env,
        level=settings.log_level,
        json_output=settings.json_logs_enabled,
    )
    provider = build_provider(settings)
    await preload_model(provider)
    await run_forever(
        provider_factory=lambda _item: provider,
        configure_worker_logging=False,
    )


if __name__ == "__main__":
    asyncio.run(main())
