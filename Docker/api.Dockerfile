FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

FROM python:3.11-slim

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY alembic.ini ./
COPY alembic ./alembic
COPY fast_Backend/app ./app

EXPOSE 8000

CMD ["sh", "-c", "uv run --locked --no-dev alembic upgrade head && if [ -n \"${ADMIN_EMAIL:-}\" ] && [ -n \"${ADMIN_PASSWORD:-}\" ]; then uv run --locked --no-dev python -m app.cli.create_admin; fi && exec uv run --locked --no-dev uvicorn app.main:app --host 0.0.0.0 --port 8000"]
