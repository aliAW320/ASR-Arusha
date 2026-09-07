import asyncio
import inspect
from pathlib import Path
from typing import Any, Callable, Protocol

# Canonical definitions live in the dependency-free `alignment` package so
# app/services/processing.py (imported by every service's container, not
# just this one) can use them without pulling in this module's lazy
# torch/pyannote imports. Re-exported here so existing `from
# diarization.provider import DiarizationError, DiarizationTurn` call sites
# throughout this worker and its tests keep working unchanged.
from alignment.types import DiarizationError, DiarizationTurn  # noqa: F401


class DiarizationProvider(Protocol):
    async def diarize(self, audio_path: Path) -> list[DiarizationTurn]: ...


PipelineFactory = Callable[[str, str], Any]


class PyannoteLocalProvider:
    """Runs pyannote.audio in-process; no audio leaves the worker container."""

    def __init__(
        self,
        *,
        model_name: str,
        token: str,
        device: str = "cpu",
        pipeline_factory: PipelineFactory | None = None,
    ):
        if not token:
            raise DiarizationError(
                "HUGGINGFACE_TOKEN is not configured",
                code="diarization_configuration_error",
                retryable=False,
            )
        self.model_name = model_name
        self.token = token
        self.device = device
        self.pipeline_factory = pipeline_factory
        self._pipeline: Any | None = None

    def _load_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        try:
            if self.pipeline_factory is not None:
                pipeline = self.pipeline_factory(self.model_name, self.token)
            else:
                import torch
                from pyannote.audio import Pipeline

                auth_parameter = (
                    "token"
                    if "token" in inspect.signature(Pipeline.from_pretrained).parameters
                    else "use_auth_token"
                )
                pipeline = Pipeline.from_pretrained(
                    self.model_name,
                    **{auth_parameter: self.token},
                )
                if pipeline is None:
                    raise RuntimeError(
                        "Pyannote model could not be loaded; verify the Hugging Face "
                        "token and acceptance of the model conditions"
                    )
                pipeline.to(torch.device(self.device))
            self._pipeline = pipeline
            return pipeline
        except DiarizationError:
            raise
        except Exception as error:
            raise DiarizationError(
                f"Pyannote model loading failed: {error}",
                code="diarization_model_load_failed",
                retryable=False,
            ) from error

    def _diarize_sync(self, audio_path: Path) -> list[DiarizationTurn]:
        try:
            output = self._load_pipeline()(str(audio_path))
            annotation = getattr(output, "speaker_diarization", output)
            turns = [
                DiarizationTurn(
                    start_ms=max(0, round(float(turn.start) * 1000)),
                    end_ms=max(0, round(float(turn.end) * 1000)),
                    speaker_id=str(speaker),
                )
                for turn, _, speaker in annotation.itertracks(yield_label=True)
                if float(turn.end) > float(turn.start)
            ]
            return sorted(turns, key=lambda item: (item.start_ms, item.end_ms, item.speaker_id))
        except DiarizationError:
            raise
        except Exception as error:
            raise DiarizationError(
                f"Pyannote inference failed: {error}",
                code="diarization_inference_failed",
                retryable=True,
            ) from error

    async def preload(self) -> None:
        """Download and initialize the pipeline before the worker accepts jobs."""
        await asyncio.to_thread(self._load_pipeline)

    async def diarize(self, audio_path: Path) -> list[DiarizationTurn]:
        return await asyncio.to_thread(self._diarize_sync, audio_path)
