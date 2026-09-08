# Persian Meeting Platform

Backend پایه سامانه Meeting Intelligence با FastAPI، PostgreSQL و MinIO به‌همراه UI و worker مستقل ASR. هیچ runtime یا کتابخانه ML در image بک‌اند نصب نمی‌شود و ارتباط ASR فقط از طریق API سازگار با OpenAI انجام می‌شود.

## قابلیت‌های این فاز

- ثبت‌نام و ورود با پاسخ یکسان شامل access token و اطلاعات کاربر
- جداسازی حساب داخلی `User` از هویت ورود `AuthIdentity` برای اتصال آینده به احراز هویت مرکزی
- نقش سراسری `USER` و `ADMIN`
- bootstrap کنترل‌شده و idempotent ادمین اولیه
- CRUD جلسه و مدیریت اعضای ثبت‌شده
- نقش‌های جلسه `OWNER`، `CONTRIBUTOR` و `VIEWER` با policy متمرکز و قابل توسعه
- آپلود multipart صوت تا سقف پیش‌فرض ۵۰۰ مگابایت در MinIO
- نگهداری bucket/key/checksum و metadata آپلود در PostgreSQL
- سه بار تلاش برای ثبت metadata و حذف object از MinIO پس از شکست نهایی
- audit log غیرقابل حذف برای تغییرات business و رخدادهای احراز هویت/امنیتی
- structured logging روی stdout/stderr با JSON در production/test و خروجی خوانا در development
- حفظ `request_id` و `correlation_id` در پاسخ HTTP، Log و History
- migration با Alembic؛ شامل پذیرش schema قدیمی ساخته‌شده توسط `create_all`
- شروع صریح پردازش Meeting و اجرای asynchronous تبدیل صوت به متن در سرویس مستقل ASR
- سه تلاش پایدار و قابل مشاهده برای خطاهای موقت ASR و توقف فوری برای خطاهای دائمی
- ذخیره transcript استاندارد و متن خام در MinIO و provenance آن‌ها در PostgreSQL

## اجرا با Docker Compose

تنظیمات را آماده کنید:

```bash
cp .env.example .env
```

حداقل `JWT_SECRET_KEY`، اطلاعات PostgreSQL و MinIO را تغییر دهید. سپس:

```bash
docker compose up --build
```

`docker-compose.yml` طبق قرارداد پروژه در ریشه است و Dockerfile بک‌اند در `Docker/api.Dockerfile` قرار دارد. container API پیش از شروع FastAPI، `alembic upgrade head` را اجرا می‌کند.
PostgreSQL فقط روی loopback میزبان و پورت `5252` منتشر می‌شود؛ ارتباط داخلی containerها همچنان از پورت `5432` استفاده می‌کند.

پس از آماده‌شدن سرویس‌ها:

- UI در `http://127.0.0.1:3000`
- API در `http://127.0.0.1:8000`
- Swagger در `http://127.0.0.1:8000/docs`

## UI مستقل

UI یک رابط فارسی و RTL چندصفحه‌ای است که به‌عنوان سرویس مستقل Nginx اجرا می‌شود. Dockerfile آن در `Docker/ui.Dockerfile`، تنظیم Nginx در `Docker/ui.nginx.conf` و source آن در پوشه `ui/` قرار دارد. مسیر `/api` در Nginx به سرویس backend پروکسی می‌شود؛ بنابراین مرورگر فقط با origin خود UI ارتباط دارد و نیازی به فعال‌کردن CORS در backend نیست.

صفحات و منطق هر حوزه جدا نگهداری می‌شوند:

```text
ui/login.html             + ui/js/auth-page.js
ui/register.html          + ui/js/auth-page.js
ui/meetings.html          + ui/js/meetings-page.js
ui/meeting.html           + ui/js/meeting-page.js
ui/history.html           + ui/js/history-page.js
ui/js/api.js              # ارتباط مشترک با backend و session
ui/js/layout.js           # layout، sidebar و ابزارهای نمایشی مشترک
```

قابلیت‌های فعلی UI:

