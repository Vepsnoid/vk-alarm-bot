"""Monitors router."""

import asyncio
import logging
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, delete
from typing import List, Optional, Any
from datetime import datetime, timedelta
from pydantic import BaseModel
from app.models.database import get_db, AsyncSessionLocal
from app.models.models import Monitor, Event, Log
from app.core.redaction import redact_sensitive_data
from app.core.lists import normalize_monitor_lists
from app.routers.auth import get_current_user
from app.services.monitor_processor import MonitorProcessor
from app.services.owner_service import resolve_default_owner_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/monitors", tags=["monitors"])


def validate_monitor_access(monitor: Monitor, user: dict):
    """Admins may access any stream; regular users only their own.

    Streams with ``owner_id = NULL`` are treated as global/admin-owned and are
    therefore accessible to admins only.
    """
    if user.get("role") != "admin" and monitor.owner_id != user.get("id"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Доступ запрещен: этот поток принадлежит другому пользователю")


REQUIRED_MONITOR_FIELDS = {
    "name": "Название потока",
    "source_channels": "Источники VK",
    "max_channels": "Целевой канал Max",
}


def collect_missing_required(params: dict) -> List[str]:
    """Return human-readable labels of required fields that are blank.

    ``params`` maps field name -> value for the fields being submitted. Only
    fields present in the mapping are checked, so partial updates (e.g. toggling
    ``is_active``) are not blocked by unrelated empty values.
    """
    return [
        REQUIRED_MONITOR_FIELDS[key]
        for key, value in params.items()
        if key in REQUIRED_MONITOR_FIELDS and not str(value or "").strip()
    ]


def raise_if_missing(missing: List[str]) -> None:
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Не заполнены обязательные поля: " + ", ".join(missing),
        )


async def _log_stream_message(monitor_id: int, level: str, message: str) -> None:
    """Append a note to a stream journal (used for user-visible feedback)."""
    try:
        async with AsyncSessionLocal() as db:
            db.add(Log(monitor_id=monitor_id, level=level, message=redact_sensitive_data(message)))
            await db.commit()
    except Exception as e:
        logger.error("Cannot append log for monitor %s: %s", monitor_id, e)


async def _run_monitor_in_background(monitor_id: int):
    """Run a stream right away (resume, fresh start, «Запустить сейчас»).

    Delegates to the same isolated runner the scheduler uses, so the run marker
    (``run_started_at``), the per-stream timeout, error logging and the shared
    lock behave identically — the UI then shows «прогон идёт…» instead of a
    stale countdown.

    When another run is already in progress the request is not dropped: the id is
    queued and executed as soon as the lock is free (see
    ``MonitorProcessor._drain_pending_runs``), and the user sees why in the log.
    """
    processor = MonitorProcessor()
    try:
        if not processor.vk.token:
            logger.warning("Skipping immediate run of monitor %s: VK token is not configured", monitor_id)
            return
        if processor._lock.locked():
            # A run is already in progress: remember the request instead of losing
            # it. A set (not a list) keeps repeated clicks from piling up work.
            MonitorProcessor._pending_run_ids.add(monitor_id)
            logger.warning("Immediate run of monitor %s postponed: another run is already in progress", monitor_id)
            await _log_stream_message(monitor_id, "info", "Запуск отложен: выполнится сразу после текущего прогона")
            return
        async with processor._lock:
            await processor._run_monitor_isolated(monitor_id)
            # Pick up runs requested while this one was in progress.
            await processor._drain_pending_runs()
    except Exception as e:
        logger.error(f"Background execution of monitor {monitor_id} failed: {e}")
    finally:
        try:
            await processor.vk.aclose()
        except Exception:
            pass


class MonitorCreate(BaseModel):
    name: str
    source_channels: str
    max_channels: Optional[str] = None
    keywords: Optional[str] = None
    minus_words: Optional[str] = None
    check_interval_minutes: Optional[int] = None
    copy_media: Optional[bool] = None
    log_all_posts: Optional[bool] = None
    min_er: Optional[float] = None
    max_er: Optional[float] = None
    use_ai: Optional[bool] = None
    ai_prompt: Optional[str] = None
    ai_tone: Optional[str] = None
    ai_max_length: Optional[int] = None
    ai_fallback_to_original: Optional[bool] = None
    is_active: Optional[bool] = None
    owner_id: Optional[int] = None
