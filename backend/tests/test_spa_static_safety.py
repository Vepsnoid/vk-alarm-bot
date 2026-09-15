# -*- coding: utf-8 -*-
"""Регрессия: SPA catch-all не должен отдавать файлы вне frontend/dist.

Раньше ``os.path.join(frontend_dist, full_path)`` пропускал ``..``, и запрос
``/../../.env`` (а также ``/%2e%2e/%2e%2e/.env``) возвращал корневой .env со
всеми токенами. Здесь проверяется, что такие пути отдают index.html, а обычные
файлы SPA по-прежнему работают.

Запуск из каталога backend:  python tests/test_spa_static_safety.py
"""
import asyncio
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.responses import FileResponse  # noqa: E402

from app import main  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND_DIST = os.path.realpath(main.frontend_dist)
INDEX = os.path.realpath(os.path.join(FRONTEND_DIST, "index.html"))
HAS_SPA = hasattr(main, "serve_spa")


def _served_path(full_path: str) -> str:
    """Путь, который реально отдаёт catch-all для request-path (уже декодированного)."""
    response = asyncio.run(main.serve_spa(full_path))
    assert isinstance(response, FileResponse), type(response)
    return os.path.realpath(response.path)


def test_env_is_not_served():
    """Обходы каталога должны отдавать index.html, а не файлы проекта."""
    if not HAS_SPA:
        return
    assert os.path.isfile(os.path.join(PROJECT_ROOT, ".env")), "для теста нужен .env в корне"
    for probe in (
        "../../.env",              # обычный обход
        "%2e%2e/%2e%2e/.env",      # закодированные точки (uvicorn декодирует)
        "..%2f..%2f.env",          # частично закодированный слэш
        "..\\..\\.env",            # Windows-разделители
        "assets/../../.env",       # обход через каталог статики
    ):
        decoded = urllib.parse.unquote(probe)
        served = _served_path(decoded)
        assert served == INDEX, f"{probe!r} отдан файл {served}"


def test_real_assets_are_still_served():
    """Обычная отдача SPA не сломана."""
    if not HAS_SPA:
        return
    assert _served_path("") == INDEX
    assert _served_path("some/spa/route") == INDEX
    logo = os.path.realpath(os.path.join(FRONTEND_DIST, "logo.png"))
    if os.path.isfile(logo):
        assert _served_path("logo.png") == logo


def test_resolve_frontend_file_rejects_escapes():
    """Разрешение пути: всё, что вне dist, отбрасывается."""
    if not hasattr(main, "resolve_frontend_file"):
        return
    for probe in ("../../.env", "..\\..\\.env", "/etc/passwd", "assets/../../../vk_alarm.db"):
        assert main.resolve_frontend_file(probe) is None, probe
    logo = os.path.realpath(os.path.join(FRONTEND_DIST, "logo.png"))
    if os.path.isfile(logo):
        assert main.resolve_frontend_file("logo.png") == logo


_TESTS = [
    ("обход каталога не отдаёт .env", test_env_is_not_served),
    ("страницы и ассеты SPA работают", test_real_assets_are_still_served),
    ("resolve_frontend_file отсекает выход из dist", test_resolve_frontend_file_rejects_escapes),
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
