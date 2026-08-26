import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse

from .context import get_log_context, reset_log_context, start_log_context
from .events import LogEvent
from .logging import get_logger


logger = get_logger(__name__)
VALID_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def _valid_or_generated(value: str | None) -> str:
    if value and VALID_ID.fullmatch(value):
        return value
    return str(uuid.uuid4())


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        request_id = _valid_or_generated(headers.get("x-request-id"))
        incoming_correlation = headers.get("x-correlation-id")
        correlation_id = (
            incoming_correlation
            if incoming_correlation and VALID_ID.fullmatch(incoming_correlation)
            else request_id
        )
        client = scope.get("client")
        token = start_log_context(
            request_id=request_id,
            correlation_id=correlation_id,
            client_ip=client[0] if client else None,
        )
        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_context(message):
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-Correlation-ID"] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        except Exception as error:
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            route = scope.get("route")
            logger.exception(
                LogEvent.UNHANDLED_EXCEPTION,
                "Unhandled application exception",
                error=error,
                method=scope.get("method"),
                route=getattr(route, "path", scope.get("path")),
                duration_ms=duration_ms,
                error_type=type(error).__name__,
            )
            if response_started:
                raise
            await JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "request_id": request_id},
            )(scope, receive, send_with_context)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            route = scope.get("route")
            path_template = getattr(route, "path", scope.get("path"))
            event_fields = {
                "method": scope.get("method"),
                "route": path_template,
                "status_code": status_code,
                "duration_ms": duration_ms,
            }
            context = get_log_context()
            event_fields.update(
                {key: context[key] for key in ("user_id", "meeting_id", "voice_id") if key in context}
            )
            if path_template == "/health":
                logger.debug(LogEvent.HTTP_REQUEST_COMPLETED, **event_fields)
            elif status_code >= 500:
                logger.error(LogEvent.HTTP_REQUEST_COMPLETED, **event_fields)
            elif status_code >= 400:
                logger.warning(LogEvent.HTTP_REQUEST_COMPLETED, **event_fields)
            else:
                logger.info(LogEvent.HTTP_REQUEST_COMPLETED, **event_fields)
            reset_log_context(token)