class MonitorUpdate(BaseModel):
    name: Optional[str] = None
    source_channels: Optional[str] = None
    max_channels: Optional[str] = None
    keywords: Optional[str] = None
    check_interval_minutes: Optional[int] = None
    copy_media: Optional[bool] = None
    log_all_posts: Optional[bool] = None
    owner_id: Optional[int] = None
    minus_words: Optional[str] = None
    min_er: Optional[float] = None
    max_er: Optional[float] = None
    use_ai: Optional[bool] = None
    ai_prompt: Optional[str] = None
    ai_tone: Optional[str] = None
    ai_max_length: Optional[int] = None
    ai_fallback_to_original: Optional[bool] = None
    is_active: Optional[bool] = None


class MonitorResponse(BaseModel):
    id: int
    name: str
    is_active: bool
    check_interval_minutes: Optional[int] = 15
    copy_media: Optional[bool] = True
    log_all_posts: Optional[bool] = False
    source_channels: str
    max_channels: Optional[str] = None
    keywords: Optional[str] = None
    minus_words: Optional[str] = None
    min_er: float
    max_er: float
    use_ai: bool
    ai_prompt: Optional[str] = None
    ai_tone: str
    ai_max_length: int
    ai_fallback_to_original: bool
    posts_processed: int
    posts_published: int
    posts_filtered_keywords: int
    posts_filtered_er: int
    ai_successes: int
    ai_fallbacks: int
    ai_filtered_out: int
    publication_errors: int
    last_post_ids: Optional[Any] = None
    last_run_at: Optional[datetime] = None
    run_started_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    owner_id: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=List[MonitorResponse])
