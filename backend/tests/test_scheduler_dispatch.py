# -*- coding: utf-8 -*-
"""Регрессия: тик диспетчера не ждёт окончания долгого прогона.

Раньше задание планировщика ждало прогон целиком, и пока он шёл (минуты), APScheduler
на каждом тике писал «Execution of job 'run_scheduled_monitors' skipped: maximum
number of running instances reached (1)» — выглядело как ошибка, хотя прогон просто
продолжался. Теперь тик запускает прогон отдельной задачей и сразу возвращается:
лишние тики пропускаются с понятной строкой в логе, прогон не прерывается.

Запуск из каталога backend:  python tests/test_scheduler_dispatch.py
"""
import asyncio
import contextlib
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.main as main_module  # noqa: E402
from app.services.monitor_processor import MonitorProcessor  # noqa: E402

RUN_SECONDS = 0.25


def _patch_processor(started):
    """Подменить прогон: фиксируем запуски и спим вместо работы с VK/Max."""
    original = MonitorProcessor.process_due_monitors

    async def fake(self, default_interval_minutes=15):
        started.append(default_interval_minutes)
        await asyncio.sleep(RUN_SECONDS)

    MonitorProcessor.process_due_monitors = fake
    return original


def _reset_dispatch_task():
    """Сбросить состояние между тестами (задача из прошлого цикла событий)."""
    task = main_module._dispatch_task
    main_module._dispatch_task = None
    if task is not None and not task.done():
        with contextlib.suppress(Exception):
            task.cancel()


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_tick_does_not_wait_for_the_run():
    """Тик возвращается сразу, второй тик при идущем прогоне не запускает новый."""
    _reset_dispatch_task()
    started = []
    original = _patch_processor(started)
    try:
        async def scenario():
            loop = asyncio.get_running_loop()
            started_at = loop.time()
            await main_module.run_scheduled_monitors()
            elapsed = loop.time() - started_at
            assert elapsed < RUN_SECONDS / 2, elapsed        # тик не ждёт прогон
            await asyncio.sleep(0.05)                         # задача успевает стартовать
            assert started == [15], started

            await main_module.run_scheduled_monitors()        # тик во время прогона
            await asyncio.sleep(0.05)
            assert started == [15], started                   # второй прогон не начался

            await asyncio.sleep(RUN_SECONDS + 0.15)
            assert main_module._dispatch_task.done(), "прогон должен завершиться сам"

            await main_module.run_scheduled_monitors()        # следующий тик — новый прогон
            await asyncio.sleep(RUN_SECONDS + 0.15)
            return started

        started = asyncio.run(scenario())
        assert started == [15, 15], started
    finally:
        MonitorProcessor.process_due_monitors = original
        _reset_dispatch_task()


def test_skipped_tick_is_logged():
    """Пропущенный тик виден в логе понятной строкой (а не WARNING планировщика)."""
    _reset_dispatch_task()
    capture = _LogCapture()
    logger = logging.getLogger("app.main")
    logger.addHandler(capture)
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    started = []
    original = _patch_processor(started)
    try:
        async def scenario():
            await main_module.run_scheduled_monitors()
            await main_module.run_scheduled_monitors()   # должен быть пропущен
            await asyncio.sleep(RUN_SECONDS + 0.15)

        asyncio.run(scenario())
        assert any("тик пропущен" in message for message in capture.messages), capture.messages
    finally:
        MonitorProcessor.process_due_monitors = original
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        _reset_dispatch_task()


_TESTS = [
    ("тик не ждёт прогон и не запускает второй", test_tick_does_not_wait_for_the_run),
    ("пропущенный тик пишется в лог", test_skipped_tick_is_logged),
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
