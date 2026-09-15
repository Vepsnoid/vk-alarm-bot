# -*- coding: utf-8 -*-
"""Регрессия: CLI управляет пользователями так же строго, как веб-панель.

``manage.py set-password``/``set-role`` должны отзывать выданные JWT (иначе
украденный токен живёт ещё сутки), последнего активного администратора нельзя
понизить или удалить, а потоки удаляемого аккаунта переносятся другому
администратору (иначе они «висят» на несуществующем id до рестарта).

Запуск из каталога backend:  python tests/test_manage_cli.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

import manage  # noqa: E402
from app.core.security import get_password_hash, verify_password  # noqa: E402
from app.models.database import Base  # noqa: E402
from app.models.models import Monitor, User  # noqa: E402

USER_PASSWORD = "старт-пароль"


def _setup(users, monitors=()):
    """Создать in-memory БД, наполнить её и подменить сессии в manage.py."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def fill():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            for spec in users:
                db.add(User(
                    username=spec["username"],
                    password_hash=get_password_hash(spec.get("password", USER_PASSWORD)),
                    role=spec.get("role", "user"),
                    is_active=spec.get("is_active", True),
                    token_version=spec.get("token_version", 1),
                ))
            await db.commit()
            for owner in monitors:
                db.add(Monitor(name=f"поток {owner}", source_channels="vk.com/a", max_channels="1", owner_id=owner))
            await db.commit()
        return [u.id for u in (await _all_users(sessions))]

    ids = asyncio.run(fill())
    manage.AsyncSessionLocal = sessions
    return engine, sessions, ids


async def _all_users(sessions):
    async with sessions() as db:
        return (await db.execute(select(User).order_by(User.id))).scalars().all()


def _user(sessions, username):
    return asyncio.run(_find(sessions, username))


async def _find(sessions, username):
    async with sessions() as db:
        return (await db.execute(select(User).where(User.username == username))).scalars().one_or_none()


def _monitors(sessions):
    async def read():
        async with sessions() as db:
            return (await db.execute(select(Monitor).order_by(Monitor.id))).scalars().all()
    return asyncio.run(read())


def test_set_password_revokes_tokens():
    """CLI-пароль отзывает токены так же, как смена через панель."""
    engine, sessions, _ = _setup([{"username": "user1"}])
    try:
        code = asyncio.run(manage.set_password("user1", "новый-пароль"))
        assert code == 0, code
        user = _user(sessions, "user1")
        assert verify_password("новый-пароль", user.password_hash)
        assert not verify_password(USER_PASSWORD, user.password_hash)
        assert user.token_version == 2, user.token_version
    finally:
        asyncio.run(engine.dispose())


def test_set_role_revokes_tokens():
    """Смена роли перелогинивает: в токене осталась бы старая роль."""
    engine, sessions, _ = _setup([{"username": "admin", "role": "admin"}, {"username": "user1"}])
    try:
        assert asyncio.run(manage.set_role("user1", "admin")) == 0
        user = _user(sessions, "user1")
        assert user.role == "admin", user.role
        assert user.token_version == 2, user.token_version

        # Ничего не изменилось — версию не трогаем (иначе разлогинивали бы зря).
        assert asyncio.run(manage.set_role("user1", "admin")) == 0
        assert _user(sessions, "user1").token_version == 2
    finally:
        asyncio.run(engine.dispose())


def test_last_admin_cannot_be_demoted():
    """Последнего активного администратора нельзя понизить (как в панели)."""
    engine, sessions, _ = _setup([{"username": "admin", "role": "admin"}])
    try:
        assert asyncio.run(manage.set_role("admin", "user")) == 1
        user = _user(sessions, "admin")
        assert user.role == "admin", user.role
        assert user.token_version == 1, user.token_version
    finally:
        asyncio.run(engine.dispose())

    # А при наличии второго администратора понижение разрешено.
    engine, sessions, _ = _setup([{"username": "admin", "role": "admin"}, {"username": "boss", "role": "admin"}])
    try:
        assert asyncio.run(manage.set_role("boss", "user")) == 0
        assert _user(sessions, "boss").role == "user"
    finally:
        asyncio.run(engine.dispose())


def test_last_admin_cannot_be_deleted():
    """Последнего активного администратора нельзя удалить."""
    engine, sessions, _ = _setup([{"username": "admin", "role": "admin"}])
    try:
        assert asyncio.run(manage.delete_user("admin")) == 1
        assert _user(sessions, "admin") is not None
    finally:
        asyncio.run(engine.dispose())


def test_delete_user_moves_streams():
    """Потоки удаляемого аккаунта переходят другому администратору."""
    engine, sessions, ids = _setup(
        [{"username": "admin", "role": "admin"}, {"username": "user1"}],
        monitors=[2, 2],  # оба потока принадлежат user1 (id 2)
    )
    try:
        assert asyncio.run(manage.delete_user("user1")) == 0
        assert _user(sessions, "user1") is None
        owners = {m.owner_id for m in _monitors(sessions)}
        assert owners == {1}, owners
    finally:
        asyncio.run(engine.dispose())


def test_create_user_rejects_too_long_password():
    """Пароль длиннее 72 байт лучше отклонить, чем молча усечь."""
    engine, sessions, _ = _setup([{"username": "admin", "role": "admin"}])
    try:
        assert asyncio.run(manage.create_user("user2", "a" * 73, "user")) == 1
        assert _user(sessions, "user2") is None
        assert asyncio.run(manage.create_user("user2", "a" * 72, "user")) == 0
        assert _user(sessions, "user2") is not None
    finally:
        asyncio.run(engine.dispose())


_TESTS = [
    ("set-password отзывает токены", test_set_password_revokes_tokens),
    ("set-role отзывает токены", test_set_role_revokes_tokens),
    ("последнего админа нельзя понизить", test_last_admin_cannot_be_demoted),
    ("последнего админа нельзя удалить", test_last_admin_cannot_be_deleted),
    ("delete-user переносит потоки", test_delete_user_moves_streams),
    ("create-user проверяет длину пароля", test_create_user_rejects_too_long_password),
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
