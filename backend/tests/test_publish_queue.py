# -*- coding: utf-8 -*-
"""Регрессия: сбой публикации в Max не должен терять посты.

Курсор потока уходит вперёд сразу после сбора постов, поэтому раньше упавшая
отправка означала безвозвратную потерю: событие помечалось ``failed`` и больше
не отправлялось. Теперь временный сбой (сеть, 5xx, 429) оставляет событие в
статусе ``pending``, и следующие прогоны повторяют отправку (``Event`` сам
служит очередью); 4xx и ошибки конфигурации остаются терминальными, а число
попыток ограничено.

Запуск из каталога backend:  python tests/test_publish_queue.py
"""
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

import app.services.monitor_processor as processor_module  # noqa: E402
from app.models.database import Base  # noqa: E402
from app.models.models import Event, Log, Monitor, Notification  # noqa: E402
from app.services.max_service import MaxBotError  # noqa: E402
from app.services.monitor_processor import MAX_PUBLISH_ATTEMPTS, MonitorProcessor  # noqa: E402

POST = {
    "id": 101,
    "owner_id": -1,
    "url": "https://vk.com/wall-1_101",
    "text": "Первый пост",
    "date": datetime(2024, 1, 1, 12, 0, 0),
    "attachments": [],
    "likes": 1,
    "reposts": 2,
    "comments": 3,
    "views": 4,
    "er": 5.0,
}


class FakeMaxService:
    """Заглушка MaxService: помнит отправки и падает по заданному сценарию."""

    sent: list = []
    error: object = None
    # Каналы, для которых надо бросать ``error`` (пусто = для всех).
    fail_chats: set = set()

    def __init__(self, token: str = ""):
        self.token = token

    async def parse_chat_id(self, channel: str):
        value = (channel or "").strip()
        return value if value.lstrip("-").isdigit() else None

    async def send_post_to_chat(self, chat_id, text, original_url=None, attachments=None):
        fails = FakeMaxService.fail_chats
        if FakeMaxService.error is not None and (not fails or chat_id in fails):
            raise FakeMaxService.error
        # Настоящий MaxService дописывает ссылку на источник сам, поэтому здесь
        # запоминаем и её: важно, что ссылка восстановлена из события.
        FakeMaxService.sent.append((chat_id, text, original_url))
        return True

    async def aclose(self):
        pass


def _reset_fake():
    FakeMaxService.sent = []
    FakeMaxService.error = None
    FakeMaxService.fail_chats = set()


def _new_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, sessions


def _processor():
    """Процессор с заглушкой Max и без обращения к сети за медиа."""
    original = processor_module.MaxService
    processor_module.MaxService = FakeMaxService
    proc = MonitorProcessor()

    async def no_attachments(max_service, monitor, post):
        return []

    proc._build_max_attachments = no_attachments
    return proc, original


async def _prepare(sessions):
    """Создать таблицы и один поток; вернуть его id."""
    async with sessions() as db:
        async with db.bind.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        monitor = Monitor(name="Тест", source_channels="vk.com/a", max_channels="123", is_active=True)
        db.add(monitor)
        await db.commit()
        await db.refresh(monitor)
        return monitor.id


async def _events(db):
    return (await db.execute(select(Event).order_by(Event.id))).scalars().all()


def test_transient_failure_is_retried():
    """502 → статус pending, следующий прогон доставляет пост."""
    _reset_fake()

    async def scenario():
        engine, sessions = _new_engine()
        proc, original = _processor()
        try:
            monitor_id = await _prepare(sessions)
            async with sessions() as db:
                monitor = await db.get(Monitor, monitor_id)
                FakeMaxService.error = MaxBotError("HTTP 502: bad gateway", 502)
                await proc._send_to_max(db, monitor, dict(POST), None, None)
                await db.commit()

                event = (await _events(db))[0]
                assert event.status == "pending", event.status
                assert event.retry_attempts == 1, event.retry_attempts
                assert FakeMaxService.sent == [], FakeMaxService.sent

                # Max снова доступен: очередь доставляет пост.
                FakeMaxService.error = None
                await proc._retry_pending_events(db, monitor)

                event = (await _events(db))[0]
                assert event.status == "sent", (event.status, event.error_message)
                assert event.retry_attempts == 0, event.retry_attempts
                assert event.sent_at is not None
                assert len(FakeMaxService.sent) == 1, FakeMaxService.sent
                chat_id, text, original_url = FakeMaxService.sent[0]
                assert chat_id == "123", chat_id
                assert text.startswith("Первый пост"), text
                assert original_url == "https://vk.com/wall-1_101", original_url
        finally:
            await proc.vk.aclose()
            processor_module.MaxService = original
            await engine.dispose()

    asyncio.run(scenario())


