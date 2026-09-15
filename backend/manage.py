#!/usr/bin/env python3
"""Управление пользователями панели из консоли сервера.

Запускать из каталога ``backend`` (там лежит база):

    sudo -u www-data .venv/bin/python manage.py list-users
    sudo -u www-data .venv/bin/python manage.py create-user user1 User1-pass
    sudo -u www-data .venv/bin/python manage.py create-user boss Boss-pass admin
    sudo -u www-data .venv/bin/python manage.py set-password user1 new-pass
    sudo -u www-data .venv/bin/python manage.py set-role user1 admin
    sudo -u www-data .venv/bin/python manage.py delete-user user1

Скрипт сам переходит в свой каталог, поэтому запускать можно и по абсолютному пути.

Команды ведут себя так же, как веб-панель:
  * ``set-password``/``set-role`` отзывают ранее выданные JWT (``token_version``);
  * последнего активного администратора нельзя понизить или удалить;
  * ``delete-user`` переносит потоки удаляемого аккаунта другому администратору.
"""

import argparse
import asyncio
import os
import sys

# База SQLite задана относительным путём — работаем из каталога скрипта.
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

# На Windows-консоли кодировка может не поддерживать кириллицу — не падаем.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from sqlalchemy import func, select, update  # noqa: E402

from app.core.security import get_password_hash, password_byte_error  # noqa: E402
from app.models.database import AsyncSessionLocal  # noqa: E402
from app.models.models import Monitor, User  # noqa: E402
from app.services.owner_service import resolve_default_owner_id  # noqa: E402


def _find_user(db, username):
    return select(User).where(User.username == username)


async def _active_admins(db, exclude_id=None) -> int:
    """How many active administrators remain (optionally excluding ``exclude_id``)."""
    query = select(func.count()).select_from(User).where(
        User.role == "admin", User.is_active.is_(True)
    )
    if exclude_id is not None:
        query = query.where(User.id != exclude_id)
    return (await db.execute(query)).scalar_one()


async def list_users() -> int:
    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User).order_by(User.id))).scalars().all()
        if not users:
            print("Пользователей нет")
            return 0
        print(f"{'id':<4} {'логин':<20} {'роль':<8} активен")
        for u in users:
            print(f"{u.id:<4} {u.username:<20} {u.role:<8} {'да' if u.is_active else 'нет'}")
    return 0


async def create_user(username: str, password: str, role: str) -> int:
    error = password_byte_error(password)
    if error:
        # Как и в API: лучше отказать, чем молча усечь пароль до 72 байт.
        print(error)
        return 1
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(_find_user(db, username))).scalar_one_or_none()
        if existing:
            print(f"Пользователь '{username}' уже существует (id {existing.id})")
            return 1
        db.add(User(username=username, password_hash=get_password_hash(password), role=role, is_active=True))
        await db.commit()
    print(f"Создан пользователь '{username}' с ролью '{role}'")
    return 0


async def set_password(username: str, password: str) -> int:
    async with AsyncSessionLocal() as db:
        user = (await db.execute(_find_user(db, username))).scalar_one_or_none()
        if not user:
            print(f"Пользователь '{username}' не найден")
            return 1
        user.password_hash = get_password_hash(password)
        # Отзываем уже выданные токены: иначе украденный JWT работает ещё до 24 часов
        # (в веб-панели версия увеличивается, CLI должен вести себя так же).
        user.token_version = (user.token_version or 1) + 1
        await db.commit()
    print(f"Пароль пользователя '{username}' обновлён, выданные токены отозваны")
    return 0


async def set_role(username: str, role: str) -> int:
    async with AsyncSessionLocal() as db:
        user = (await db.execute(_find_user(db, username))).scalar_one_or_none()
        if not user:
            print(f"Пользователь '{username}' не найден")
            return 1
        if role != "admin" and user.role == "admin" and user.is_active:
            # Та же защита, что и в веб-панели: без администратора панель недоступна.
            if await _active_admins(db, exclude_id=user.id) == 0:
                print(f"Отказано: '{username}' — последний активный администратор. Сначала назначьте другого.")
                return 1
        changed = user.role != role
        user.role = role
        if changed:
            # Роль и так берётся из базы на каждом запросе, но токен несёт старую роль
            # в открытом виде: увеличиваем версию, чтобы клиент перелогинился.
            user.token_version = (user.token_version or 1) + 1
        await db.commit()
    print(f"Пользователь '{username}' теперь '{role}'" + (", выданные токены отозваны" if changed else ""))
    return 0


async def delete_user(username: str) -> int:
    async with AsyncSessionLocal() as db:
        user = (await db.execute(_find_user(db, username))).scalar_one_or_none()
        if not user:
            print(f"Пользователь '{username}' не найден")
            return 1
        if user.role == "admin" and user.is_active and await _active_admins(db, exclude_id=user.id) == 0:
            print(f"Отказано: '{username}' — последний активный администратор. Сначала назначьте другого.")
            return 1
        # Потоки удаляемого аккаунта нельзя оставить без владельца — та же логика,
        # что и в веб-панели (иначе потоки висят на несуществующем id до рестарта).
        target_id = await resolve_default_owner_id(db)
        reassigned = 0
        if target_id and target_id != user.id:
            result = await db.execute(
                update(Monitor).where(Monitor.owner_id == user.id).values(owner_id=target_id)
            )
            reassigned = result.rowcount or 0
        await db.delete(user)
        await db.commit()
    print(f"Пользователь '{username}' удалён" + (f", потоки перенесены на пользователя id {target_id} ({reassigned})" if reassigned else ""))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Управление пользователями VK Alarm Bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-users", help="показать всех пользователей")

    p = sub.add_parser("create-user", help="создать пользователя")
    p.add_argument("username")
    p.add_argument("password")
    p.add_argument("role", nargs="?", default="user", choices=["user", "admin"])

    p = sub.add_parser("set-password", help="сменить пароль")
    p.add_argument("username")
    p.add_argument("password")

    p = sub.add_parser("set-role", help="сменить роль")
    p.add_argument("username")
    p.add_argument("role", choices=["user", "admin"])

    p = sub.add_parser("delete-user", help="удалить пользователя")
    p.add_argument("username")

    args = parser.parse_args()
    handlers = {
        "list-users": lambda: list_users(),
        "create-user": lambda: create_user(args.username, args.password, args.role),
        "set-password": lambda: set_password(args.username, args.password),
        "set-role": lambda: set_role(args.username, args.role),
        "delete-user": lambda: delete_user(args.username),
    }
    return asyncio.run(handlers[args.command]())


if __name__ == "__main__":
    sys.exit(main())
