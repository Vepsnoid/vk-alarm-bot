"""Authentication router."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import create_access_token, verify_password, decode_token
from app.models.database import get_db
from app.models.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])
security = HTTPBearer(auto_error=False)
settings = get_settings()


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserProfileResponse(BaseModel):
    id: int = 0
    username: str
    role: str
    is_active: bool
    theme_preference: str = "system"


class ThemeUpdateRequest(BaseModel):
    theme: str


@router.post("/login", response_model=TokenResponse)
async def login(
    credentials: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    # Check DB user accounts first
    result = await db.execute(
        select(User).where(User.username == credentials.username)
    )
    user = result.scalar_one_or_none()

    if user:
        if not user.is_active or not verify_password(
            credentials.password, user.password_hash
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Неверное имя пользователя или пароль (или аккаунт заблокирован)",
            )
        token = create_access_token(
            {"sub": user.username, "role": user.role, "user_id": user.id}
        )
        return {"access_token": token, "token_type": "bearer"}

    # Fallback to .env admin credentials for bootstrapping
    if credentials.username == settings.admin_username and verify_password(
        credentials.password, settings.admin_password
    ):
        token = create_access_token(
            {"sub": settings.admin_username, "role": "admin"}
        )
        return {"access_token": token, "token_type": "bearer"}

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Неверное имя пользователя или пароль",
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    payload = decode_token(credentials.credentials)
    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        )

    username = payload.get("sub")
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if user:
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Учетная запись заблокирована",
            )
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "is_active": user.is_active,
            "theme_preference": user.theme_preference or "system",
        }

    # Fallback if admin from .env
    if username == settings.admin_username:
        return {
            "id": 0,
            "username": settings.admin_username,
            "role": "admin",
            "is_active": True,
            "theme_preference": "system",
        }

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
    )


@router.get("/me", response_model=UserProfileResponse)
async def get_me(user=Depends(get_current_user)):
    return user


@router.put("/theme")
async def update_user_theme(
    data: ThemeUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    theme = data.theme if data.theme in ["light", "dark", "system"] else "system"
    user_id = current_user.get("id")
    if user_id:
        user = await db.get(User, user_id)
        if user:
            user.theme_preference = theme
            await db.commit()
    return {"theme_preference": theme}