def test_attempts_are_bounded():
    """Постоянный 5xx не должен повторяться вечно."""
    _reset_fake()

    async def scenario():
        engine, sessions = _new_engine()
        proc, original = _processor()
        try:
            monitor_id = await _prepare(sessions)
            async with sessions() as db:
                monitor = await db.get(Monitor, monitor_id)
                FakeMaxService.error = MaxBotError("HTTP 503: unavailable", 503)
                await proc._send_to_max(db, monitor, dict(POST), None, None)
                await db.commit()
                for _ in range(MAX_PUBLISH_ATTEMPTS + 2):
                    await proc._retry_pending_events(db, monitor)

                event = (await _events(db))[0]
                assert event.status == "failed", event.status
                assert event.retry_attempts == MAX_PUBLISH_ATTEMPTS, event.retry_attempts
                assert "попытки исчерпаны" in (event.error_message or ""), event.error_message
                assert FakeMaxService.sent == []
        finally:
            await proc.vk.aclose()
            processor_module.MaxService = original
            await engine.dispose()

    asyncio.run(scenario())


def test_terminal_errors_are_not_queued():
    """4xx (неверный токен/чат) и нераспознанный канал не ставятся в очередь."""
    _reset_fake()

    async def scenario():
        engine, sessions = _new_engine()
        proc, original = _processor()
        try:
            monitor_id = await _prepare(sessions)
            async with sessions() as db:
                monitor = await db.get(Monitor, monitor_id)
                FakeMaxService.error = MaxBotError("HTTP 400: bad request", 400)
                await proc._send_to_max(db, monitor, dict(POST), None, None)
                await db.commit()
                event = (await _events(db))[0]
                assert event.status == "failed", event.status
                assert event.retry_attempts == 1, event.retry_attempts

                # Очередь пуста: повторный прогон ничего не берёт.
                await proc._retry_pending_events(db, monitor)
                event = (await _events(db))[0]
                assert event.status == "failed", event.status

                # Нераспознанный канал — тоже терминальная ошибка конфигурации.
                FakeMaxService.error = None
                monitor.max_channels = "не-канал"
                await proc._send_to_max(db, monitor, dict(POST), None, None)
                await db.commit()
                event = (await _events(db))[1]
                assert event.status == "failed", (event.status, event.error_message)
                assert "не распознан" in (event.error_message or ""), event.error_message
                assert FakeMaxService.sent == []
        finally:
            await proc.vk.aclose()
            processor_module.MaxService = original
            await engine.dispose()

    asyncio.run(scenario())


def test_ai_text_and_original_survive_retry():
    """Текст, подготовленный ИИ, сохраняется в очереди и уходит при повторе."""
    _reset_fake()

    async def scenario():
        engine, sessions = _new_engine()
        proc, original = _processor()
        try:
            monitor_id = await _prepare(sessions)
            async with sessions() as db:
                monitor = await db.get(Monitor, monitor_id)
                # Сетевая ошибка (status_code=0) — временная.
                FakeMaxService.error = MaxBotError("Сетевая ошибка POST messages")
                await proc._send_to_max(db, monitor, dict(POST), "Текст от ИИ", None)
                await db.commit()
                event = (await _events(db))[0]
                assert event.status == "pending", event.status
                assert event.ai_analysis_result == "Текст от ИИ", event.ai_analysis_result

                FakeMaxService.error = None
                await proc._retry_pending_events(db, monitor)
                assert FakeMaxService.sent[0][1].startswith("Текст от ИИ"), FakeMaxService.sent

                # Сбой ИИ: в очередь попадает пометка, при повторе уходит оригинал.
                FakeMaxService.sent = []
                FakeMaxService.error = MaxBotError("Сетевая ошибка POST messages")
                await proc._send_to_max(db, monitor, dict(POST), None, "ИИ недоступен")
                await db.commit()
                event = (await _events(db))[1]
                assert event.status == "pending", event.status
                assert event.ai_analysis_result == "Ошибка ИИ: ИИ недоступен", event.ai_analysis_result
                assert not event.ai_filtered

                FakeMaxService.error = None
                await proc._retry_pending_events(db, monitor)
                assert FakeMaxService.sent[0][1].startswith("Первый пост"), FakeMaxService.sent
        finally:
            await proc.vk.aclose()
            processor_module.MaxService = original
            await engine.dispose()

    asyncio.run(scenario())


