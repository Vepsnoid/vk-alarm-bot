"""Main FastAPI application for VK Alarm Bot."""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import asyncio
import logging
import os
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO level, which would leak access tokens
# (VK/Max/AI) into the log file; keep only warnings/errors from these libraries.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Defence in depth: even if some library (or a future log statement) prints a
# URL/token, the filter rewrites it before it reaches the file. Uvicorn logs its
# access lines on dedicated loggers (``propagate = False``), so they are patched
# explicitly.
from app.core.redaction import install_log_redaction  # noqa: E402

install_log_redaction(
    logging.getLogger(),
    logging.getLogger("uvicorn"),
    logging.getLogger("uvicorn.error"),
    logging.getLogger("uvicorn.access"),
)

from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, text

from app.models.database import engine, Base
from app.routers import auth, monitors, settings, notifications, users
from app.services.monitor_processor import MonitorProcessor
from app.core.config import get_settings

scheduler = AsyncIOScheduler()

# The dispatcher runs frequently and starts only the streams whose own interval
# has elapsed. This enables per-stream frequency without creating one scheduler
# job per monitor and keeps VK requests staggered instead of bursting.
DISPATCHER_TICK_SECONDS = 30


# Fallback for legacy streams without a per-stream interval. New streams always
# carry their own ``check_interval_minutes``.
DEFAULT_CHECK_INTERVAL_MINUTES = 15

# Hard cap for a single dispatcher run; a hung stream is aborted (and, per-monitor
# timeouts inside the processor keep one stream from eating the whole budget).
SCHEDULER_RUN_TIMEOUT_SECONDS = 1500


async def run_scheduled_monitors():
    processor = MonitorProcessor()
    try:
        await asyncio.wait_for(
            processor.process_due_monitors(DEFAULT_CHECK_INTERVAL_MINUTES),
            timeout=SCHEDULER_RUN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("Scheduled stream processing timed out after %s s and was aborted", SCHEDULER_RUN_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error(f"Scheduled monitor processing failed: {e}")
    finally:
        await processor.vk.aclose()


PLACEHOLDER_ADMIN_PASSWORD = "replace-with-a-strong-password"


async def sync_admin_password(db, admin_user, admin_password: str) -> bool:
    """Apply the admin password from ``.env``; return True when it actually changed.

    A changed password must also bump ``token_version``: JWTs carry the version
    they were issued with, so without the bump a previously issued (possibly
    stolen) token would stay valid for the rest of its 24-hour lifetime even
    though the login has changed. An unchanged ``.env`` must NOT invalidate the
    sessions of the running admin, so the password is verified first and the
    bump happens only when the value really differs.
    """
    from app.core.security import get_password_hash, verify_password
    if verify_password(admin_password, admin_user.password_hash or ""):
        return False
    admin_user.password_hash = get_password_hash(admin_password)
    admin_user.token_version = (admin_user.token_version or 1) + 1
    await db.commit()
    return True


async def seed_initial_user():
    """Create the admin from ``.env`` or sync its password when ``.env`` changed.

    ``.env`` is the documented source of truth for the admin account: editing
    ``ADMIN_PASSWORD`` and restarting the service must be enough to change the
    login. Without the sync below the old bcrypt hash stayed in the database and
    the new password «не подходил»; the sync also revokes the tokens issued with
    the previous password (see ``sync_admin_password``).
    """
    from app.models.database import AsyncSessionLocal
    from app.models.models import User
    from app.core.security import get_password_hash
    try:
        app_settings = get_settings()
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.username == app_settings.admin_username))
            admin_user = result.scalar_one_or_none()
            if admin_user is None:
                admin_user = User(
                    username=app_settings.admin_username,
                    password_hash=get_password_hash(app_settings.admin_password),
                    role="admin",
                    is_active=True,
                )
                db.add(admin_user)
                await db.commit()
                logger.info("Created admin '%s' from .env", app_settings.admin_username)
            elif await sync_admin_password(db, admin_user, app_settings.admin_password):
                logger.info("Admin '%s' password updated from .env, issued tokens revoked", app_settings.admin_username)
        if app_settings.admin_password == PLACEHOLDER_ADMIN_PASSWORD:
            logger.warning("ADMIN_PASSWORD is still the template placeholder — set a real password in .env")
    except Exception as e:
        print(f"Failed to seed initial user: {e}")


