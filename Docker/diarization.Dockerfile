FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 \
    && python -m pip install --no-cache-dir uv==0.12.3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY Docker/diarization.requirements.lock ./Docker/diarization.requirements.lock
RUN uv pip install \
    --python .venv/bin/python \
    --require-hashes \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    --index-strategy unsafe-best-match \
    --requirement Docker/diarization.requirements.lock

COPY fast_Backend/app ./app
COPY src/diarization ./diarization

CMD [".venv/bin/python", "-m", "diarization.bootstrap"]
