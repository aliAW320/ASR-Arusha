# Hamneshin — Meeting Intelligence Platform

Hamneshin (هم‌نشین) turns meeting recordings into published knowledge-base
documents. You upload audio and supporting files; the platform normalises the
audio, transcribes it, optionally separates speakers, cleans the text,
composes one transcript per meeting, summarises it, and publishes the result
to an Outline knowledge base — without further manual steps.

The backend is FastAPI on PostgreSQL, MinIO and RabbitMQ. No machine-learning
runtime or model weight lives in the API image: speech recognition and text
cleaning are reached over an OpenAI-compatible HTTP API, and speaker
diarization runs in its own optional container.

---

## Contents

- [Architecture](#architecture)
- [Processing pipeline](#processing-pipeline)
- [Running the stack](#running-the-stack)
- [Configuration](#configuration)
- [Creating the first administrator](#creating-the-first-administrator)
- [Permissions](#permissions)
- [HTTP API](#http-api)
- [Web interface](#web-interface)
- [Storage layout](#storage-layout)
- [Messaging and durability](#messaging-and-durability)
- [Logging and correlation](#logging-and-correlation)
- [Database migrations](#database-migrations)
- [Tests](#tests)
- [CI/CD](#cicd)

---

## Architecture

Six containers, defined in `docker-compose.yml`:

| Service | Image source | Role |
|---|---|---|
| `ui` | `Docker/ui.Dockerfile` | Nginx serving the static Persian RTL front end, proxying `/api` to the backend |
| `api` | `Docker/api.Dockerfile` | FastAPI application **and** all background workers |
| `diarization` | `Docker/diarization.Dockerfile` | Speaker diarization (pyannote); optional |
| `postgres` | `postgres` | System of record |
| `minio` | `minio` | Object storage for audio, attachments and transcripts |
| `rabbitmq` | `rabbitmq` | Work queues |

**The API process also runs the workers.** `broker-dispatcher`,
`preprocess-worker`, `asr-worker`, `cleaner-worker`,
`meeting-composer-worker` and `mcp-worker` are asyncio background tasks
supervised inside the API process — see `app.main.BACKGROUND_WORKERS`. Each is
restarted automatically if it stops.

Diarization is the one exception. Its torch/pyannote dependencies are far too
heavy to carry in the API image, so it ships as a separate container, and the
platform treats it as **optional at runtime** rather than assumed — see
[Optional diarization](#optional-diarization).

### The ML boundary

The API image contains no model runtime and no model weights. Transcription
and cleaning go through an HTTP adapter to an external OpenAI-compatible
endpoint (`BASE_URL`). Only the `diarization` service loads a model, and it
has its own manifest, Dockerfile and process.

---

## Processing pipeline

```
upload ──▶ preprocess ──▶ transcription ──┐
                                          ├──▶ alignment ──▶ cleaning ──▶ meeting compose ──▶ summarise + publish
                          diarization ────┘
                          (optional)
```

Uploading audio creates a `Result` and queues `preprocess`, which converts the
file to canonical 16 kHz mono PCM WAV. Transcription and diarization then run
concurrently; a durable PostgreSQL barrier releases a single `cleaning` job
once both have finished. When every voice in a meeting has been cleaned, the
composer merges them into one meeting transcript, and publication follows
immediately — summarising the transcript and writing it to the knowledge base
over MCP. No manual approval gate stands anywhere in this chain.

Processing stages are `preprocess`, `transcription`, `diarization`,
`alignment`, `cleaning`, `meeting_compose` and `minutes_generation`.

### Optional diarization

Diarization needs its own GPU-capable container, which may or may not be
running. Rather than assume it, the backend decides per voice at upload time
and records the answer on `VoiceFile.diarization_decision`:

| Decision | Meaning |
|---|---|
| `unavailable` | No diarization worker was alive; the voice goes straight from transcription to cleaning |
| `pending` | A worker is alive; the pipeline waits for the user to answer before cleaning anything |
| `enabled` | The user asked for speaker separation |
| `skipped` | The user declined it |

Availability is a liveness question, not a configuration flag: the diarization
worker writes a heartbeat to `service_heartbeats`, and
`GET /processing/diarization` reports whether that heartbeat is fresher than
`DIARIZATION_HEARTBEAT_TTL_SECONDS`.

Holding the pipeline on `pending` matters. Cleaning the raw ASR text
immediately would discard the speaker-attributed transcript the user may still
ask for, so nothing downstream runs until the question is answered. Runs
without diarization still fill the same `speaker-transcript/v1` artifact slot,
labelling every segment `SPEAKER_UNKNOWN`, so the cleaner, composer and
publication stages need no special case.

---

## Running the stack

Requirements: Docker with Compose. For running tests or tooling outside
Docker, Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
# edit .env — at minimum JWT_SECRET_KEY, the PostgreSQL and MinIO
# credentials, TRANSCRIPT_API_KEY and KB_API_KEY
docker compose up --build
```

The API container runs `alembic upgrade head` before starting FastAPI.

Once healthy:

| Endpoint | URL |
|---|---|
| Web interface | http://127.0.0.1:3000 |
| API | http://127.0.0.1:8000 |
| OpenAPI / Swagger | http://127.0.0.1:8000/docs |
| MinIO console | http://127.0.0.1:9001 |
| RabbitMQ management | http://127.0.0.1:15672 |

PostgreSQL is published on host loopback port `5252` only; containers still
reach it on `5432` internally.

To run without diarization, simply omit the service — the pipeline detects the
missing heartbeat and routes around it:

```bash
docker compose up -d ui api postgres minio rabbitmq
```

---

## Configuration

All settings come from the environment; `.env.example` is the authoritative
list. The groups that matter most:

```text
# Runtime
APP_ENV=development          # development | test | production
LOG_LEVEL=INFO
LOG_FORMAT=auto              # auto | json | console
API_PORT=8000
UI_PORT=3000

# Speech recognition (OpenAI-compatible endpoint)
BASE_URL=https://example.com/v1
TRANSCRIPT_API_KEY=...
TRANSCRIPT_MODEL_NAME=whisper-large-v3-persian
ASR_REQUEST_TIMEOUT_SECONDS=600
ASR_NUM_BEAMS=5
ASR_MAX_ATTEMPTS=3

# Speaker diarization (optional service)
DIARIZATION_ENABLED=true
DIARIZATION_MODEL_NAME=pyannote/speaker-diarization-community-1
DIARIZATION_DEVICE=cpu
DIARIZATION_HEARTBEAT_INTERVAL_SECONDS=15
DIARIZATION_HEARTBEAT_TTL_SECONDS=60
HUGGINGFACE_TOKEN=...

# Text cleaning and summarisation
CLEANER_MODEL_NAME=openai/Qwen3.8-27B
CLEANER_CHUNK_MAX_CHARS=12000
SUMMARY_MODEL_NAME=openai/Qwen3.8-27B
SUMMARY_MULTIMODAL_ENABLED=true

# Knowledge base (Outline over MCP)
KB_BASE_URL=https://kb.arusha.dev
KB_API_KEY=...
MEETING_PUBLICATION_DEFAULT_PATH=...

# Upload limits
VOICE_UPLOAD_MAX_BYTES=524288000        # 500 MB per audio file
MEETING_ATTACHMENT_MAX_BYTES=52428800   # 50 MB per attachment
```

Every stage has its own `*_MAX_ATTEMPTS`, `*_REQUEST_TIMEOUT_SECONDS` and
`*_WORKER_NAME`, so retry behaviour is tunable per stage without code changes.

---

## Creating the first administrator

Set the credentials in `.env`:

```text
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=a-strong-password
ADMIN_FULL_NAME=Administrator
```

Then run the idempotent bootstrap command:

```bash
docker compose exec api uv run python -m app.cli.create_admin
```

Public registration always creates a `USER`. A user's global role is
independent of their role in any given meeting.

---

## Permissions

Two independent layers: a global role (`USER` or `ADMIN`) and a per-meeting
role.

| Meeting role | View | Edit meeting | Manage voices | Manage members | Delete meeting |
|---|:-:|:-:|:-:|:-:|:-:|
| `OWNER` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `CONTRIBUTOR` | ✓ | ✓ | ✓ | — | — |
| `VIEWER` | ✓ | — | — | — | — |
| `ADMIN` (global) | all meetings | ✓ | ✓ | ✓ | ✓ |

The matrix lives in `app/services/permissions.py`, deliberately centralised so
that changing a role's reach never leaks into individual routes.

Accounts are modelled as an internal `User` separate from the `AuthIdentity`
used to sign in, so a central identity provider can be added later without
touching the account model.

---

## HTTP API

Interactive documentation, including every query parameter and schema, is
served at `/docs`.

```text
# Authentication
POST   /auth/register
POST   /auth/login
GET    /auth/me

# Meetings and membership
GET    /meetings
POST   /meetings
GET    /meetings/{meeting_id}
PATCH  /meetings/{meeting_id}
DELETE /meetings/{meeting_id}
GET    /meetings/{meeting_id}/members
POST   /meetings/{meeting_id}/members
PATCH  /meetings/{meeting_id}/members/{user_id}
DELETE /meetings/{meeting_id}/members/{user_id}

# Uploads — audio and attachments in one request
POST   /meetings/{meeting_id}/files
POST   /meetings/{meeting_id}/voices
GET    /meetings/{meeting_id}/voices
GET    /voices/{voice_id}
DELETE /voices/{voice_id}
GET    /voices                                   # admin only
GET    /meetings/{meeting_id}/attachments
POST   /meetings/{meeting_id}/attachments
DELETE /meetings/{meeting_id}/attachments/{attachment_id}

# Processing
POST   /meetings/{meeting_id}/process            # 202 Accepted
GET    /meetings/{meeting_id}/processing
GET    /processing/jobs/{job_id}
GET    /processing/diarization                   # is a worker alive?
POST   /voices/{voice_id}/diarization            # answer yes/no, once per voice

# Per-voice results
GET    /voices/{voice_id}/results
GET    /results/{result_id}
GET    /results/{result_id}/transcript

# Composed meeting transcript
POST   /meetings/{meeting_id}/compose
GET    /meetings/{meeting_id}/results
GET    /meetings/{meeting_id}/transcript
GET    /meeting-results/{meeting_result_id}
GET    /meeting-results/{meeting_result_id}/transcript

# Knowledge-base publication
GET    /meetings/{meeting_id}/publication
PATCH  /meetings/{meeting_id}/publication        # change destination path

# Speakers
GET    /speakers
POST   /speakers/register
GET    /speakers/{speaker_id}
PATCH  /speakers/{speaker_id}
DELETE /speakers/{speaker_id}
POST   /speakers/{speaker_id}/voice-samples

# Audit trail
GET    /history                                  # admin only, append-only

GET    /health
```

`POST /meetings/{meeting_id}/files` accepts audio and attachments together in
a single multipart request, sorting each file by content type and magic bytes.
Accepted attachment types are PNG, JPEG and PDF.

---

## Web interface

A multi-page Persian RTL interface with no build step: plain ES modules,
served directly by Nginx from `ui/`. Nginx proxies `/api` to the backend, so
the browser only ever talks to the UI's own origin and the backend needs no
CORS configuration.

```text
ui/login.html               ui/js/auth-page.js
ui/register.html            ui/js/auth-page.js
ui/meetings.html            ui/js/meetings-page.js
ui/meeting.html             ui/js/meeting-page.js          # uploads, pipeline, members
ui/transcript.html          ui/js/transcript-page.js       # one voice, before/after cleaning
ui/meeting-transcript.html  ui/js/meeting-transcript-page.js
ui/history.html             ui/js/history-page.js
ui/js/api.js                # backend calls and session handling
ui/js/layout.js             # shell, sidebar, shared helpers
ui/js/icons.js              # inline SVG sprite
ui/styles.css               # design system
ui/pages.css                # page-specific styles
```

Current capabilities: registration and sign-in; meeting CRUD; member
management; batch upload of audio and attachments staged behind an explicit
"start processing" action; live pipeline status per stage with the failing
stage and error code surfaced; per-voice transcripts shown before and after
cleaning; the composed meeting transcript; publication status and destination;
and a filterable audit trail for administrators.

Every request carries an `X-Request-ID`, which ties UI actions to backend logs
and history entries.

---

## Storage layout

Audio and attachments never travel through the message broker; queue messages
carry PostgreSQL identifiers only. Objects use deterministic keys, and their
bucket, key, checksum and producing job are recorded in PostgreSQL:

```text
meetings/{meeting_id}/results/{result_id}/transcript.json
meetings/{meeting_id}/results/{result_id}/raw.txt
```

Artifact types are `normalized_audio`, `transcript_json`, `raw_text`,
`word_timestamps_json`, `diarization_json`, `aligned_transcript_json`,
`cleaned_text`, `metadata` and `other`.

Metadata registration is retried three times; if it ultimately fails, the
orphaned object is deleted from MinIO rather than left behind.

---

## Messaging and durability

Five durable work queues — `preprocess.queue`, `asr.queue`, `diar.queue`,
`cleaning.queue`, `mcp.queue` — all bound to a shared dead-letter exchange
feeding `processing.dlq`.

The platform uses a **transactional outbox**. A job, its first attempt and the
broker message are written in one database transaction, so a RabbitMQ outage
cannot lose work: the dispatcher publishes outbox rows with publisher
confirms once the broker returns. Workers consume with `prefetch=1` and
acknowledge only after the result is durably stored in PostgreSQL and MinIO.

Retries are visible rather than hidden. Transient failures create a new
`ProcessingAttempt`, up to the stage's `*_MAX_ATTEMPTS`, with a configurable
delay. A permanent failure is rejected with `requeue=false`, lands in
`processing.dlq`, and keeps its error code and message on the attempt row so
the API and UI can show exactly which stage failed and why. Duplicate
deliveries are idempotent, guarded by attempt status and a unique outbox key.

Deleting a voice cancels its `queued` and `running` attempts and removes any
unpublished outbox rows. Running workers poll PostgreSQL for cancellation and
will neither store their result nor advance to the next stage. A message
already published to RabbitMQ cannot be selectively withdrawn; it is
acknowledged as stale on delivery without doing the work.

---

## Logging and correlation

Every request gets an `X-Request-ID` and an `X-Correlation-ID`, both returned
in the response headers. A client-supplied identifier is preserved if it is at
most 100 characters from `A-Z a-z 0-9 . _ : -`; otherwise a UUID is generated.
An absent correlation ID defaults to the request ID.

In `test` and `production` each stdout line is a standalone JSON object:

```text
timestamp, level, service, environment, event, message
request_id, correlation_id, client_ip
user_id, meeting_id, voice_id        # when present in context
method, route, status_code, duration_ms
```

`route` is the route pattern, and query strings are not recorded. Request
bodies, query parameters, cookies, `Authorization` headers, passwords, tokens,
secrets, transcripts and raw headers are never logged; email addresses are
masked. An unhandled exception is logged once with its stack trace and
context, and the client receives a generic 500 carrying the request ID.
`/health` logs at `DEBUG` so it stays quiet in production.

With `LOG_FORMAT=auto`, development prints human-readable output while test
and production emit JSON. The application manages no log files and no
rotation — container infrastructure can ship stdout/stderr to Loki,
OpenSearch or similar. `trace_id` is not emitted until real tracing is added.

`GET /history` is a separate, append-only business audit trail that shares
`request_id` and `correlation_id` with the operational log, and can be
filtered by event, actor, request ID or correlation ID.

---

## Database migrations

```bash
uv run alembic upgrade head
```

The initial migration both creates a fresh database and adopts a legacy one
built by `Base.metadata.create_all`, upgrading it in place without dropping
existing users, credentials, meetings or history. `create_all` is no longer
called at startup.

---

## Tests

Tests are organised by domain in `test/`:

```text
test_auth.py                 test_meetings.py            test_voices.py
test_security.py             test_history.py             test_logging.py
test_preprocessing.py        test_asr.py                 test_diarization.py
test_diarization_optional.py test_cleaner.py             test_processing.py
test_meeting_composer.py     test_publication.py         test_auto_publication.py
test_mcp_worker.py           test_messaging.py           test_worker_supervision.py
test_migrations.py           test_architecture.py        test_ui.py
test_compose_integration.py  # needs a live stack
test_asr_benchmark.py        # manual only, excluded from CI
```

Full suite:

```bash
uv run pytest -q -m "not asr_benchmark" test/
```

The live quality benchmark runs manually against the first 50 samples and
sends real audio to the configured external API:

```bash
RUN_ASR_BENCHMARK=1 uv run pytest -q -m asr_benchmark test/test_asr_benchmark.py
```

WER and CER are computed corpus-level after Persian normalisation, with
thresholds of `WER < 25%` and `CER < 8%`. This test is deliberately excluded
from CI.

---

## CI/CD

`.github/workflows/ci-cd.yml` runs the Python suite first, then builds the
`api`, `ui` and `diarization` images in a matrix. The Compose integration
test runs against real PostgreSQL, MinIO and RabbitMQ containers, for the API
image only. On a push to `main` or a version tag, the images that passed those
tests are published to GHCR:

```text
ghcr.io/<owner>/asr-arusha-api
ghcr.io/<owner>/asr-arusha-ui
ghcr.io/<owner>/asr-arusha-diarization
```

Pull requests build and test without publishing. The integration job brings up
only `postgres minio rabbitmq api`, so the independently built UI image cannot
interfere with backend testing.