- ثبت‌نام و ورود و نگهداری نشست
- فهرست، ساخت، ویرایش و حذف Meeting
- مشاهده، افزودن، تغییر نقش و حذف اعضا
- آپلود، مشاهده و حذف فایل صوتی
- نمایش و فیلتر History برای ادمین
- ارسال `X-Request-ID` برای اتصال درخواست‌های UI به Logging و History

پورت UI از `UI_PORT` قابل تغییر است. UI وابستگی runtime یا build به Node ندارد و فایل‌های استاتیک مستقیماً توسط Nginx ارائه می‌شوند.

## ساخت یا به‌روزرسانی ادمین اولیه

در `.env` مقدارهای زیر را تنظیم کنید:

```text
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=a-strong-password
ADMIN_FULL_NAME=Administrator
```

سپس فرمان idempotent زیر را اجرا کنید:

```bash
docker compose exec api uv run python -m app.cli.create_admin
```

ثبت‌نام عمومی همیشه کاربر با نقش `USER` می‌سازد. نقش سراسری کاربر از نقش او در یک Meeting مستقل است.

## ماتریس دسترسی Meeting

| نقش | مشاهده | ویرایش Meeting | مدیریت Voice | مدیریت اعضا | حذف Meeting |
|---|---:|---:|---:|---:|---:|
| OWNER | بله | بله | بله | بله | بله |
| CONTRIBUTOR | بله | بله | بله | خیر | خیر |
| VIEWER | بله | خیر | خیر | خیر | خیر |
| ADMIN | همه Meetingها | بله | بله | بله | بله |

ماتریس در `app/services/permissions.py` متمرکز است تا تغییر نقش‌ها در آینده به routeها نشت نکند.

## APIهای اصلی

```text
POST   /auth/register
POST   /auth/login
GET    /auth/me

POST   /meetings
GET    /meetings
GET    /meetings/{meeting_id}
PATCH  /meetings/{meeting_id}
DELETE /meetings/{meeting_id}

GET    /meetings/{meeting_id}/members
POST   /meetings/{meeting_id}/members
PATCH  /meetings/{meeting_id}/members/{user_id}
DELETE /meetings/{meeting_id}/members/{user_id}

POST   /meetings/{meeting_id}/voices
GET    /meetings/{meeting_id}/voices
GET    /voices/{voice_id}
DELETE /voices/{voice_id}
GET    /voices                         # admin only

POST   /meetings/{meeting_id}/process # 202 Accepted
GET    /meetings/{meeting_id}/processing
GET    /processing/jobs/{job_id}
GET    /voices/{voice_id}/results
GET    /results/{result_id}

GET    /history                        # admin only, immutable
```

Swagger UI پس از اجرا در `/docs` در دسترس است.

## Logging و Correlation

هر request یک `X-Request-ID` و `X-Correlation-ID` معتبر دریافت می‌کند. اگر client شناسه‌ای با حداکثر ۱۰۰ کاراکتر از مجموعه `A-Z a-z 0-9 . _ : -` بفرستد، همان شناسه حفظ می‌شود؛ در غیر این صورت UUID جدید ساخته می‌شود. اگر correlation ارسال نشود، مقدار request ID را می‌گیرد. هر دو شناسه در header پاسخ نیز برگردانده می‌شوند.

در production و test هر خط stdout یک JSON مستقل با قرارداد پایه زیر است:

```text
timestamp, level, service, environment, event, message
request_id, correlation_id, client_ip
user_id, meeting_id, voice_id       # در صورت وجود context
method, route, status_code, duration_ms
```

`route` الگوی route است و query string در access log ثبت نمی‌شود. body، query، Cookie، Authorization، password، token، secret، transcript و headerهای خام ثبت نمی‌شوند؛ email نیز mask می‌شود. Exception مدیریت‌نشده یک‌بار همراه stack trace و context ثبت می‌شود و پاسخ عمومی ۵۰۰ شامل request ID است. رخداد `/health` در سطح `DEBUG` ثبت می‌شود تا در production پرحجم نباشد.

تنظیمات قابل تغییر:

```text
APP_ENV=development        # development | test | production
SERVICE_NAME=meeting-api
LOG_LEVEL=INFO
LOG_FORMAT=auto            # auto | json | console
```

در حالت `auto`، development خروجی console و test/production خروجی JSON دارند. برنامه فایل log و rotation مدیریت نمی‌کند؛ زیرساخت container می‌تواند stdout/stderr را بعداً به Loki/OpenSearch یا سامانه مشابه ارسال کند. `trace_id` تا زمان اضافه‌شدن tracing واقعی تولید نمی‌شود.

History یک audit trail تجاری جدا از log عملیاتی است، ولی `request_id` و `correlation_id` مشترک دارد. ادمین می‌تواند `GET /history` را علاوه بر event/actor با queryهای `request_id` و `correlation_id` فیلتر کند.

## پردازش صوت و RabbitMQ

آپلود موفق Voice به‌صورت خودکار یک Result و دو Job مستقل برای ASR و diarization ایجاد می‌کند. ایجاد Job، Attempt و پیام outbox در همان transaction دیتابیس انجام می‌شود؛ بنابراین قطع‌شدن RabbitMQ باعث گم‌شدن کار نمی‌شود. حلقهٔ dispatch پیام‌های outbox را با publisher-confirm به RabbitMQ می‌فرستد و workerها با `prefetch=1` آن‌ها را مصرف می‌کنند. به‌جز diarization (که به دلیل وابستگی سنگین torch/pyannote container/Dockerfile جدا دارد)، dispatcher و همهٔ workerها (asr، cleaner، meeting-composer، mcp) به‌صورت background task داخل همان process که API را serve می‌کند اجرا می‌شوند -- به `app.main.BACKGROUND_WORKERS` نگاه کنید.

چهار صف durable عبارت‌اند از `asr.queue`، `diar.queue`، `cleaning.queue` و `mcp.queue`. هر چهار صف به DLX مشترک متصل‌اند. فایل صوتی داخل broker قرار نمی‌گیرد و پیام فقط شناسه‌های PostgreSQL را حمل می‌کند. ASR و diarization هم‌زمان اجرا می‌شوند؛ barrier پایدار PostgreSQL بعد از موفقیت هر دو فقط یک Job cleaning ایجاد می‌کند. cleaner دیگر مستقیماً به `mcp.queue` پیام نمی‌فرستد: تکمیل cleaning برای کل Meeting باعث ترکیب (compose) و رسیدن Meeting به وضعیت «منتظر تأیید» می‌شود؛ فقط پس از تأیید صریح یک کاربر مجاز (`POST /meetings/{id}/publication/approve`) Job مرحلهٔ `MINUTES_GENERATION` صف و روی `mcp.queue` منتشر می‌شود.

ACK بعد از ثبت پایدار نتیجه در PostgreSQL و MinIO ارسال می‌شود. خطاهای موقت با Attempt جدید، حداکثر سه بار و با تأخیر قابل تنظیم retry می‌شوند؛ خطای نهایی با `reject(requeue=false)` به `processing.dlq` می‌رود و جزئیات آن در ProcessingAttempt و History برای API/UI باقی می‌ماند. تحویل تکراری با وضعیت Attempt و کلید یکتای outbox idempotent شده است.

حذف Voice از UI، Attemptهای `queued` و `running` وابسته را لغو و پیام‌های منتشرنشدهٔ outbox را حذف می‌کند. workerهای فعال PostgreSQL را برای cancellation بررسی می‌کنند و نتیجهٔ کار لغوشده را ذخیره یا وارد مرحلهٔ بعد نمی‌کنند. پیام RabbitMQ که قبلاً publish شده است قابل حذف انتخابی نیست؛ هنگام تحویل به‌عنوان پیام stale بدون اجرای کار ACK می‌شود.

تنظیمات لازم در `.env`:

```text
RABBITMQ_HOST=rabbitmq
RABBITMQ_PORT=5672
RABBITMQ_USER=meeting_app
RABBITMQ_PASSWORD=...
RABBITMQ_PREFETCH_COUNT=1
RABBITMQ_RETRY_DELAY_SECONDS=5

BASE_URL=https://example.com/v1
TRANSCRIPT_API_KEY=...
TRANSCRIPT_MODEL_NAME=whisper-large-v3-persian
ASR_REQUEST_TIMEOUT_SECONDS=600
ASR_MAX_ATTEMPTS=3
ASR_WORKER_NAME=asr-worker
```

خروجی استاندارد `canonical-transcript/v1` و فایل متن خام با مسیر deterministic زیر در bucket خروجی MinIO ذخیره می‌شوند و bucket، key، checksum و producer job در PostgreSQL ثبت می‌شود:

```text
meetings/{meeting_id}/results/{result_id}/transcript.json
meetings/{meeting_id}/results/{result_id}/raw.txt
```

خطاهای timeout، network، HTTP 429 و HTTP 5xx تا سقف سه Attempt پیگیری می‌شوند؛ خطای احراز هویت و پاسخ نامعتبر retry نمی‌شوند. رخدادهای `processing.queued`، `processing.started`، `processing.retry_queued`، `processing.succeeded` و `processing.failed` در History ثبت می‌شوند.

## Migration

برای اجرای دستی migration:

```bash
uv run alembic upgrade head
```

اولین migration هم دیتابیس تازه را می‌سازد و هم دیتابیس قدیمی ایجادشده با `Base.metadata.create_all` را شناسایی و بدون حذف کاربران، رمزها، Meetingها یا History موجود ارتقا می‌دهد. اجرای مستقیم `create_all` از startup حذف شده است.

## تست‌ها

تست‌ها بر اساس حوزه در پوشه `test/` نگهداری می‌شوند:

```text
test_auth.py
test_meetings.py
test_voices.py
test_history.py
test_logging.py
test_ui.py
test_migrations.py
test_architecture.py
test_compose_integration.py
test_asr.py
test_processing.py
test_messaging.py
test_asr_benchmark.py            # فقط اجرای دستی؛ خارج از CI
```

اجرای کامل این فاز:

```bash
uv run pytest -q -m "not asr_benchmark" test/
```

بنچمارک زنده کیفیت فقط به‌صورت دستی و روی ۵۰ نمونه اول اجرا می‌شود. این فرمان فایل‌های نمونه را به API خارجی تنظیم‌شده ارسال می‌کند:

```bash
RUN_ASR_BENCHMARK=1 uv run pytest -q -m asr_benchmark test/test_asr_benchmark.py
```

WER و CER به‌صورت corpus-level پس از نرمال‌سازی فارسی محاسبه می‌شوند؛ شرط‌های ثبت‌شده در تست `WER < 25%` و `CER < 8%` هستند. این تست به‌طور صریح از CI حذف شده است.

در فازهای بعد، تست جدید به فایل حوزه مربوط اضافه می‌شود و همان مجموعه حوزه برای جلوگیری از regression اجرا خواهد شد.

## CI/CD

workflow موجود در `.github/workflows/ci-cd.yml` اکنون سه image مستقل `api`، `ui` و `asr` را در matrix می‌سازد. تست‌های Python پیش از build اجرا می‌شوند؛ تست Compose روی PostgreSQL و MinIO واقعی فقط برای API اجرا می‌شود؛ سپس در push به `main` یا tag نسخه، همان imageهای ساخته‌شده به GHCR منتشر می‌شوند:

```text
ghcr.io/<owner>/asr-arusha-api
ghcr.io/<owner>/asr-arusha-ui
ghcr.io/<owner>/asr-arusha-worker
```

برای pull request فقط build و test انجام می‌شود و image منتشر نمی‌شود. Compose integration عمداً سرویس‌های `postgres minio api` را صریح بالا می‌آورد تا image مستقل UI در job جداگانه باعث تداخل در تست backend نشود.

## مرز ML

هیچ dependency یا runtime مدل در سرویس backend وجود ندارد. worker نیز مدل را import یا اجرا نمی‌کند و فقط از طریق adapter HTTP با سرویس ASR خارجی ارتباط دارد؛ manifest، Dockerfile و process آن از API و UI جدا هستند.
