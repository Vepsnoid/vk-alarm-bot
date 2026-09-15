# -*- coding: utf-8 -*-
"""Регрессия: владелец потока должен существовать (PUT/POST /api/monitors).

Раньше PUT принимал любой ``owner_id``: прямым запросом можно было «повесить»
поток на несуществующего пользователя (SQLite не включает PRAGMA foreign_keys),
обычный пользователь такой поток не увидел бы, а backfill починил бы это только
при следующем старте. Теперь это 422 — и при обновлении, и при создании.

Запуск из каталога backend:  python tests/test_monitor_owner.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.core.security import get_password_hash  # noqa: E402
from app.models.database import Base  # noqa: E402
from app.models.models import Monitor, User  # noqa: E402
from app.routers.monitors import (  # noqa: E402
    MonitorCreate,
    MonitorUpdate,
    create_monitor,
    update_monitor,
)

ADMIN = {"id": 1, "role": "admin", "username": "admin"}
REGULAR = {"id": 2, "role": "user", "username": "user1"}


def _setup():
    """In-memory БД: администратор, обычный пользователь и поток пользователя."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def fill():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(User(username="admin", password_hash=get_password_hash("pw"), role="admin", is_active=True))
            db.add(User(username="user1", password_hash=get_password_hash("pw"), role="user", is_active=True))
            db.add(Monitor(name="Тест", source_channels="vk.com/a", max_channels="123",
                           is_active=False, owner_id=2))
            await db.commit()

    asyncio.run(fill())
    return engine, sessions


def test_update_rejects_unknown_owner():
    """PUT не принимает несуществующего владельца и не трогает поток."""
    engine, sessions = _setup()
    try:
        async def scenario():
            async with sessions() as db:
                codes = []
                try:
                    await update_monitor(1, MonitorUpdate(owner_id=999999), db=db, user=dict(ADMIN))
                except HTTPException as exc:
                    codes.append(exc.status_code)
                owners = [(await db.get(Monitor, 1)).owner_id]

                # Существующий владелец принимается как обычно.
                await update_monitor(1, MonitorUpdate(owner_id=2), db=db, user=dict(ADMIN))
                owners.append((await db.get(Monitor, 1)).owner_id)
                return codes, owners

        codes, owners = asyncio.run(scenario())
        assert codes == [422], codes
        assert owners == [2, 2], owners
    finally:
        asyncio.run(engine.dispose())


def test_update_ignores_owner_for_regular_user():
    """Обычный пользователь не может переназначить владельца (поле игнорируется)."""
    engine, sessions = _setup()
    try:
        async def scenario():
            async with sessions() as db:
                await update_monitor(1, MonitorUpdate(owner_id=999999), db=db, user=dict(REGULAR))
                return (await db.get(Monitor, 1)).owner_id

        assert asyncio.run(scenario()) == 2
    finally:
        asyncio.run(engine.dispose())


def test_create_rejects_unknown_owner():
    """POST с несуществующим владельцем — 422, поток не создаётся."""
    engine, sessions = _setup()
    try:
        async def scenario():
            async with sessions() as db:
                codes = []
                try:
                    await create_monitor(
                        MonitorCreate(name="Новый", source_channels="vk.com/b", max_channels="123", owner_id=999999),
                        db=db, user=dict(ADMIN),
                    )
                except HTTPException as exc:
                    codes.append(exc.status_code)
                created = (await db.execute(select(Monitor).where(Monitor.name == "Новый"))).scalars().all()

                monitor = await create_monitor(
                    MonitorCreate(name="Новый", source_channels="vk.com/b", max_channels="123", owner_id=2),
                    db=db, user=dict(ADMIN),
                )
                return codes, len(created), monitor.owner_id

        codes, created, owner = asyncio.run(scenario())
        assert codes == [422], codes
        assert created == 0, created
        assert owner == 2, owner
    finally:
        asyncio.run(engine.dispose())


_TESTS = [
    ("PUT отклоняет несуществующего владельца", test_update_rejects_unknown_owner),
    ("обычный пользователь не меняет владельца", test_update_ignores_owner_for_regular_user),
    ("POST отклоняет несуществующего владельца", test_create_rejects_unknown_owner),
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
