# -*- coding: utf-8 -*-
"""Регрессия: смена ADMIN_PASSWORD в .env отзывает ранее выпущенные JWT.

При старте с новым паролем ``seed_initial_user()`` меняет bcrypt-хеш в БД, но
раньше не увеличивал ``token_version`` — и старый токен продолжал работать до
истечения своих 24 часов. Обратная ситуация тоже важна: перезапуск с неизменным
``.env`` не должен разлогинивать администратора.

Запуск из каталога backend:  python tests/test_admin_env_password.py
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

import app.main as main_module  # noqa: E402
import app.models.database as database_module  # noqa: E402
from app.core.security import (  # noqa: E402
    create_access_token,
    decode_token,
    get_password_hash,
    token_version_ok,
    verify_password,
)
from app.models.database import Base  # noqa: E402
from app.models.models import User  # noqa: E402

OLD_PASSWORD = "старый-пароль"
NEW_PASSWORD = "новый-пароль"
ADMIN = "admin"


def _run_seed(admin_password: str, stored_password=None, stored_hash=None):
    """Выполнить настоящий seed_initial_user() на in-memory БД.

    ``stored_hash`` позволяет положить в БД легаси-значение (plaintext) вместо
    bcrypt-хеша; ``None`` в обоих аргументах — админа в БД ещё нет.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        if stored_hash is not None or stored_password is not None:
            value = stored_hash if stored_hash is not None else get_password_hash(stored_password)
            async with sessions() as db:
                db.add(User(username=ADMIN, password_hash=value, role="admin",
                            is_active=True, token_version=1))
                await db.commit()

        original_settings = main_module.get_settings
        original_sessions = database_module.AsyncSessionLocal
        main_module.get_settings = lambda: SimpleNamespace(
            admin_username=ADMIN, admin_password=admin_password
        )
        database_module.AsyncSessionLocal = sessions
        try:
            await main_module.seed_initial_user()
            async with sessions() as db:
                return (await db.execute(select(User).where(User.username == ADMIN))).scalars().one()
        finally:
            main_module.get_settings = original_settings
            database_module.AsyncSessionLocal = original_sessions
            await engine.dispose()

    return asyncio.run(scenario())


def test_env_password_change_revokes_old_tokens():
    """Новый ADMIN_PASSWORD → хеш обновлён, токены старой версии отвергаются."""
    user = _run_seed(NEW_PASSWORD, stored_password=OLD_PASSWORD)
    assert verify_password(NEW_PASSWORD, user.password_hash)
    assert not verify_password(OLD_PASSWORD, user.password_hash)
    assert user.token_version == 2, user.token_version

    # Токен, выпущенный до смены пароля (tv=1), больше не принимается.
    old_payload = decode_token(create_access_token({"sub": ADMIN, "tv": 1}))
    assert token_version_ok(old_payload, user.token_version) is False
    # А токен с новой версией — принимается.
    new_payload = decode_token(create_access_token({"sub": ADMIN, "tv": user.token_version}))
    assert token_version_ok(new_payload, user.token_version) is True


def test_unchanged_env_does_not_log_anyone_out():
    """Перезапуск с тем же паролем не должен отзывать сессии."""
    user = _run_seed(OLD_PASSWORD, stored_password=OLD_PASSWORD)
    assert user.token_version == 1, user.token_version
    assert verify_password(OLD_PASSWORD, user.password_hash)


def test_legacy_plaintext_is_not_treated_as_a_change():
    """Legacy-значение без bcrypt с тем же паролем — не смена пароля."""
    user = _run_seed(OLD_PASSWORD, stored_hash=OLD_PASSWORD)
    assert user.token_version == 1, user.token_version
    assert verify_password(OLD_PASSWORD, user.password_hash)


def test_missing_admin_is_created_with_first_version():
    """Если админа в БД нет, он создаётся с текущей версией токена."""
    user = _run_seed(NEW_PASSWORD)
    assert verify_password(NEW_PASSWORD, user.password_hash)
    assert user.token_version == 1, user.token_version
    assert user.role == "admin" and user.is_active


_TESTS = [
    ("новый ADMIN_PASSWORD отзывает старые токены", test_env_password_change_revokes_old_tokens),
    ("тот же пароль не разлогинивает", test_unchanged_env_does_not_log_anyone_out),
    ("legacy plaintext не считается сменой пароля", test_legacy_plaintext_is_not_treated_as_a_change),
    ("админ создаётся с первой версией", test_missing_admin_is_created_with_first_version),
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
