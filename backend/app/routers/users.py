from typing import List, Optional
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, update
from pydantic import BaseModel
from datetime import datetime

from app.models.database import get_db
from app.models.models import Monitor, User
from app.routers.auth import get_current_user
from app.services.owner_service import resolve_default_owner_id
from app.core.security import get_password_hash, password_byte_error
from app.core.config import get_settings, set_admin_password, set_admin_username

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])


async def require_admin_role(current_user=Depends(get_current_user)):
    """Enforce admin role requirement."""
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ запрещен: требуется роль Администратора",
        )
    return current_user


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"


class UserUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("", response_model=List[UserResponse])
@router.get("/", response_model=List[UserResponse])
async def get_users(
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin_role),
):
    """List all registered user accounts (Admin only)."""
    result = await db.execute(select(User).order_by(desc(User.created_at)))
    return result.scalars().all()


@router.post("", response_model=UserResponse)
@router.post("/", response_model=UserResponse)
async def create_user(
    data: UserCreate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin_role),
):
    """Create a new user account (Admin only)."""
    if not data.username.strip() or not data.password.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Имя пользователя и пароль не могут быть пустыми",
        )

    pw_error = password_byte_error(data.password)
    if pw_error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=pw_error
        )

    if data.role not in ("admin", "user"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Роль должна быть 'admin' или 'user'",
        )

    # Check unique username
    existing = await db.execute(
        select(User).where(User.username == data.username.strip())
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Пользователь с таким именем уже существует",
        )

    user = User(
        username=data.username.strip(),
        password_hash=get_password_hash(data.password),
        role=data.role,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin_role),
):
    """Update user details, role, or reset password (Admin only)."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден"
        )

    # ``.env`` is the source of truth for one specific account, and all the checks
    # below must look at the account being edited *before* it is renamed:
    # renaming a user *to* the .env login must not make .env follow that account.
    previous_username = user.username
    is_env_admin = previous_username == get_settings().admin_username

    if data.username and data.username.strip() != user.username:
        existing = await db.execute(
            select(User).where(User.username == data.username.strip())
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Пользователь с таким именем уже существует",
            )
        user.username = data.username.strip()
        if is_env_admin:
            # Keep .env in sync: without it the next start would seed a *second*
            # admin under the old login and leave the renamed one an administrator.
            # (Tokens of database accounts die on a rename anyway, their ``sub`` no
            # longer matches a row.)
            try:
                set_admin_username(user.username)
            except Exception as e:
                logger.warning("Could not persist the admin username to .env: %s", e)

    if data.password and data.password.strip():
        pw_error = password_byte_error(data.password.strip())
        if pw_error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=pw_error
            )
        user.password_hash = get_password_hash(data.password.strip())
        # A new password must kill the sessions issued with the old one: the JWT
        # lifetimes (24 h) are otherwise wide open for a stolen token.
        user.token_version = (user.token_version or 1) + 1
        # Keep .env in sync when the .env admin's own password is changed here:
        # startup seeding treats .env as the source of truth for that account, so
        # without this the change would be reverted on the next restart.
        if is_env_admin:
            try:
                set_admin_password(data.password.strip())
            except Exception as e:
                logger.warning("Could not persist the admin password to .env: %s", e)
    if data.role and data.role not in ("admin", "user"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Роль должна быть 'admin' или 'user'",
        )

    # An admin must not lock themselves (or the last admin) out of the panel.
    demotes_or_blocks = (data.role is not None and data.role != "admin") or data.is_active is False
    if demotes_or_blocks and user.role == "admin":
        if user_id == admin.get("id"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Нельзя снять роль администратора или заблокировать собственный аккаунт",
            )
        other_admins = await db.execute(
            select(User).where(User.role == "admin", User.is_active.is_(True), User.id != user_id)
        )
        if not other_admins.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Нельзя снять роль последнего администратора",
            )

    if data.role:
        user.role = data.role

    if data.is_active is not None:
        user.is_active = data.is_active

    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin_role),
):
    """Delete a user account (Admin only)."""
    if admin.get("id") == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя удалить собственный аккаунт",
        )

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден"
        )

    # Reassign the streams owned by the deleted account to an admin so that no
    # stream is ever left without an owner.
    target_admin_id = await resolve_default_owner_id(db, prefer=admin.get("id"))
    if target_admin_id:
        await db.execute(update(Monitor).where(Monitor.owner_id == user_id).values(owner_id=target_admin_id))
    await db.delete(user)
    await db.commit()
    return {"message": "Пользователь удален"}