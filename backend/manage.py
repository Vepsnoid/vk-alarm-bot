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

from sqlalchemy import select  # noqa: E402

from app.core.security import get_password_hash  # noqa: E402
from app.models.database import AsyncSessionLocal  # noqa: E402
from app.models.models import User  # noqa: E402


def _find_user(db, username):
    return select(User).where(User.username == username)


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
        await db.commit()
    print(f"Пароль пользователя '{username}' обновлён")
    return 0


async def set_role(username: str, role: str) -> int:
    async with AsyncSessionLocal() as db:
        user = (await db.execute(_find_user(db, username))).scalar_one_or_none()
        if not user:
            print(f"Пользователь '{username}' не найден")
            return 1
        user.role = role
        await db.commit()
    print(f"Пользователь '{username}' теперь '{role}'")
    return 0


async def delete_user(username: str) -> int:
    async with AsyncSessionLocal() as db:
        user = (await db.execute(_find_user(db, username))).scalar_one_or_none()
        if not user:
            print(f"Пользователь '{username}' не найден")
            return 1
        await db.delete(user)
        await db.commit()
    print(f"Пользователь '{username}' удалён")
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
