# -*- coding: utf-8 -*-
"""Регрессия: числовые настройки потока валидируются на сервере.

Фронтенд ограничивает поля, но API доступен напрямую: отрицательный интервал
делал поток «просроченным» на каждом тике планировщика (раз в 30 с), а
перевёрнутое окно ER отсекало все посты. Теперь это 422, а значения из старых
записей БД нормализуются в процессоре.

Запуск из каталога backend:  python tests/test_monitor_validation.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.core.limits import bounded_float, bounded_int, er_range_error  # noqa: E402
from app.routers.monitors import (  # noqa: E402
    MonitorCreate,
    MonitorUpdate,
    raise_if_invalid_er_range,
)

BASE = {"name": "Тест", "source_channels": "https://vk.com/durov"}


def _rejected(model, **kwargs) -> bool:
    try:
        model(**{**BASE, **kwargs})
    except ValidationError:
        return True
    return False


def test_check_interval_bounds():
    for bad in (0, -5, -1440, 1441, 100000):
        assert _rejected(MonitorCreate, check_interval_minutes=bad), f"принят интервал {bad}"
        assert _rejected(MonitorUpdate, check_interval_minutes=bad), f"принят интервал {bad}"
    assert MonitorCreate(**BASE, check_interval_minutes=1).check_interval_minutes == 1
    assert MonitorCreate(**BASE, check_interval_minutes=1440).check_interval_minutes == 1440
    # Частичное обновление без интервала не должно падать.
    assert MonitorUpdate(name="Новое имя").check_interval_minutes is None


def test_er_bounds():
    for bad in (-1, 101, 1000):
        assert _rejected(MonitorCreate, min_er=bad), f"принят min_er {bad}"
        assert _rejected(MonitorUpdate, max_er=bad), f"принят max_er {bad}"
    assert MonitorCreate(**BASE, min_er=0, max_er=100).max_er == 100

    # Перевёрнутое окно ловится на уровне роутера (нужны оба значения).
    assert er_range_error(60, 10) is not None
    assert er_range_error(10, 60) is None
    assert er_range_error(None, 10) is None
    try:
        raise_if_invalid_er_range(60, 10)
    except HTTPException as e:
        assert e.status_code == 422 and "ER" in e.detail
    else:
        raise AssertionError("перевёрнутый ER не отклонён")
    raise_if_invalid_er_range(10, 60)  # корректное окно не должно бросать


def test_ai_max_length_bounds():
    for bad in (0, -100, 99, 15001):
        assert _rejected(MonitorCreate, ai_max_length=bad), f"принят ai_max_length {bad}"
    assert MonitorCreate(**BASE, ai_max_length=100).ai_max_length == 100
    assert MonitorCreate(**BASE, ai_max_length=15000).ai_max_length == 15000


def test_legacy_values_are_clamped():
    """Значения из старых записей БД не должны ломать прогон."""
    assert bounded_int(-5, 15, 1, 1440) == 1
    assert bounded_int(0, 15, 1, 1440) == 1
    assert bounded_int(None, 15, 1, 1440) == 15
    assert bounded_int("мусор", 15, 1, 1440) == 15
    assert bounded_int(99999, 15, 1, 1440) == 1440
    assert bounded_int(-1, 5000, 100, 15000) == 100
    assert bounded_float(None, 100.0, 0.0, 100.0) == 100.0
    assert bounded_float(-10, 0.0, 0.0, 100.0) == 0.0
    assert bounded_float(500, 100.0, 0.0, 100.0) == 100.0


_TESTS = [
    ("интервал проверки: 1..1440", test_check_interval_bounds),
    ("окно ER: 0..100 и min<=max", test_er_bounds),
    ("макс. символов ИИ: 100..15000", test_ai_max_length_bounds),
    ("старые значения нормализуются", test_legacy_values_are_clamped),
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