@router.get("/", response_model=List[MonitorResponse])
async def get_monitors(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    query = select(Monitor).order_by(desc(Monitor.created_at))
    if user.get("role") != "admin":
        query = query.where(Monitor.owner_id == user.get("id"))
    result = await db.execute(query)
    monitors = result.scalars().all()
    for m in monitors:
        if not m.is_active and not m.paused_at:
            m.paused_at = datetime.utcnow()
    await db.commit()
    return monitors


@router.delete("/{monitor_id}")
async def delete_monitor(monitor_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    monitor = await db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Мониторинг не найден")
    validate_monitor_access(monitor, user)
    await db.delete(monitor)
    await db.commit()
    return {"message": "Мониторинг удален"}


@router.post("/{monitor_id}/run")
async def run_monitor_now(monitor_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    monitor = await db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Мониторинг не найден")
    validate_monitor_access(monitor, user)
    # Stamp the check immediately: the UI then shows the new «последний запуск»
    # right away instead of waiting for the (possibly multi-minute) run to end.
    monitor.last_run_at = datetime.utcnow()
    await db.commit()
    asyncio.ensure_future(_run_monitor_in_background(monitor_id))
    return {"message": "Запущен обработчик мониторинга"}


@router.post("/{monitor_id}/test-sources")
async def test_sources(monitor_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    from app.services.vk_service import VKService
    monitor = await db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Мониторинг не найден")
    validate_monitor_access(monitor, user)
    sources = [s.strip() for s in monitor.source_channels.split("\n") if s.strip()]
    results = []
    vk = VKService()
    await vk.init_settings(db)
    for source in sources:
        entry = {"source": source, "ok": False, "id": None, "error": None}
        try:
            owner_id = await vk.resolve_owner_id(source)
            if not owner_id:
                entry["error"] = vk.describe_unresolved_source(source)
            else:
                entry["id"] = owner_id
                access_ok, reason = await vk.check_access(owner_id)
                entry["ok"] = access_ok
                if not access_ok:
                    entry["error"] = reason
        except Exception as e:
            entry["error"] = str(e)
        results.append(entry)
    await vk.aclose()
    return {"results": results}


@router.post("/{monitor_id}/test-max")
async def test_max_channels(monitor_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    from app.services.max_service import MaxService
    from app.core.config import get_settings
    config = get_settings()
    monitor = await db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Мониторинг не найден")
    validate_monitor_access(monitor, user)
    channels = [c.strip() for c in (monitor.max_channels or "").split("\n") if c.strip()]
    if not channels:
        return {"results": []}
    if not config.max_bot_token:
        return {"results": [{"target": ch, "ok": False, "error": "Max-бот не настроен: укажите токен в «Настройках»"} for ch in channels]}
    ms = MaxService(token=config.max_bot_token)
    bot_name = None
    if config.max_bot_token:
        bot_name = MaxService.describe_bot(await MaxService.get_me_info(config.max_bot_token))
    results = []
    for ch in channels:
        try:
            cid = await ms.parse_chat_id(ch)
            if cid:
                member = await ms.check_membership(int(cid))
                hint = (" (бот " + bot_name + ")") if bot_name else ""
                results.append({"target": ch, "ok": member is True, "error": None if member else (("Бот не участник" + hint) if member is False else ("Не удалось проверить" + hint))})
            else:
                results.append({"target": ch, "ok": False, "error": "Не удалось распознать ID"})
        except Exception as e:
            results.append({"target": ch, "ok": False, "error": str(e)})
    await ms.aclose()
    return {"results": results}


class EventResponse(BaseModel):
    id: int
    monitor_id: int
    original_id: str
    original_owner_id: str
    original_url: str
    original_text: Optional[str] = None
    original_date: Optional[datetime] = None
    attachments: Optional[str] = None
    likes: int
    reposts: int
    comments: int
    views: int
    er: float
    ai_analysis_result: Optional[str] = None
    ai_filtered: bool
    status: str
    sent_at: Optional[datetime] = None
    error_message: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class EventListResponse(BaseModel):
    events: List[EventResponse]
    total: int


@router.get("/{monitor_id}/events", response_model=EventListResponse)
async def get_events(monitor_id: int, status_filter: str = "all", limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    monitor = await db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Мониторинг не найден")
    validate_monitor_access(monitor, user)
    query = select(Event).where(Event.monitor_id == monitor_id)
    if status_filter != "all":
        query = query.where(Event.status == status_filter)
    count_query = select(func.count()).select_from(query.order_by(None).subquery())
    total = (await db.execute(count_query)).scalar_one()
    query = query.order_by(desc(Event.created_at)).offset(offset).limit(limit)
    res = await db.execute(query)
    return EventListResponse(events=res.scalars().all(), total=total)


# Moscow is permanently UTC+3 (no daylight saving since 2014). Event timestamps
# are stored in UTC, so date filters coming from the UI (Moscow local dates) are
# converted to UTC before being applied to the query.
MOSCOW_OFFSET = timedelta(hours=3)


def _parse_moscow_date(value: Optional[str], *, end_of_day: bool = False) -> Optional[datetime]:
    """Parse an ISO ``YYYY-MM-DD`` (or ISO datetime) Moscow-local value into naive UTC."""
    if not value or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", ""))
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None
    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed - MOSCOW_OFFSET


@router.get("/events", response_model=List[EventResponse])
async def get_all_events(
    limit: int = Query(100, ge=1, le=500),
    offset: int = 0,
    monitor_id: Optional[int] = None,
    status_filter: str = "all",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    query = _events_query(user, monitor_id, status_filter, date_from, date_to, search)

    query = query.order_by(desc(Event.created_at)).offset(offset).limit(limit)
    res = await db.execute(query)
    return res.scalars().all()


def _events_query(user, monitor_id=None, status_filter="all", date_from=None, date_to=None, search=None):
    """Build the events query shared by the list and the delete endpoints.

    Access control: admins see everything, other users only the events of the
    streams they own.
    """
    query = select(Event)
    if user.get("role") != "admin":
        accessible = select(Monitor.id).where(Monitor.owner_id == user.get("id"))
        query = query.where(Event.monitor_id.in_(accessible))
    if monitor_id:
        query = query.where(Event.monitor_id == monitor_id)
    if status_filter and status_filter != "all":
        query = query.where(Event.status == status_filter)
    parsed_from = _parse_moscow_date(date_from)
    parsed_to = _parse_moscow_date(date_to, end_of_day=True)
    if parsed_from:
        query = query.where(Event.created_at >= parsed_from)
    if parsed_to:
        query = query.where(Event.created_at <= parsed_to)
    if search and search.strip():
        query = query.where(Event.original_text.ilike(f"%{search.strip()}%"))
    return query


@router.delete("/events/{event_id}")
async def delete_event(event_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Delete a single event (admins, or the owner of the event's stream)."""
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Событие не найдено")
    monitor = await db.get(Monitor, event.monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Поток события не найден")
    validate_monitor_access(monitor, user)
    await db.delete(event)
    await db.commit()
    return {"message": "Событие удалено"}


@router.post("/events/clear")
async def clear_events(
    monitor_id: Optional[int] = None,
    status_filter: str = "all",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Delete every event matching the given filters (access rights respected)."""
    ids = (await db.execute(
        _events_query(user, monitor_id, status_filter, date_from, date_to, search).with_only_columns(Event.id)
    )).scalars().all()
    deleted = len(ids)
    if deleted:
        await db.execute(delete(Event).where(Event.id.in_(ids)))
        await db.commit()
    return {"deleted": deleted}


@router.get("/{monitor_id}", response_model=MonitorResponse)
async def get_monitor(monitor_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    monitor = await db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Мониторинг не найден")
    validate_monitor_access(monitor, user)
    return monitor


@router.post("", response_model=MonitorResponse)
@router.post("/", response_model=MonitorResponse)
async def create_monitor(data: MonitorCreate, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    raise_if_missing(collect_missing_required({
        "name": data.name,
        "source_channels": data.source_channels,
        "max_channels": data.max_channels,
    }))
    # Admins own their streams too (so the per-user filter finds them) but may
    # assign a different owner; regular users always own what they create.
    owner_id = data.owner_id if (user.get("role") == "admin" and data.owner_id) else user.get("id")
    owner_id = await resolve_default_owner_id(db, prefer=owner_id)
    # Pasted lists are normalised (literal "\n", commas, semicolons, tabs, CRLF)
    # so that every source/channel/keyword ends up on its own line.
    fields = normalize_monitor_lists(data.model_dump(exclude_none=True, exclude={"owner_id"}))
    monitor = Monitor(**fields, owner_id=owner_id)
    db.add(monitor)
    await db.commit()
    await db.refresh(monitor)
    return monitor


@router.put("/{monitor_id}", response_model=MonitorResponse)
async def update_monitor(monitor_id: int, data: MonitorUpdate, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    monitor = await db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Мониторинг не найден")
    validate_monitor_access(monitor, user)
    update_data = data.model_dump(exclude_none=True)
    # Normalise pasted lists before anything else: the "changed?" comparison below
    # then sees canonical values, so re-saving the same list is not a fresh start.
    normalize_monitor_lists(update_data)
    raise_if_missing(collect_missing_required({k: v for k, v in update_data.items() if k in REQUIRED_MONITOR_FIELDS}))
    requested_owner = update_data.pop("owner_id", None)
    is_active_changed = "is_active" in update_data and update_data["is_active"] != monitor.is_active
    # Any *operational* setting that actually changed restarts the stream from
    # "now" (fresh start): the cursor is re-initialised so the backlog collected
    # since the last run is not published, and a check is fired immediately.
    # Renaming or reassigning a stream must not discard the backlog.
    fresh_start = any(
        getattr(monitor, key, None) != value
        for key, value in update_data.items()
        if key not in ("name", "is_active")
    )
    for key, value in update_data.items():
        setattr(monitor, key, value)
    # Only admins may (re)assign ownership.
    if user.get("role") == "admin" and requested_owner is not None:
        monitor.owner_id = requested_owner
    if not monitor.is_active:
        # Keep the pause marker: the run that resumes the stream starts fresh
        # and skips the backlog that piled up while it was down.
        if monitor.paused_at is None:
            monitor.paused_at = datetime.utcnow()
    elif is_active_changed or fresh_start:
        # Pause marker / fresh start: the processor re-initialises the cursors
        # on the next run (history is skipped) and clears the marker afterwards.
        monitor.paused_at = datetime.utcnow()
        # A check is fired below, so the UI shows the new «последний запуск»
        # immediately instead of the previous one (the runner re-stamps it too).
        monitor.last_run_at = datetime.utcnow()
    await db.commit()
    await db.refresh(monitor)
    # Resuming and fresh start both fire a check immediately instead of waiting
    # for the stream's interval.
    if monitor.is_active and (is_active_changed or fresh_start):
        asyncio.ensure_future(_run_monitor_in_background(monitor_id))
    return monitor