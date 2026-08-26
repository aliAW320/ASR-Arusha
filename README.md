# Persian Meeting Platform

Backend پایه سامانه Meeting Intelligence با FastAPI، PostgreSQL و MinIO به‌همراه UI سبک و مستقل. در این فاز تنها قابلیت‌های سبک API پیاده‌سازی شده‌اند و هیچ runtime یا کتابخانه ML در image بک‌اند نصب نمی‌شود.

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
```

اجرای کامل این فاز:

```bash
PYTHONPATH=fast_Backend uv run pytest -q test/
```

در فازهای بعد، تست جدید به فایل حوزه مربوط اضافه می‌شود و همان مجموعه حوزه برای جلوگیری از regression اجرا خواهد شد.

## CI/CD

workflow موجود در `.github/workflows/ci-cd.yml` اکنون دو image مستقل `api` و `ui` را در matrix می‌سازد. تست‌های Python پیش از build اجرا می‌شوند؛ تست Compose روی PostgreSQL و MinIO واقعی فقط برای API اجرا می‌شود؛ سپس در push به `main` یا tag نسخه، همان imageهای ساخته‌شده به GHCR منتشر می‌شوند:

```text
ghcr.io/<owner>/asr-arusha-api
ghcr.io/<owner>/asr-arusha-ui
```

برای pull request فقط build و test انجام می‌شود و image منتشر نمی‌شود. Compose integration عمداً سرویس‌های `postgres minio api` را صریح بالا می‌آورد تا image مستقل UI در job جداگانه باعث تداخل در تست backend نشود.

## مرز ML

هیچ dependency، runtime یا تنظیمات اجرایی مدل در پروژه بک‌اند وجود ندارد. در فاز worker، سرویس ML باید manifest، image و محیط مستقل خودش را داشته باشد؛ ارتباط آن با backend فقط از مرز API/پیام تعریف‌شده انجام خواهد شد.
