"""Monitor processing service - parses VK posts, analyzes with AI, sends to Max."""

import asyncio
import ast
import logging
import random
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from app.models.models import Monitor, Event, Log, Notification
from app.services.vk_service import VKService, VKAPIError
from app.services.ai_service import AIService
from app.services.max_service import MaxService, MaxBotError
from app.services.notification_service import NotificationService
from app.models.database import AsyncSessionLocal
from app.core.config import get_settings
from app.core.limits import (
    DEFAULT_AI_MAX_LENGTH,
    DEFAULT_CHECK_INTERVAL_MINUTES,
    MAX_AI_MAX_LENGTH,
    MAX_CHECK_INTERVAL_MINUTES,
    MAX_ER_PERCENT,
    MIN_AI_MAX_LENGTH,
    MIN_CHECK_INTERVAL_MINUTES,
    MIN_ER_PERCENT,
    bounded_float,
    bounded_int,
)
from app.core.redaction import redact_sensitive_data

logger = logging.getLogger(__name__)

# Upper bound on how many new posts are pulled per source per run. Pagination
# stops earlier as soon as already-seen (older) posts are reached. When a source
# publishes more than this between two runs, the surplus is NOT silently dropped:
# the run logs a warning (see ``_warn_skipped_posts``).
MAX_NEW_POSTS_PER_RUN = 200
# Upper bound on media attachments forwarded for a single post.
MAX_MEDIA_ATTACHMENTS = 10
# How many events are kept per stream when the full pipeline log is enabled.
MAX_LOGGED_EVENTS_PER_STREAM = 2000
# A publication that failed for a *transient* reason (network, 5xx, rate limit)
# is kept as a ``pending`` event and retried by the following runs, so an outage
# cannot silently swallow posts. The counter stops the retries, so a permanently
# broken Max setup does not loop forever — the post then shows up in «События»
# as ``failed`` with the reason.
MAX_PUBLISH_ATTEMPTS = 5
# How many queued posts are retried in a single run (bounds the run duration).
MAX_RETRIES_PER_RUN = 50


def _is_transient_max_error(error: Exception) -> bool:
    """Whether a Max failure is worth retrying.

    ``MaxBotError.status_code`` is 0 for network problems (and for errors without
    an HTTP status), 5xx/429 when the service is temporarily unavailable. A 4xx
    answer means the request itself is wrong (bad token, the bot is not a member,
    the message was rejected) — repeating it would produce the same answer.
    """
    status = getattr(error, "status_code", 0) or 0
    if status == 0:
        return True
    return status >= 500 or status == 429