def test_event_row_restores_the_post():
    """Строка события восстанавливает пост (включая вложения) для повтора."""
    proc = MonitorProcessor()
    event = Event(
        monitor_id=1,
        original_id="101",
        original_owner_id="-1",
        original_url="https://vk.com/wall-1_101",
        original_text="Текст",
        original_date=datetime(2024, 1, 1),
        attachments="[{'type': 'photo', 'photo': {'sizes': [{'url': 'https://x/1.jpg', 'width': 10, 'height': 10}]}}]",
        likes=1, reposts=2, comments=3, views=4, er=5.0,
        status="pending", retry_attempts=1,
    )
    post = proc._post_from_event(event)
    assert post["id"] == 101 and post["owner_id"] == -1, post
    assert post["text"] == "Текст"
    assert post["attachments"][0]["type"] == "photo", post["attachments"]

    assert proc._ai_result_from_event(event) == (None, None)
    event.ai_analysis_result = "Ошибка ИИ: таймаут"
    assert proc._ai_result_from_event(event) == (None, "таймаут")
    event.ai_analysis_result = "Готовый текст"
    event.ai_filtered = True
    assert proc._ai_result_from_event(event) == ("Готовый текст", None)

    broken = Event(monitor_id=1, original_id="1", original_owner_id="-1", original_url="",
                   original_text="", attachments="не список", status="pending")
    assert proc._attachments_from_event(broken) == []
    assert proc._post_from_event(broken)["attachments"] == []


def test_media_failure_is_queued():
    """Сбой подготовки вложений не публикует пост без медиа и не теряет его."""
    _reset_fake()

    async def scenario():
        engine, sessions = _new_engine()
        proc, original = _processor()

        async def broken_attachments(max_service, monitor, post):
            raise RuntimeError("не удалось скачать фото")

        proc._build_max_attachments = broken_attachments
        try:
            monitor_id = await _prepare(sessions)
            async with sessions() as db:
                monitor = await db.get(Monitor, monitor_id)
                await proc._send_to_max(db, monitor, dict(POST), None, None)
                await db.commit()

                event = (await _events(db))[0]
                assert event.status == "pending", (event.status, event.error_message)
                assert "медиа" in (event.error_message or ""), event.error_message
                assert FakeMaxService.sent == [], "пост не должен уходить без вложений"
        finally:
            await proc.vk.aclose()
            processor_module.MaxService = original
            await engine.dispose()

    asyncio.run(scenario())


def test_partial_success_retries_only_the_failed_channel():
    """A доставлен, B упал, C доставлен → повтор только для B."""
    _reset_fake()

    async def scenario():
        engine, sessions = _new_engine()
        proc, original = _processor()
        try:
            monitor_id = await _prepare(sessions)
            async with sessions() as db:
                monitor = await db.get(Monitor, monitor_id)
                monitor.max_channels = "111\n222\n333"
                FakeMaxService.error = MaxBotError("HTTP 502: bad gateway", 502)
                FakeMaxService.fail_chats = {"222"}
                await proc._send_to_max(db, monitor, dict(POST), None, None)
                await db.commit()

                event = (await _events(db))[0]
                assert event.status == "pending", (event.status, event.error_message)
                assert event.delivery_state == {"sent": ["111", "333"], "pending": ["222"]}, event.delivery_state
                assert [c for c, _, _ in FakeMaxService.sent] == ["111", "333"], FakeMaxService.sent
                assert monitor.posts_published == 1, monitor.posts_published

                # B снова доступен: повтор достаётся только ему, A и C — нет.
                FakeMaxService.error = None
                await proc._retry_pending_events(db, monitor)

                event = (await _events(db))[0]
                assert event.status == "sent", (event.status, event.error_message)
                assert event.delivery_state == {"sent": ["111", "222", "333"], "pending": []}, event.delivery_state
                assert [c for c, _, _ in FakeMaxService.sent] == ["111", "333", "222"], FakeMaxService.sent
                assert monitor.posts_published == 1, "пост не должен считаться отправленным дважды"
                assert event.sent_at is not None
        finally:
            await proc.vk.aclose()
            processor_module.MaxService = original
            await engine.dispose()

    asyncio.run(scenario())


def test_terminal_channel_failure_does_not_block_delivery():
    """4xx на одном канале не блокирует событие: остальные доставлены."""
    _reset_fake()

    async def scenario():
        engine, sessions = _new_engine()
        proc, original = _processor()
        try:
            monitor_id = await _prepare(sessions)
            async with sessions() as db:
                monitor = await db.get(Monitor, monitor_id)
                monitor.max_channels = "111\n222"
                FakeMaxService.error = MaxBotError("HTTP 400: bad request", 400)
                FakeMaxService.fail_chats = {"222"}
                await proc._send_to_max(db, monitor, dict(POST), None, None)
                await db.commit()

                event = (await _events(db))[0]
                assert event.status == "sent", (event.status, event.error_message)
                assert event.delivery_state == {"sent": ["111"], "pending": []}, event.delivery_state
                assert "Не все каналы" in (event.error_message or ""), event.error_message
                assert "222" in (event.error_message or ""), event.error_message

                # Повторять нечего: очередь пуста, дубля для 111 не будет.
                await proc._retry_pending_events(db, monitor)
                assert [c for c, _, _ in FakeMaxService.sent] == ["111"], FakeMaxService.sent
        finally:
            await proc.vk.aclose()
            processor_module.MaxService = original
            await engine.dispose()

    asyncio.run(scenario())