async def migrate_sqlite_schema():
    if engine.url.get_backend_name() != "sqlite":
        return
    additions = {
        "monitors": {
            "ai_tone": "VARCHAR(100) DEFAULT 'нейтральный'",
            "ai_max_length": "INTEGER DEFAULT 5000",
            "ai_fallback_to_original": "BOOLEAN DEFAULT 1",
            "ai_filtered_out": "INTEGER DEFAULT 0",
            "publication_errors": "INTEGER DEFAULT 0",
            "owner_id": "INTEGER REFERENCES users(id)",
            "paused_at": "DATETIME",
            "run_started_at": "DATETIME",
            "check_interval_minutes": "INTEGER DEFAULT 15",
            "copy_media": "BOOLEAN DEFAULT 1",
            "log_all_posts": "BOOLEAN DEFAULT 0",
        },
        "users": {
            "theme_preference": "VARCHAR(20) DEFAULT 'system'",
            "token_version": "INTEGER DEFAULT 1",
        },
        "events": {
            "retry_attempts": "INTEGER DEFAULT 0",
            "delivery_state": "JSON",
        },
    }
    async with engine.begin() as conn:
        for table, columns in additions.items():
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            existing = {row[1] for row in result.fetchall()}
            for col, definition in columns.items():
                if col not in existing:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {definition}"))


async def redact_stored_secrets():
    from app.models.database import AsyncSessionLocal
    from app.models.models import Log, Notification
    from app.core.redaction import redact_sensitive_data
    async with AsyncSessionLocal() as db:
        for model, fields in ((Log, ("message", "details")), (Notification, ("title", "message"))):
            result = await db.execute(select(model))
            for record in result.scalars():
                for field in fields:
                    value = getattr(record, field)
                    redacted = redact_sensitive_data(value)
                    if redacted != value:
                        setattr(record, field, redacted)
        await db.commit()


async def hash_legacy_passwords():
    """Rewrite legacy plaintext passwords as bcrypt hashes.

    ``verify_password`` still accepts a non-hash value, but only so that
    ``ADMIN_PASSWORD`` from ``.env`` can be compared; rows in the database must
    never keep a readable password.
    """
    from app.models.database import AsyncSessionLocal
    from app.models.models import User
    from app.core.security import BCRYPT_MAX_BYTES, get_password_hash, is_password_hash
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(User))).scalars().all()
        migrated = 0
        truncated = []
        for user in rows:
            if user.password_hash and not is_password_hash(user.password_hash):
                if len(user.password_hash.encode("utf-8")) > BCRYPT_MAX_BYTES:
                    # bcrypt учитывает только первые 72 байта. Вход продолжит работать
                    # с полным паролем (проверка усекает так же), но такую запись лучше
                    # перевыдать: скажите пользователю сменить пароль.
                    truncated.append(user.username)
                user.password_hash = get_password_hash(user.password_hash)
                migrated += 1
        if migrated:
            await db.commit()
            logger.warning("Пароли без bcrypt перехешированы: %s записей", migrated)
        if truncated:
            logger.warning(
                "Пароль длиннее %s байт усечён bcrypt (попросите сменить пароль): %s",
                BCRYPT_MAX_BYTES,
                ", ".join(truncated),
            )


async def backfill_stream_owners():
    """Give every stream a valid owner.

    Legacy rows created before ``owner_id`` existed (and rows whose owner was
    deleted) are assigned to an admin, so the application never has to show a
    stream without an owner.
    """
    from app.models.database import AsyncSessionLocal
    from app.models.models import Monitor, User
    from app.services.owner_service import resolve_default_owner_id
    try:
        async with AsyncSessionLocal() as db:
            default_owner_id = await resolve_default_owner_id(db)
            if not default_owner_id:
                return
            valid_ids = set((await db.execute(select(User.id))).scalars().all())
            result = await db.execute(select(Monitor))
            fixed = 0
            cleared = 0
            for monitor in result.scalars():
                if monitor.owner_id not in valid_ids:
                    monitor.owner_id = default_owner_id
                    fixed += 1
                if monitor.run_started_at is not None:
                    # No run can still be in progress right after a restart: the
                    # marker would otherwise leave the UI stuck on "run in progress".
                    monitor.run_started_at = None
                    cleared += 1
            if fixed or cleared:
                await db.commit()
                logger.info("Startup cleanup: %s stream owner(s) fixed, %s run marker(s) cleared", fixed, cleared)
    except Exception as e:
        print(f"Failed to backfill stream owners: {e}")


