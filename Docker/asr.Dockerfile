FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

FROM python:3.11-slim

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY fast_Backend/app ./app
COPY src/asr ./asr
COPY src/alignment ./alignment
COPY src/meeting_composer ./meeting_composer

CMD ["uv", "run", "--locked", "--no-dev", "python", "-m", "asr.worker"]
