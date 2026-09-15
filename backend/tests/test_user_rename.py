# -*- coding: utf-8 -*-
"""Регрессия: переименование .env-админа не должно ломать связь с .env.

Раньше проверка «это .env-админ?» делалась уже ПОСЛЕ смены имени, поэтому:
  * переименование админа не обновляло ADMIN_USERNAME — при следующем старте
    seed_initial_user() создавал второго админа под старым логином;
  * смена пароля пользователя, переименованного В имя .env-админа, писала его
    пароль в .env как ADMIN_PASSWORD.

Запуск из каталога backend:  python tests/test_user_rename.py
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

import app.routers.users as users_module  # noqa: E402
from app.core.security import get_password_hash, verify_password  # noqa: E402
from app.models.database import Base  # noqa: E402
from app.models.models import User  # noqa: E402
from app.routers.users import UserUpdate, update_user  # noqa: E402

ADMIN_ID = 1
ENV_ADMIN = "admin"
ADMIN_TOKEN = {"id": ADMIN_ID, "role": "admin", "username": ENV_ADMIN}


class _EnvStub:
    """Подмена .env: помним записанные значения так же, как ``config.set_*``."""

    def __init__(self, admin_username: str):
        self.admin_username = admin_username
        self.usernames: list = []
        self.passwords: list = []

    def record_username(self, value: str) -> None:
        self.usernames.append(value)
        self.admin_username = value  # настоящий set_admin_username меняет значение в .env

    def record_password(self, value: str) -> None:
        self.passwords.append(value)


def _run(users, scenario):
    """Прогнать сценарий на in-memory БД с подменёнными настройками и .env."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    env = _EnvStub(ENV_ADMIN)

    async def fill():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            for spec in users:
                db.add(User(
                    username=spec["username"],
                    password_hash=get_password_hash(spec.get("password", "пароль")),
                    role=spec.get("role", "user"),
                    is_active=True,
                    token_version=spec.get("token_version", 1),
                ))
            await db.commit()

    async def runner():
        async with sessions() as db:
            return await scenario(db, env)

    original = (users_module.get_settings, users_module.set_admin_username, users_module.set_admin_password)
    users_module.get_settings = lambda: SimpleNamespace(admin_username=env.admin_username)
    users_module.set_admin_username = env.record_username
    users_module.set_admin_password = env.record_password
    try:
        asyncio.run(fill())
        result = asyncio.run(runner())
    finally:
        users_module.get_settings, users_module.set_admin_username, users_module.set_admin_password = original
        asyncio.run(engine.dispose())
    return result, env, sessions


def test_renaming_env_admin_updates_env():
    """Переименовали .env-админа → в .env пишется новый логин (иначе второй админ)."""
    async def scenario(db, env):
        return await update_user(ADMIN_ID, UserUpdate(username="root"), db=db, admin=dict(ADMIN_TOKEN))

    result, env, sessions = _run([{"username": ENV_ADMIN, "role": "admin"}], scenario)

    assert result.username == "root", result.username
    assert env.usernames == ["root"], env.usernames
    assert env.passwords == [], env.passwords
    assert result.role == "admin", result.role


def test_password_of_same_named_user_is_not_written_to_env():
    """Пароль пользователя, занявшего освободившийся логин .env-админа, не уходит в .env."""
    async def scenario(db, env):
        # Админа переименовали: имя «admin» освободилось, .env теперь указывает на root.
        await update_user(ADMIN_ID, UserUpdate(username="root"), db=db, admin=dict(ADMIN_TOKEN))
        # Обычный пользователь занимает это имя и меняет пароль.
        return await update_user(2, UserUpdate(username=ENV_ADMIN, password="новый-пароль"), db=db, admin=dict(ADMIN_TOKEN))

    result, env, sessions = _run([{"username": ENV_ADMIN, "role": "admin"}, {"username": "user1"}], scenario)

    assert result.username == ENV_ADMIN, result.username
    assert env.passwords == [], env.passwords
    assert env.usernames == ["root"], env.usernames
    assert verify_password("новый-пароль", result.password_hash)


def test_env_admin_password_change_is_still_synced():
    """Пароль настоящего .env-админа по-прежнему сохраняется в .env."""
    async def scenario(db, env):
        return await update_user(ADMIN_ID, UserUpdate(password="новый-админ-пароль"), db=db, admin=dict(ADMIN_TOKEN))

    result, env, sessions = _run([{"username": ENV_ADMIN, "role": "admin"}], scenario)

    assert env.passwords == ["новый-админ-пароль"], env.passwords
    assert result.token_version == 2, result.token_version


_TESTS = [
    ("переименование .env-админа пишется в .env", test_renaming_env_admin_updates_env),
    ("пароль однофамильца не уходит в .env", test_password_of_same_named_user_is_not_written_to_env),
    ("пароль .env-админа синхронизируется", test_env_admin_password_change_is_still_synced),
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