async def normalize_stored_lists():
    """Fix list fields saved before normalisation (literal ``\\n``, commas, …).

    A stream whose ``source_channels`` holds ``"durov\\npublic1"`` (literal
    backslash-n) is treated as a single source: the UI shows «Источники: 1» and
    the run cannot resolve it. Rewriting such rows on startup repairs them.
    """
    from app.models.database import AsyncSessionLocal
    from app.models.models import Monitor
    from app.core.lists import LIST_FIELDS, normalize_monitor_lists

    fixed = 0
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Monitor))).scalars().all()
        for row in rows:
            values = {key: getattr(row, key, None) for key in LIST_FIELDS}
            normalize_monitor_lists(values)
            changed = {key: value for key, value in values.items() if value != getattr(row, key, None)}
            if changed:
                for key, value in changed.items():
                    setattr(row, key, value)
                fixed += 1
        if fixed:
            await db.commit()
            logger.info("Normalised pasted lists for %s stream(s)", fixed)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app_settings = get_settings()
    if not app_settings.secret_key or not app_settings.admin_username or not app_settings.admin_password:
        raise RuntimeError("Set SECRET_KEY, ADMIN_USERNAME, and ADMIN_PASSWORD before starting")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate_sqlite_schema()
    await normalize_stored_lists()
    await redact_stored_secrets()
    await seed_initial_user()
    await hash_legacy_passwords()
    await backfill_stream_owners()

    scheduler.add_job(run_scheduled_monitors, trigger=IntervalTrigger(seconds=DISPATCHER_TICK_SECONDS, jitter=10), id="monitor_dispatcher", replace_existing=True)
    scheduler.start()
    yield
    # ``wait=False`` keeps reloads and shutdown fast even while a long stream run
    # is still in flight.
    scheduler.shutdown(wait=False)
    await engine.dispose()


app = FastAPI(title="VK Alarm Bot", description="VK post monitoring, AI analysis, Max notifications", version="1.0.0", lifespan=lifespan, docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json")

app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:3000", "http://localhost:3000", "http://127.0.0.1:8000", "http://localhost:8000"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

app.include_router(auth.router)
app.include_router(monitors.router)
app.include_router(settings.router)
app.include_router(notifications.router)
app.include_router(users.router)

@app.get("/api/health")
async def health_check():
    jobs = scheduler.get_jobs()
    return {"status": "ok", "scheduler_running": scheduler.running, "jobs": [{"id": j.id, "interval_seconds": int(j.trigger.interval.total_seconds()) if isinstance(j.trigger, IntervalTrigger) and j.trigger.interval else None, "next_run_time": str(j.next_run_time) if j.next_run_time else None} for j in jobs]}


# NOTE: keep the SPA catch-all route *after* all API routes, otherwise it would
# swallow requests such as /api/health and return index.html instead of JSON.
frontend_dist = os.path.join(os.path.dirname(__file__), "../../frontend/dist")


def resolve_frontend_file(full_path: str) -> Optional[str]:
    """Resolve a request path inside ``frontend/dist`` (``None`` when unsafe).

    ``os.path.join`` happily accepts ``../..`` (and on Windows also backslashes),
    which used to let a crafted request (``/../../.env``, ``/%2e%2e/%2e%2e/.env``)
    read files outside the SPA directory — including ``.env`` with all the
    tokens. Only files that stay inside the (resolved) dist directory are served;
    everything else falls back to ``index.html``.
    """
    if not full_path:
        return None
    # Treat backslashes as separators: on Windows they would otherwise be passed
    # through to the filesystem as a valid separator.
    candidate = os.path.realpath(os.path.join(frontend_dist, full_path.replace("\\", "/")))
    root = os.path.realpath(frontend_dist)
    try:
        if os.path.commonpath([candidate, root]) != root:
            return None
    except ValueError:
        # Different drives (Windows) — definitely outside the SPA directory.
        return None
    if os.path.isfile(candidate):
        return candidate
    return None


if os.path.exists(frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dist, "assets")), name="assets")
    index_file = os.path.join(frontend_dist, "index.html")
    @app.get("/")
    async def serve_index():
        return FileResponse(index_file)
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Never serve the SPA for API paths; let FastAPI return a proper 404 JSON.
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        file_path = resolve_frontend_file(full_path)
        if file_path:
            return FileResponse(file_path)
        return FileResponse(index_file)