def test_skipped_burst_is_reported():
    """Предупреждение о пропущенных постах попадает в журнал и уведомления."""
    _reset_fake()

    async def scenario():
        engine, sessions = _new_engine()
        proc, original = _processor()
        try:
            monitor_id = await _prepare(sessions)
            async with sessions() as db:
                monitor = await db.get(Monitor, monitor_id)
                await proc._warn_skipped_posts(db, monitor, 42, 200)
                logs = (await db.execute(select(Log).where(Log.monitor_id == monitor_id))).scalars().all()
                notifications = (await db.execute(
                    select(Notification).where(Notification.monitor_id == monitor_id)
                )).scalars().all()
                assert any("42" in (log.message or "") for log in logs), [log.message for log in logs]
                assert any("42" in (item.message or "") for item in notifications), [n.message for n in notifications]
        finally:
            await proc.vk.aclose()
            processor_module.MaxService = original
            await engine.dispose()

    asyncio.run(scenario())


def test_retry_notifications_do_not_multiply():
    """Повторные попытки не плодят одинаковые уведомления."""
    _reset_fake()

    async def scenario():
        engine, sessions = _new_engine()
        proc, original = _processor()
        try:
            monitor_id = await _prepare(sessions)
            async with sessions() as db:
                monitor = await db.get(Monitor, monitor_id)
                FakeMaxService.error = MaxBotError("HTTP 503: unavailable", 503)
                await proc._send_to_max(db, monitor, dict(POST), None, None)
                await db.commit()
                for _ in range(3):
                    await proc._retry_pending_events(db, monitor)

                async def unread():
                    return (await db.execute(
                        select(Notification).where(
                            Notification.monitor_id == monitor_id,
                            Notification.is_read.is_(False),
                        )
                    )).scalars().all()

                notifications = await unread()
                assert len(notifications) == 1, [n.message for n in notifications]

                # Исчерпание попыток — уже другое событие: второе уведомление.
                await proc._retry_pending_events(db, monitor)
                notifications = await unread()
                assert len(notifications) == 2, [n.message for n in notifications]
                assert any("попытки исчерпаны" in (n.message or "") for n in notifications), [n.message for n in notifications]
        finally:
            await proc.vk.aclose()
            processor_module.MaxService = original
            await engine.dispose()

    asyncio.run(scenario())


def test_legacy_pending_event_targets_all_channels():
    """Событие без delivery_state (старая запись) повторяется во все каналы."""
    _reset_fake()

    async def scenario():
        engine, sessions = _new_engine()
        proc, original = _processor()
        try:
            monitor_id = await _prepare(sessions)
            async with sessions() as db:
                monitor = await db.get(Monitor, monitor_id)
                monitor.max_channels = "111\n222"
                db.add(Event(
                    monitor_id=monitor_id, original_id="101", original_owner_id="-1",
                    original_url="https://vk.com/wall-1_101", original_text="Текст",
                    status="pending", retry_attempts=1, delivery_state=None,
                ))
                await db.commit()

                await proc._retry_pending_events(db, monitor)
                event = (await _events(db))[0]
                assert event.status == "sent", (event.status, event.error_message)
                assert [c for c, _, _ in FakeMaxService.sent] == ["111", "222"], FakeMaxService.sent
        finally:
            await proc.vk.aclose()
            processor_module.MaxService = original
            await engine.dispose()

    asyncio.run(scenario())


_TESTS = [
    ("временный сбой: пост уходит в очередь и доставляется", test_transient_failure_is_retried),
    ("число попыток ограничено", test_attempts_are_bounded),
    ("4xx и ошибки настройки не повторяются", test_terminal_errors_are_not_queued),
    ("текст ИИ/оригинал сохраняются при повторе", test_ai_text_and_original_survive_retry),
    ("событие восстанавливает пост для повтора", test_event_row_restores_the_post),
    ("сбой медиа: пост уходит в очередь", test_media_failure_is_queued),
    ("всплеск: пропущенные посты видны в журнале", test_skipped_burst_is_reported),
    ("частичный успех: повтор только для упавшего канала", test_partial_success_retries_only_the_failed_channel),
    ("4xx канала не блокирует доставку", test_terminal_channel_failure_does_not_block_delivery),
    ("повторы не плодят уведомления", test_retry_notifications_do_not_multiply),
    ("старое pending-событие: повтор во все каналы", test_legacy_pending_event_targets_all_channels),
]

if __name__ == "__main__":
    failures = 0
    for name, fn in _TESTS:
        try:
            fn()
            print(f"OK    {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    total = len(_TESTS)
    print(f"{total - failures}/{total} тестов пройдено")
    sys.exit(1 if failures else 0)
