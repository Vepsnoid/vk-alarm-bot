# -*- coding: utf-8 -*-
"""Регрессия: auth.py должен читать .env-настройки на каждый запрос.

Раньше модуль кэшировал объект настроек на импорте (``settings = get_settings()``),
поэтому после переименования .env-админа из панели (``set_admin_username`` →
запись в .env + ``get_settings.cache_clear()``) аварийный вход по **новому** логину
не работал до перезапуска процесса: модуль продолжал сравнивать со старым.

Запуск из каталога backend:  python tests/test_auth_settings_reload.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

import app.routers.auth as auth_module  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.security import create_access_token, decode_token  # noqa: E402
from app.models.database import Base  # noqa: E402
from app.routers.auth import LoginRequest, get_current_user, login  # noqa: E402

OLD_LOGIN, OLD_PASSWORD = "admin", "старый-пароль"
NEW_LOGIN, NEW_PASSWORD = "boss", "новый-пароль"


class _EnvPatch:
    """Подмена ADMIN_USERNAME/ADMIN_PASSWORD «как из панели»: env + cache_clear."""

    def __init__(self, login: str, password: str):
        self.login = login
        self.password = password
        self.saved = {}

    def apply(self):
        for key, value in (("ADMIN_USERNAME", self.login), ("ADMIN_PASSWORD", self.password)):
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value
        get_settings.cache_clear()   # то же, что делают set_admin_username/set_admin_password

    def restore(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


def _sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def create_all():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create_all())
    return engine, factory


def _bearer(sub: str, tv: int = 0):
    return HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=create_access_token({"sub": sub, "tv": tv})
    )


def test_env_admin_rename_takes_effect_without_restart():
    """После переименования .env-админа аварийный вход работает в том же процессе."""
    engine, factory = _sessions()
    old_env = _EnvPatch(OLD_LOGIN, OLD_PASSWORD)
    try:
        old_env.apply()

        async def scenario():
            results = {}
            async with factory() as db:
                # исходное состояние: работает старый логин
                token = await login(LoginRequest(username=OLD_LOGIN, password=OLD_PASSWORD), db=db)
                results["old_sub"] = decode_token(token["access_token"])["sub"]

                # «переименовали админа из панели»: .env обновлён, кэш настроек сброшен
                new_env = _EnvPatch(NEW_LOGIN, NEW_PASSWORD)
                new_env.apply()

                # без перезапуска процесса: логин по новому имени
                token = await login(LoginRequest(username=NEW_LOGIN, password=NEW_PASSWORD), db=db)
                payload = decode_token(token["access_token"])
                results["new_sub"] = payload["sub"]
                results["new_tv"] = payload["tv"]

                # и recovery-ветка get_current_user тоже знает новый логин
                user = await get_current_user(credentials=_bearer(NEW_LOGIN), db=db)
                results["fallback_user"] = (user["username"], user["role"])

                # старый логин больше не пускают
                for attempt in (
                    lambda: login(LoginRequest(username=OLD_LOGIN, password=OLD_PASSWORD), db=db),
                    lambda: get_current_user(credentials=_bearer(OLD_LOGIN), db=db),
                ):
                    try:
                        await attempt()
                        results.setdefault("old_rejected", False)
                    except HTTPException as exc:
                        results.setdefault("codes", []).append(exc.status_code)
            return results

        results = asyncio.run(scenario())
        assert results["old_sub"] == OLD_LOGIN, results
        assert results["new_sub"] == NEW_LOGIN, results
        assert results["new_tv"] == 0, results          # именно recovery-токен
        assert results["fallback_user"] == (NEW_LOGIN, "admin"), results
        assert results.get("old_rejected") is not True, results
        assert results.get("codes") == [401, 401], results
    finally:
        old_env.restore()
        asyncio.run(engine.dispose())


def test_auth_module_does_not_cache_settings():
    """Страховка от возврата кэша настроек на уровне модуля."""
    assert not hasattr(auth_module, "settings"), "auth.py снова держит settings на импорте"


_TESTS = [
    ("смена .env-админа работает без перезапуска", test_env_admin_rename_takes_effect_without_restart),
    ("auth.py не кэширует настройки", test_auth_module_does_not_cache_settings),
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
