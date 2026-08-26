import json
import logging
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, TextIO

from .context import get_log_context


SENSITIVE_KEY_PARTS = {
    "api_key",
    "authorization",
    "body",
    "cookie",
    "credential",
    "headers",
    "password",
    "query",
    "secret",
    "token",
    "transcript",
}
BEARER_PATTERN = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
EMAIL_PATTERN = re.compile(r"(?<![\w.+-])([\w])[^\s@]*(@[\w.-]+\.[A-Za-z]{2,})(?![\w.-])")
URL_QUERY_PATTERN = re.compile(r"(https?://[^\s?]+)\?[^\s\"']+")


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _redact_string(value: str) -> str:
    value = BEARER_PATTERN.sub("Bearer ***", value)
    value = URL_QUERY_PATTERN.sub(r"\1?[REDACTED]", value)
    return EMAIL_PATTERN.sub(r"\1***\2", value)


def redact_data(value: Any, key: str | None = None) -> Any:
    if key and key.lower() == "email" and isinstance(value, str):
        return _redact_string(value) if EMAIL_PATTERN.search(value) else "***"
    if key and _is_sensitive_key(key):
        return "***"
    if isinstance(value, Mapping):
        return {str(item_key): redact_data(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _timestamp(record: logging.LogRecord) -> str:
    created = datetime.fromtimestamp(record.created, tz=timezone.utc)
    return created.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StructuredFormatter(logging.Formatter):
    def __init__(self, *, service: str, environment: str):
        super().__init__()
        self.service = service
        self.environment = environment

    def payload(self, record: logging.LogRecord) -> dict[str, Any]:
        event_fields = getattr(record, "event_fields", {})
        payload: dict[str, Any] = {
            "timestamp": _timestamp(record),
            "level": record.levelname,
            "service": self.service,
            "environment": self.environment,
            "event": getattr(record, "event", "log_message"),
        }
        reserved = {*payload, "message", "logger", "stack_trace"}
        for source in (get_log_context(), event_fields):
            payload.update({key: value for key, value in source.items() if key not in reserved})
        payload["message"] = _redact_string(record.getMessage())
        if record.name:
            payload["logger"] = record.name
        if record.exc_info:
            exception = record.exc_info[1]
            payload.setdefault("error_type", type(exception).__name__ if exception else "Exception")
            payload.setdefault("error_message", _redact_string(str(exception)) if exception else "")
            payload["stack_trace"] = _redact_string(self.formatException(record.exc_info))
        return redact_data(payload)


class JsonLogFormatter(StructuredFormatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self.payload(record), ensure_ascii=False, default=str)


class ConsoleLogFormatter(StructuredFormatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = self.payload(record)
        primary = " ".join(
            str(payload.pop(key))
            for key in ("timestamp", "level", "service", "event")
        )
        message = payload.pop("message", "")
        payload.pop("environment", None)
        payload.pop("logger", None)
        context = " ".join(f"{key}={value}" for key, value in payload.items())
        return " ".join(part for part in (primary, context, message) if part)


class EventLogger:
    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def log(
        self,
        level: int,
        event: str,
        message: str = "",
        *,
        exc_info=None,
        **fields: Any,
    ) -> None:
        self._logger.log(
            level,
            message or event,
            exc_info=exc_info,
            extra={"event": str(event), "event_fields": redact_data(fields)},
        )

    def debug(self, event: str, message: str = "", **fields: Any) -> None:
        self.log(logging.DEBUG, event, message, **fields)

    def info(self, event: str, message: str = "", **fields: Any) -> None:
        self.log(logging.INFO, event, message, **fields)

    def warning(self, event: str, message: str = "", **fields: Any) -> None:
        self.log(logging.WARNING, event, message, **fields)

    def error(self, event: str, message: str = "", **fields: Any) -> None:
        self.log(logging.ERROR, event, message, **fields)

    def exception(
        self,
        event: str,
        message: str = "",
        *,
        error: BaseException | None = None,
        **fields: Any,
    ) -> None:
        exc_info = (
            (type(error), error, error.__traceback__)
            if error is not None
            else True
        )
        self.log(logging.ERROR, event, message, exc_info=exc_info, **fields)


def get_logger(name: str) -> EventLogger:
    return EventLogger(logging.getLogger(name))


def configure_logging(
    *,
    service: str,
    environment: str,
    level: str = "INFO",
    json_output: bool = True,
    stream: TextIO | None = None,
) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    formatter_type = JsonLogFormatter if json_output else ConsoleLogFormatter
    handler.setFormatter(formatter_type(service=service, environment=environment))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # The middleware emits one canonical access event per request.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