class MonitorProcessor:
    _lock = asyncio.Lock()
    # Serialises publishing independently from post collection.
    _publish_lock = asyncio.Lock()
    # Hard timeout for processing a single stream.
    MONITOR_RUN_TIMEOUT_SECONDS = 900
    # Streams whose «запустить сейчас» arrived while a run was already going.
    # They are executed as soon as the lock is free again (see
    # ``_drain_pending_runs``), so a click is never silently dropped.
    _pending_run_ids: set = set()

    # Phrases in the AI prompt that disable media forwarding.
    _NO_MEDIA_MARKERS = ("без медиа", "без вложений", "без картинок", "без фото", "без видео", "только текст", "text only", "no media")
    def __init__(self):
        self.vk = VKService()
        self.ai = AIService()

    # NOTE: the scheduler drives ``process_due_monitors`` only; the former
    # ``process_all_monitors`` helper was removed to keep a single code path.

    async def process_due_monitors(self, default_interval_minutes: int = 15):
        """Run only the monitors whose own interval has elapsed.

        Each stream is processed in its own DB session and with a hard timeout,
        so one hung stream neither blocks nor corrupts the rest. A random pause
        between streams keeps VK API requests staggered (reduces rate limiting).
        """
        if self._lock.locked():
            logger.warning("Skipped dispatch: previous run is still in progress")
            return
        async with self._lock:
            try:
                # Work out which streams are due, keeping only plain ids so the
                # per-stream sessions stay clean.
                async with AsyncSessionLocal() as db:
                    await self.vk.init_settings(db)
                    await self.ai.init_settings(db)
                    if not self.vk.token:
                        return
                    result = await db.execute(select(Monitor).where(Monitor.is_active.is_(True)))
                    monitors = result.scalars().all()
                    now = datetime.utcnow()
                    due_ids = []
                    for monitor in monitors:
                        # Bounds are enforced again here: rows saved by an older
                        # version (the API had no validation) may hold a 0 or
                        # negative interval, which would make the stream «due» on
                        # every scheduler tick and hammer the VK API.
                        interval = bounded_int(
                            monitor.check_interval_minutes or default_interval_minutes,
                            DEFAULT_CHECK_INTERVAL_MINUTES,
                            MIN_CHECK_INTERVAL_MINUTES,
                            MAX_CHECK_INTERVAL_MINUTES,
                        )
                        if monitor.last_run_at is None or (now - monitor.last_run_at) >= timedelta(minutes=interval):
                            due_ids.append(monitor.id)

                for monitor_id in due_ids:
                    await self._run_monitor_isolated(monitor_id)
                    # Honour «запустить сейчас» clicks received during the run.
                    await self._drain_pending_runs()
                    # Stagger runs between streams to avoid VK rate limiting.
                    await asyncio.sleep(random.uniform(2.0, 5.0))
                await self._drain_pending_runs()
            finally:
                await self.vk.aclose()

    async def _run_monitor_isolated(self, monitor_id: int) -> None:
        """Process one stream in its own session with a hard timeout."""
        async with AsyncSessionLocal() as db:
            monitor = await db.get(Monitor, monitor_id)
            if not monitor or not monitor.is_active:
                return
            monitor_name = monitor.name
            processed_before = monitor.posts_processed or 0
            published_before = monitor.posts_published or 0
            started_at = datetime.utcnow()
            # Both markers are written when the run STARTS:
            #   * ``run_started_at`` makes the UI show «прогон идёт: N мин»;
            #   * ``last_run_at`` is what the UI shows as «последний запуск» and what
            #     the scheduler uses as «next check = last_run_at + interval».
            # Writing them up front makes the click visible immediately (instead of
            # only when a multi-minute run is over) and keeps the interval honest:
            # a long run no longer pushes the next check away by its own duration.
            monitor.run_started_at = started_at
            monitor.last_run_at = started_at
            await db.commit()
            await self._add_log(db, monitor_id, "info", "Проверка потока запущена")
            await db.commit()

            failure = None
            try:
                await asyncio.wait_for(self.process_monitor(db, monitor), timeout=self.MONITOR_RUN_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                failure = f"превысила {self.MONITOR_RUN_TIMEOUT_SECONDS} с"
                await db.rollback()
                await self._add_log(db, monitor_id, "error", f"Обработка потока '{monitor_name}' превысила {self.MONITOR_RUN_TIMEOUT_SECONDS} с и была прервана")
                await db.commit()
            except Exception as e:
                failure = str(e)
                await db.rollback()
                await self._add_log(db, monitor_id, "error", f"Monitor processing failed: {str(e)}")
                await NotificationService.create_notification(db, title=f"Сбой в потоке '{monitor_name}'", message=f"Ошибка: {str(e)}", level="error", monitor_id=monitor_id)
                await db.commit()
            finally:
                try:
                    row = (await db.execute(select(Monitor).where(Monitor.id == monitor_id))).scalars().first()
                    if row is not None:
                        if failure is None:
                            minutes = (datetime.utcnow() - started_at).total_seconds() / 60
                            await self._add_log(
                                db,
                                monitor_id,
                                "info",
                                f"Проверка завершена за {minutes:.1f} мин: новых постов "
                                f"{(row.posts_processed or 0) - processed_before}, отправлено "
                                f"{(row.posts_published or 0) - published_before}",
                            )
                        # Consume the fresh-start marker this run was started with.
                        # The values are read as plain columns (not from the cached
                        # ORM instance) so that a marker the user set while this run
                        # was in progress is seen and kept for the queued run.
                        marker, run_start = (await db.execute(
                            select(Monitor.paused_at, Monitor.run_started_at).where(Monitor.id == monitor_id)
                        )).first()
                        if marker is not None and (run_start is None or marker <= run_start):
                            row.paused_at = None
                        if row.run_started_at is not None:
                            row.run_started_at = None
                        await db.commit()
                except Exception:
                    await db.rollback()

    async def _drain_pending_runs(self) -> None:
        """Run the streams whose «запустить сейчас» arrived while we were busy.

        A run holds the shared lock for minutes; without this the click would be
        silently ignored until the next interval. The ids live in a set, so a
        burst of clicks collapses into one extra run per stream.
        """
        for _ in range(10):
            if not self._pending_run_ids:
                return
            ids = sorted(self._pending_run_ids)
            self._pending_run_ids.clear()
            for monitor_id in ids:
                await self._run_monitor_isolated(monitor_id)

    async def process_monitor(self, db: AsyncSession, monitor: Monitor):
        """Process a single monitor: fetch VK posts, filter, analyze with AI, send to Max."""
        sources = [s.strip() for s in monitor.source_channels.split("\n") if s.strip()]
        keywords = [k.strip() for k in (monitor.keywords or "").split("\n") if k.strip()]
        minus_words = [m.strip() for m in (monitor.minus_words or "").split("\n") if m.strip()]

        if not sources:
            return

        # Normalise the numeric settings: rows saved before the API validated them
        # can hold out-of-range values (a negative interval, an inverted ER window
        # that would drop every post for good, a negative AI length limit).
        min_er = bounded_float(monitor.min_er, MIN_ER_PERCENT, MIN_ER_PERCENT, MAX_ER_PERCENT)
        max_er = bounded_float(monitor.max_er, MAX_ER_PERCENT, MIN_ER_PERCENT, MAX_ER_PERCENT)
        if min_er > max_er:
            min_er, max_er = max_er, min_er
        ai_max_length = bounded_int(
            monitor.ai_max_length, DEFAULT_AI_MAX_LENGTH, MIN_AI_MAX_LENGTH, MAX_AI_MAX_LENGTH
        )

        last_ids = dict(monitor.last_post_ids or {})
        # The marker is set either by pausing the stream or by saving its
        # settings ("fresh start"): in both cases this run re-initialises the
        # cursors below, so the backlog collected so far is not published.
        skip_backlog = monitor.paused_at is not None and monitor.is_active
        all_new_posts = []
        # New posts that did not fit into MAX_NEW_POSTS_PER_RUN (see
        # ``_warn_skipped_posts``): counted so the run can report them instead of
        # dropping them silently.
        skipped_posts = 0

        total_sources = len(sources)
        progress_every = 25
        last_cooldown_notice = None

        for index, source in enumerate(sources, start=1):
            # Explain long silent periods: after flood control / captcha the whole
            # service pauses every VK request for up to 5 minutes.
            cooldown_left = self.vk.cooldown_remaining()
            if cooldown_left >= 5 and (
                last_cooldown_notice is None or (datetime.utcnow() - last_cooldown_notice).total_seconds() >= 60
            ):
                last_cooldown_notice = datetime.utcnow()
                await self._add_log(db, monitor.id, "warning", f"Пауза из-за лимитов VK: ещё ~{int(cooldown_left)} с")
                await db.commit()
            try:
                owner_id = await self.vk.resolve_owner_id(source)
                if not owner_id:
                    await self._warn_source(db, monitor, source, self.vk.describe_unresolved_source(source))
                    # Commit here too: the ``continue`` below skips the tail of
                    # the loop, and pending rows must not pile up until the end.
                    await db.commit()
                    continue
                # No accessibility pre-check: the fetch below already reports access
                # errors with a hint, and skipping the extra call nearly halves the
                # VK requests spent per source on every run.
                last_id = last_ids.get(str(owner_id))
                fetch_stats: dict = {}
                posts = await self.vk.get_posts_since_last(
                    owner_id, last_id, max_posts=MAX_NEW_POSTS_PER_RUN, stats=fetch_stats
                )
                # Posts older than the newest MAX_NEW_POSTS_PER_RUN ones cannot be
                # reached any more once the cursor moves: report them (see below).
                skipped_posts += int(fetch_stats.get("skipped") or 0)

                if last_id is None or skip_backlog:
                    # Initialise the cursor and do not publish the history.
                    if posts:
                        last_ids[str(owner_id)] = max(int(p["id"]) for p in posts)
                    reason = "первый запуск" if last_id is None else "fresh start"
                    await self._add_log(db, monitor.id, "info", f"Источник {source}: {reason} — история пропущена")
                    # Commit here too (see the note above): a fresh start skips the
                    # history of every source, so without this the whole run would
                    # hold one long write transaction.
                    await db.commit()
                    continue

                if posts:
                    max_id = max(int(p["id"]) for p in posts)
                    last_ids[str(owner_id)] = max_id
                all_new_posts.extend(posts)
            except VKAPIError as e:
                await self._warn_source(db, monitor, source, self.vk.describe_error(e.error_code, e.error_message))
            except Exception as e:
                await self._warn_source(db, monitor, source, str(e))
            # Commit per source: a long run must not keep one write transaction
            # open for minutes, otherwise every other write (saving settings,
            # pausing a stream) fails with "database is locked".
            await db.commit()
            if index % progress_every == 0 and index < total_sources:
                # Visible progress for streams with hundreds of sources.
                await self._add_log(db, monitor.id, "info", f"Прогресс: {index}/{total_sources} источников")
                await db.commit()
        if skipped_posts:
            # Explicit policy for a burst: the newest MAX_NEW_POSTS_PER_RUN posts
            # per source are processed, the older ones cannot be reached once the
            # cursor moves past them — so say so instead of losing them silently.
            await self._warn_skipped_posts(db, monitor, skipped_posts)
        # Optional debug log: one row per parsed post so the UI can show which
        # ones were dropped by keywords/ER and which reached Max.
        post_events: dict = {}
        if monitor.log_all_posts:
            for post in all_new_posts:
                event = self._new_event(monitor, post, "parsed")
                db.add(event)
                post_events[int(post.get("id") or 0)] = event
            await db.flush()

        # Apply keyword / minus-word / ER filters (pre-AI)
        passed_posts = []
        for post in all_new_posts:
            filter_ok, category, detail = self.vk.check_filters(post["text"], keywords, minus_words, min_er, max_er, post["likes"], post["reposts"], post["comments"], post["views"])
            if not filter_ok:
                if category == "er":
                    monitor.posts_filtered_er += 1
                    status = "filtered_er"
                else:
                    monitor.posts_filtered_keywords += 1
                    status = "filtered_keywords"
                self._mark_event(post_events.get(int(post.get("id") or 0)), status, detail)
                continue
            passed_posts.append(post)

        # AI processing: transform the post text according to the prompt
        # (rewrite / transliterate / digest / ...). Runs AFTER keyword filtering.
        ready_posts = []
        ai_failures = 0
        ai_skipped_no_fallback = 0
        ai_streak = 0
        ai_disabled = False
        for post in passed_posts:
            if not monitor.use_ai or not monitor.ai_prompt or ai_disabled:
                # No AI for this post: either it is switched off for the stream, or
                # the provider just proved to be down (see the streak below).
                if ai_disabled:
                    monitor.ai_fallbacks += 1
                    ai_failures += 1
                    if monitor.ai_fallback_to_original:
                        ready_posts.append((post, None, "ИИ недоступен"))
                    else:
                        ai_skipped_no_fallback += 1
                        self._mark_event(
                            post_events.get(int(post.get("id") or 0)),
                            "filtered_ai",
                            "ИИ недоступен, публикация пропущена",
                        )
                else:
                    ready_posts.append((post, None, None))
                continue
            try:
                processed_text, ok, error = await self.ai.rewrite_text(
                    post.get("text", ""),
                    monitor.ai_prompt,
                    monitor.ai_tone,
                    ai_max_length,
                    monitor.ai_fallback_to_original,
                )
                if ok:
                    ai_streak = 0
                    if not (processed_text or "").strip():
                        # The model answered with a skip marker: the post is out of
                        # scope, so it is not published (and counted as AI-filtered).
                        monitor.ai_filtered_out += 1
                        self._mark_event(post_events.get(int(post.get("id") or 0)), "filtered_ai", "ИИ: пост не соответствует отбору (SKIP)")
                        continue
                    monitor.ai_successes += 1
                    ready_posts.append((post, processed_text, None))
                else:
                    monitor.ai_fallbacks += 1
                    ai_failures += 1
                    ai_streak += 1
                    if monitor.ai_fallback_to_original:
                        ready_posts.append((post, None, error))
                    else:
                        # Fallback disabled: do not publish an unvetted post, but
                        # record why it was dropped.
                        ai_skipped_no_fallback += 1
                        self._mark_event(
                            post_events.get(int(post.get("id") or 0)),
                            "filtered_ai",
                            f"ИИ недоступен, публикация пропущена: {error}",
                        )
                    if ai_streak >= self.ai.AI_MAX_CONSECUTIVE_FAILURES:
                        ai_disabled = True
            except Exception as e:
                monitor.ai_fallbacks += 1
                ai_failures += 1
                ai_streak += 1
                if monitor.ai_fallback_to_original:
                    ready_posts.append((post, None, str(e)))
                else:
                    ai_skipped_no_fallback += 1
                    self._mark_event(
                        post_events.get(int(post.get("id") or 0)),
                        "filtered_ai",
                        f"ИИ недоступен, публикация пропущена: {e}",
                    )
                if ai_streak >= self.ai.AI_MAX_CONSECUTIVE_FAILURES:
                    ai_disabled = True

        if ai_failures:
            # An AI outage must be visible: otherwise raw, unfiltered posts are
            # published (fallback on) or silently dropped (fallback off).
            detail = (
                f"ИИ недоступен: {ai_failures} пост(ов) — "
                + ("опубликованы оригиналы без фильтра" if monitor.ai_fallback_to_original
                   else f"публикация пропущена у {ai_skipped_no_fallback}")
            )
            await self._add_log(db, monitor.id, "warning", detail)
            await self._notify_once(db, monitor, f"Сбой ИИ: {monitor.name}", detail)
            await db.commit()

        monitor.posts_processed += len(all_new_posts)
        monitor.last_post_ids = last_ids
        # NOTE: ``last_run_at`` is written by ``_run_monitor_isolated`` when the run
        # STARTS — that is what the UI shows as «последний запуск» and what the
        # scheduler uses for «next check». Only the counters are finalised here.
        # The fresh-start marker (``paused_at``) is NOT cleared here: it is
        # consumed by the runner from a freshly read row, so a marker the user set
        # while this run was in progress survives for the queued run.

        # Retry the posts kept from previous runs (transient Max failures) before
        # sending the fresh batch: a short outage must not lose publications.
        await self._retry_pending_events(db, monitor)

        # Publish oldest -> newest (chronological order).
        ready_posts.sort(key=lambda item: (item[0].get("date") or datetime.min, int(item[0].get("id") or 0)))

        for post, ai_text, ai_error in ready_posts:
            try:
                await self._send_to_max(db, monitor, post, ai_text, ai_error, event=post_events.get(int(post.get("id") or 0)))
            except Exception as e:
                await self._add_log(db, monitor.id, "error", f"Failed to send post {post['id']} to Max: {str(e)}")
            # Keep transactions short here as well.
            await db.commit()

        # Keep the debug log bounded per stream. ``pending`` rows are never pruned:
        # they are still waiting for a successful publication.
        if monitor.log_all_posts:
            keep_ids = (await db.execute(
                select(Event.id).where(Event.monitor_id == monitor.id).order_by(Event.id.desc()).limit(MAX_LOGGED_EVENTS_PER_STREAM)
            )).scalars().all()
            if keep_ids:
                await db.execute(delete(Event).where(
                    Event.monitor_id == monitor.id,
                    Event.status != "pending",
                    Event.id.notin_(keep_ids),
                ))

        await db.commit()

    async def _retry_pending_events(self, db: AsyncSession, monitor: Monitor) -> None:
        """Re-send the posts whose publication failed for a transient reason.

        The cursor moves past a post as soon as it is collected, so without this
        queue a Max outage (or a network hiccup) lost those posts for good: they
        were marked ``failed`` and never retried. A retryable failure now keeps the
        event in the ``pending`` state and the event row itself acts as the queue —
        drained here, oldest first, with a bounded number of attempts per post.
        """
        rows = (await db.execute(
            select(Event)
            .where(Event.monitor_id == monitor.id, Event.status == "pending")
            .order_by(Event.original_date.is_(None), Event.original_date.asc(), Event.id.asc())
            .limit(MAX_RETRIES_PER_RUN)
        )).scalars().all()
        if not rows:
            return
        sent = 0
        for event in rows:
            try:
                ai_text, ai_error = self._ai_result_from_event(event)
                await self._send_to_max(db, monitor, self._post_from_event(event), ai_text, ai_error, event=event)
                if event.status == "sent":
                    sent += 1
            except Exception as e:  # noqa: BLE001 - one bad row must not stop the queue
                await self._add_log(db, monitor.id, "error", f"Повторная отправка поста {event.original_id} не удалась: {e}")
            await db.commit()
        await self._add_log(db, monitor.id, "info", f"Повторная отправка из очереди: отправлено {sent} из {len(rows)}")
        await db.commit()

    @staticmethod
    def _attachments_from_event(event: Event) -> list:
        """Restore the VK attachments stored as a Python literal on the event.

        ``Event.attachments`` keeps ``str(post["attachments"])`` (the VK JSON
        rendered by Python), so ``ast.literal_eval`` can read it back — no code is
        executed, only literals are parsed.
        """
        if not event.attachments:
            return []
        try:
            parsed = ast.literal_eval(event.attachments)
        except (ValueError, SyntaxError):
            return []
        return parsed if isinstance(parsed, list) else []

    def _post_from_event(self, event: Event) -> dict:
        """Rebuild the post dict of a queued event (the row is the queue)."""
        def _as_int(value, fallback):
            try:
                return int(value)
            except (TypeError, ValueError):
                return fallback

        return {
            "id": _as_int(event.original_id, event.original_id),
            "owner_id": _as_int(event.original_owner_id, event.original_owner_id),
            "url": event.original_url,
            "text": event.original_text or "",
            "date": event.original_date,
            "attachments": self._attachments_from_event(event),
            "likes": event.likes or 0,
            "reposts": event.reposts or 0,
            "comments": event.comments or 0,
            "views": event.views or 0,
            "er": event.er or 0.0,
        }

    @staticmethod
    def _ai_result_from_event(event: Event) -> tuple:
        """Recover the AI text/error stored on a queued event.

        A rewritten text is flagged by ``ai_filtered``; a failure is stored with
        the ``Ошибка ИИ: `` prefix (see ``_send_to_max``), in which case the
        original text is published again on the retry.
        """
        stored = event.ai_analysis_result or ""
        if event.ai_filtered and stored:
            return stored, None
        prefix = "Ошибка ИИ: "
        if stored.startswith(prefix):
            return None, stored[len(prefix):]
        return None, None

    async def _warn_skipped_posts(self, db: AsyncSession, monitor: Monitor, skipped: int) -> None:
        """Report a burst that did not fit into ``MAX_NEW_POSTS_PER_RUN``."""
        message = (
            f"Всплеск публикаций: не менее {skipped} самых старых постов не обработаны — "
            f"за прогон берётся не более {MAX_NEW_POSTS_PER_RUN} новых постов на источник. "
            "Уменьшите периодичность проверки потока, чтобы успевать за публикациями."
        )
        await self._add_log(db, monitor.id, "warning", message)
        await self._notify_once(db, monitor, f"Пропущены посты: {monitor.name}", message)
        await db.commit()

    def _new_event(self, monitor: Monitor, post: dict, status: str) -> Event:
        """Build an event row for a post (published ones and debug-logged ones)."""
        return Event(
            monitor_id=monitor.id,
            original_id=post.get("id", ""),
            original_owner_id=post.get("owner_id", ""),
            original_url=post.get("url", ""),
            original_text=post.get("text", ""),
            original_date=post.get("date"),
            attachments=str(post.get("attachments", [])) if post.get("attachments") else None,
            likes=post.get("likes", 0),
            reposts=post.get("reposts", 0),
            comments=post.get("comments", 0),
            views=post.get("views", 0),
            er=post.get("er", 0.0),
            status=status,
        )

    def _mark_event(self, event: Optional[Event], status: str, detail: Optional[str] = None) -> None:
        """Update a debug-logged event (no-op when the stream logs publications only)."""
        if event is None:
            return
        event.status = status
        if detail:
            event.error_message = detail[:500]

    async def _send_to_max(self, db: AsyncSession, monitor: Monitor, post: dict, ai_text: Optional[str] = None, ai_error: Optional[str] = None, event: Optional[Event] = None):
        async with self._publish_lock:
            max_channels = [c.strip() for c in (monitor.max_channels or "").split("\n") if c.strip()]
            config = get_settings()
            max_service = MaxService(token=config.max_bot_token)
            try:
                text_to_send = ai_text if ai_text else (post.get("text", "") or "(пост без текста)")
                media_error: Optional[Exception] = None
                try:
                    attachments = await self._build_max_attachments(max_service, monitor, post)
                except Exception as e:  # noqa: BLE001
                    # Подготовка медиа — тоже сетевой шаг: при сбое пост не должен
                    # потеряться (и не должен уйти без своих вложений).
                    attachments = []
                    media_error = e
                if event is None:
                    # Streams without the debug log only record publications.
                    event = self._new_event(monitor, post, "failed")
                    db.add(event)
                else:
                    event.status = "failed"
                    event.error_message = None
                if ai_text:
                    event.ai_analysis_result = ai_text
                    event.ai_filtered = True
                elif ai_error:
                    event.ai_analysis_result = f"Ошибка ИИ: {ai_error}"
                await db.flush()

                # A post counts as sent only when at least one channel really
                # accepted it: an unconfigured or unrecognised channel must not
                # produce a "sent" event nor inflate the published counter.
                sent_channels: list = []
                problems: list = []
                # ``retryable`` stays True while every reason is transient (network
                # hiccup, HTTP 5xx, rate limit): such a post is retried by the next
                # runs instead of being lost. A missing/mistyped channel or a 4xx
                # answer would repeat identically, so those posts are not queued.
                retryable = True
                if media_error is not None:
                    problems.append(f"не удалось подготовить медиа: {media_error}")
                    await self._add_log(db, monitor.id, "error", f"Max media error for post {post.get('id')}: {media_error}")
                elif not max_channels:
                    retryable = False
                    problems.append("в потоке не указан ни один Max-канал")
                # При сбое подготовки медиа каналы не трогаем: пост ждёт повтора.
                for channel in ([] if media_error is not None else max_channels):
                    try:
                        chat_id = await max_service.parse_chat_id(channel)
                        if not chat_id:
                            retryable = False
                            problems.append(f"канал '{channel}' не распознан")
                            await self._add_log(db, monitor.id, "warning", f"Не отправлено: канал '{channel}' не распознан (нужен chat_id или ссылка вида https://max.ru/chat/123)")
                            continue
                        await max_service.send_post_to_chat(chat_id, text_to_send, original_url=post.get("url"), attachments=attachments)
                        sent_channels.append(channel)
                        await self._add_log(db, monitor.id, "info", f"Post {post['id']} sent to Max channel {channel}")
                    except MaxBotError as e:
                        monitor.publication_errors += 1
                        if not _is_transient_max_error(e):
                            retryable = False
                        problems.append(f"{channel}: {e}")
                        await self._add_log(db, monitor.id, "error", f"Max publish error for {channel}: {e}")
                    except Exception as e:
                        monitor.publication_errors += 1
                        # Anything unexpected (no ``status_code``) counts as
                        # transient and is retried a few times.
                        retryable = retryable and _is_transient_max_error(e)
                        problems.append(f"{channel}: {e}")
                        await self._add_log(db, monitor.id, "error", f"Max error for {channel}: {e}")

                if sent_channels:
                    event.status = "sent"
                    event.sent_at = datetime.utcnow()
                    event.retry_attempts = 0
                    event.error_message = (("Не все каналы: " + "; ".join(problems))[:500] if problems else None)
                    monitor.posts_published += 1
                    if event.error_message:
                        await self._notify_once(db, monitor, f"Ошибка отправки в Max: {monitor.name}", event.error_message)
                else:
                    attempts = (event.retry_attempts or 0) + 1
                    event.retry_attempts = attempts
                    event.sent_at = None
                    detail = "Не отправлено: " + "; ".join(problems)
                    if retryable and attempts < MAX_PUBLISH_ATTEMPTS:
                        # Keep the post for the next runs (see ``_retry_pending_events``)
                        # instead of losing it: the cursor has already moved on.
                        event.status = "pending"
                        event.error_message = detail[:500]
                        await self._add_log(db, monitor.id, "warning", f"Post {post['id']} не отправлен, попытка {attempts}/{MAX_PUBLISH_ATTEMPTS}: остаётся в очереди")
                    else:
                        event.status = "failed"
                        if retryable:
                            detail += f" — попытки исчерпаны ({attempts})"
                        event.error_message = detail[:500]
                    await self._notify_once(db, monitor, f"Ошибка отправки в Max: {monitor.name}", event.error_message)
            finally:
                await max_service.aclose()

    def _prompt_allows_media(self, monitor: Monitor) -> bool:
        """Media is copied unless disabled on the stream or forbidden by the prompt."""
        # ``None`` (legacy rows) is treated as enabled.
        if monitor.copy_media is False:
            return False
        if monitor.use_ai and monitor.ai_prompt:
            low = monitor.ai_prompt.lower()
            if any(marker in low for marker in self._NO_MEDIA_MARKERS):
                return False
        return True

    async def _build_max_attachments(self, max_service: MaxService, monitor: Monitor, post: dict) -> list:
        """Copy the VK post's photos/docs to Max as ready-to-use attachments."""
        if not self._prompt_allows_media(monitor):
            return []
        raw = post.get("attachments") or []
        if not isinstance(raw, list):
            return []
        attachments = []
        for att in raw[:MAX_MEDIA_ATTACHMENTS]:
            if not isinstance(att, dict):
                continue
            url = None
            if att.get("type") == "photo":
                sizes = (att.get("photo") or {}).get("sizes") or []
                if sizes:
                    largest = max(sizes, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
                    url = largest.get("url")
            elif att.get("type") == "doc":
                url = (att.get("doc") or {}).get("url")
            if not url:
                continue
            media = await max_service.media_attachment_from_url(url)
            if media:
                attachments.append(media)
        return attachments

    async def _warn_source(self, db: AsyncSession, monitor: Monitor, source: str, reason: str):
        """Report an unusable source: log it and surface it once in the UI.

        A broken link or a closed/blocked wall must not stop the other sources,
        so this only records the problem. To make it visible without spamming the
        notifications bell on every run, at most one unread notification per
        (stream, source) is kept until the user reads or clears it.
        """
        await self._add_log(db, monitor.id, "warning", f"Источник {source} недоступен: {reason}")
        await self._notify_once(db, monitor, f"Источник недоступен: {monitor.name}", f"{source}: {reason}")

    async def _notify_once(self, db: AsyncSession, monitor: Monitor, title: str, message: str) -> None:
        """Create at most one unread notification per (stream, title, message).

        Keeps the bell informative without spamming it on every run: while the
        same problem stays unread, no duplicate notification is created.
        """
        existing = await db.execute(
            select(Notification).where(
                Notification.monitor_id == monitor.id,
                Notification.title == title,
                Notification.is_read.is_(False),
                Notification.message == message,
            )
        )
        if existing.scalars().first():
            return
        await NotificationService.create_notification(
            db, title=title, message=message, level="warning", monitor_id=monitor.id
        )

    async def _add_log(self, db: AsyncSession, monitor_id: int, level: str, message: str, details: dict = None):
        log = Log(monitor_id=monitor_id, level=level, message=redact_sensitive_data(message), details=redact_sensitive_data(details))
        db.add(log)