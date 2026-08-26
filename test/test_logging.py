import io
import json
import logging
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.observability.context import reset_log_context, start_log_context
from app.observability.logging import configure_logging, get_logger
from app.observability.middleware import RequestContextMiddleware
from conftest import authorization, register_user


def _configure_json_logging() -> io.StringIO:
    stream = io.StringIO()
    configure_logging(
        service="test-api",
        environment="test",
        level="DEBUG",
        json_output=True,
        stream=stream,
    )
    return stream


def _events(stream: io.StringIO, event: str | None = None) -> list[dict]:
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    return [record for record in records if event is None or record["event"] == event]


def test_structured_logger_emits_contract_and_redacts_sensitive_values():
    stream = _configure_json_logging()
    token = start_log_context(request_id="request-1", correlation_id="correlation-1")
    try:
        get_logger("test.security").info(
            "security_check",
            "Login for private.user@example.com with Bearer raw-token",
            email="private.user@example.com",
            password="password-value",
            authorization="Bearer authorization-value",
            nested={"access_token": "token-value", "safe": "visible"},
            upstream_url="https://example.test/path?api_key=query-secret",
        )
    finally:
        reset_log_context(token)

    [record] = _events(stream, "security_check")
    assert {
        "timestamp",
        "level",
        "service",
        "environment",
        "event",
        "message",
        "logger",
    } <= record.keys()
    assert record["service"] == "test-api"
    assert record["environment"] == "test"
    assert record["request_id"] == "request-1"
    assert record["correlation_id"] == "correlation-1"
    assert record["email"] == "p***@example.com"
    assert record["password"] == "***"
    assert record["authorization"] == "***"
    assert record["nested"] == {"access_token": "***", "safe": "visible"}
    assert record["upstream_url"] == "https://example.test/path?[REDACTED]"
    serialized = json.dumps(record)
    for secret in (
        "private.user",
        "raw-token",
        "password-value",
        "authorization-value",
        "token-value",
        "query-secret",
    ):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_http_log_preserves_safe_ids_and_never_records_request_secrets():
    stream = _configure_json_logging()
    health_app = FastAPI()
    health_app.add_middleware(RequestContextMiddleware)

    @health_app.get("/health")
    async def health():
        return {"status": "ok"}

    async with AsyncClient(
        transport=ASGITransport(app=health_app), base_url="http://test"
    ) as health_client:
        response = await health_client.get(
            "/health",
            params={"password": "query-secret"},
            headers={
                "X-Request-ID": "request.safe-1",
                "X-Correlation-ID": "correlation:safe-1",
                "Authorization": "Bearer header-secret",
                "Cookie": "session=cookie-secret",
            },
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "request.safe-1"
    assert response.headers["x-correlation-id"] == "correlation:safe-1"
    [record] = _events(stream, "http_request_completed")
    assert record["level"] == "DEBUG"
    assert record["method"] == "GET"
    assert record["route"] == "/health"
    assert record["status_code"] == 200
    assert record["duration_ms"] >= 0
    assert record["request_id"] == "request.safe-1"
    assert record["correlation_id"] == "correlation:safe-1"
    assert record["client_ip"] == "127.0.0.1"
    serialized = stream.getvalue()
    for secret in ("query-secret", "header-secret", "cookie-secret"):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_http_log_generates_ids_and_binds_user_and_meeting_context(client):
    stream = _configure_json_logging()
    auth = await register_user(client, "logger@example.com")
    created = await client.post(
        "/meetings",
        json={"title": "Logging meeting"},
        headers={**authorization(auth), "X-Request-ID": "invalid id"},
    )

    assert created.status_code == 201
    generated_request_id = created.headers["x-request-id"]
    assert uuid.UUID(generated_request_id)
    assert created.headers["x-correlation-id"] == generated_request_id
    meeting_id = created.json()["id"]
    records = _events(stream, "http_request_completed")
    meeting_record = next(record for record in records if record["route"] == "/meetings")
    assert meeting_record["request_id"] == generated_request_id
    assert meeting_record["correlation_id"] == generated_request_id
    assert meeting_record["user_id"] == auth["user"]["id"]
    assert meeting_record["meeting_id"] == meeting_id


@pytest.mark.asyncio
async def test_unhandled_exception_is_safe_correlated_and_logged_once():
    stream = _configure_json_logging()
    failing_app = FastAPI()
    failing_app.add_middleware(RequestContextMiddleware)

    @failing_app.get("/explode")
    async def explode():
        raise RuntimeError("Bearer exception-secret")

    async with AsyncClient(
        transport=ASGITransport(app=failing_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as failing_client:
        response = await failing_client.get(
            "/explode",
            headers={"X-Request-ID": "error-request", "X-Correlation-ID": "error-flow"},
        )

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Internal server error",
        "request_id": "error-request",
    }
    assert response.headers["x-request-id"] == "error-request"
    assert response.headers["x-correlation-id"] == "error-flow"
    [failure] = _events(stream, "unhandled_exception")
    assert failure["error_type"] == "RuntimeError"
    assert failure["request_id"] == "error-request"
    assert "stack_trace" in failure
    assert "exception-secret" not in stream.getvalue()
    [completion] = _events(stream, "http_request_completed")
    assert completion["status_code"] == 500
    assert completion["level"] == "ERROR"


def test_console_logging_is_human_readable_without_losing_context():
    stream = io.StringIO()
    configure_logging(
        service="test-api",
        environment="development",
        level="INFO",
        json_output=False,
        stream=stream,
    )
    get_logger("test.console").info("service_ready", port=8000)

    line = stream.getvalue()
    assert "INFO test-api service_ready" in line
    assert "port=8000" in line
    assert not line.lstrip().startswith("{")
    assert logging.getLogger("uvicorn.access").disabled is True


def test_log_format_defaults_follow_environment_and_allow_override():
    common = {
        "database_url": "sqlite+aiosqlite://",
        "jwt_secret_key": "test-secret-key-that-is-at-least-32-characters",
    }
    assert Settings(app_env="development", **common).json_logs_enabled is False
    assert Settings(app_env="test", **common).json_logs_enabled is True
    assert Settings(app_env="production", **common).json_logs_enabled is True
    assert Settings(app_env="production", log_format="console", **common).json_logs_enabled is False
