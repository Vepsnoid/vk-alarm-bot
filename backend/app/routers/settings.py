"""Settings router."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional
from pydantic import BaseModel, Field
from app.models.database import get_db
from app.models.models import Setting
from app.routers.auth import get_current_user
from app.core.config import (
    get_settings as get_app_settings,
    set_vk_service_token,
    set_max_bot_token,
    set_ai_api_key,
    set_ai_settings,
)
from app.core.redaction import is_masked_secret, mask_secret
from app.services.vk_service import VKService
from app.services.ai_service import AIService
from app.services.max_service import MaxService

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Known AI providers. Any OpenAI-compatible provider can be used; these are the
# presets offered in the UI together with their default API base and model.
AI_PROVIDER_DEFAULTS = {
    "gigachat": {"api_base": "", "model": "GigaChat"},
    "openai": {"api_base": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "deepseek": {"api_base": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "openrouter": {"api_base": "https://openrouter.ai/api/v1", "model": "openai/gpt-4o-mini"},
    "custom": {"api_base": "", "model": ""},
}


async def require_admin_role(user=Depends(get_current_user)):
    """Enforce admin role requirement for settings modification."""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ запрещен: изменение системных настроек доступно только Администраторам",
        )
    return user


class SettingsUpdate(BaseModel):
    vk_service_token: Optional[str] = Field(default=None, min_length=30, max_length=500)
    max_bot_token: Optional[str] = None
    ai_api_key: Optional[str] = None
    ai_provider: Optional[str] = None
    ai_model: Optional[str] = None
    ai_api_base: Optional[str] = None


class SettingsResponse(BaseModel):
    vk_service_token: Optional[str] = None
    vk_service_token_configured: bool
    token_status: bool
    vk_account_blocked: bool
    ai_configured: bool
    ai_status: bool
    max_bot_token: Optional[str] = None
    max_bot_token_configured: bool
    max_bot_token_valid: bool
    max_bot_identity: Optional[str] = None
    max_bot_note: Optional[str] = None
    ai_api_key: Optional[str] = None
    ai_provider: str = "gigachat"
    ai_model: str = "GigaChat"
    ai_api_base: str = ""
    ai_provider_options: List[str] = []
    ai_provider_presets: dict = {}


@router.get("", response_model=SettingsResponse)
@router.get("/", response_model=SettingsResponse)
async def read_settings(
    db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    config = get_app_settings()

    token_value = config.vk_service_token
    token_valid, account_blocked = (
        await VKService.get_token_status(token_value) if token_value else (False, False)
    )

    ai_configured = bool(config.ai_api_key or config.gigachat_credentials)
    ai_valid = await AIService.validate_ai_config() if ai_configured else False
    # MAX resolves the sending bot from the token alone, so we only show which bot
    # the configured token belongs to (handy when several bots exist).
    max_bot_info = await MaxService.get_me_info(config.max_bot_token) if config.max_bot_token else None

    # Secrets are never sent to the browser: the admin gets a placeholder
    # (``••••1234``) plus the ``*_configured`` flags, so a stolen session/XSS or a
    # proxied response cannot leak the VK/Max/AI credentials. An unchanged
    # placeholder submitted back by the form is ignored (see ``update_settings``).
    is_admin = user.get("role") == "admin"
    ai_key_value = config.ai_api_key or config.gigachat_credentials

    return SettingsResponse(
        vk_service_token=mask_secret(token_value) if is_admin else None,
        vk_service_token_configured=bool(token_value),
        token_status=token_valid,
        vk_account_blocked=account_blocked,
        ai_configured=ai_configured,
        ai_status=ai_valid,
        max_bot_token=mask_secret(config.max_bot_token) if is_admin else None,
        max_bot_token_configured=bool(config.max_bot_token),
        max_bot_token_valid=bool(max_bot_info),
        max_bot_identity=MaxService.describe_bot(max_bot_info),
        max_bot_note=(max_bot_info or {}).get("description") or None,
        ai_api_key=mask_secret(ai_key_value) if is_admin else None,
        ai_provider=(config.ai_provider or "gigachat").lower(),
        ai_model=config.ai_model or "",
        ai_api_base=config.ai_api_base or "",
        ai_provider_options=list(AI_PROVIDER_DEFAULTS.keys()),
        ai_provider_presets=AI_PROVIDER_DEFAULTS,
    )


@router.put("")
@router.put("/")
async def update_settings(
    settings_data: SettingsUpdate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin_role),
):
    updated_values = settings_data.model_dump(exclude_none=True)
    # The form is pre-filled with placeholders (``••••1234``) instead of the real
    # secrets; a placeholder that comes back unchanged must not overwrite the
    # stored value.
    for secret_field in ("vk_service_token", "max_bot_token", "ai_api_key"):
        if is_masked_secret(updated_values.get(secret_field)):
            updated_values.pop(secret_field, None)

    vk_service_token = updated_values.pop("vk_service_token", None)
    if vk_service_token is not None:
        token = vk_service_token.strip()
        if not VKService._is_valid_token(token):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Введите корректный сервисный ключ VK API",
            )
        set_vk_service_token(token)

    max_bot_token = updated_values.pop("max_bot_token", None)
    if max_bot_token is not None:
        token = max_bot_token.strip()
        if token:
            set_max_bot_token(token)

    ai_api_key = updated_values.pop("ai_api_key", None)
    ai_provider = updated_values.pop("ai_provider", None)
    ai_model = updated_values.pop("ai_model", None)
    ai_api_base = updated_values.pop("ai_api_base", None)

    if ai_provider is not None:
        ai_provider = ai_provider.strip().lower()
        if ai_provider not in AI_PROVIDER_DEFAULTS:
            ai_provider = "custom"

    if any(v is not None for v in (ai_provider, ai_model, ai_api_base, ai_api_key)):
        set_ai_settings(
            provider=ai_provider,
            model=ai_model.strip() if ai_model is not None else None,
            api_base=ai_api_base.strip() if ai_api_base is not None else None,
            api_key=ai_api_key.strip() if ai_api_key is not None else None,
        )

    for key, value in updated_values.items():
        existing = await db.execute(select(Setting).where(Setting.key == key))
        setting = existing.scalar_one_or_none()

        if setting:
            setting.value = str(value)
        else:
            setting = Setting(key=key, value=str(value))
            db.add(setting)

    await db.commit()

    return {"message": "Settings updated"}


class AIModelsRequest(BaseModel):
    ai_provider: Optional[str] = None
    ai_api_base: Optional[str] = None
    ai_api_key: Optional[str] = None


@router.post("/ai/models")
async def list_ai_models(payload: AIModelsRequest, admin=Depends(require_admin_role)):
    """Fetch available model IDs for the given/configured AI provider.

    Pulls the list live from the provider's API base URL (OpenAI-compatible
    ``/models``) or from GigaChat, so the UI can offer real model choices.
    """
    config = get_app_settings()
    provider = (payload.ai_provider or config.ai_provider or "gigachat").lower()
    api_base = payload.ai_api_base if payload.ai_api_base is not None else config.ai_api_base
    api_key = (payload.ai_api_key or "").strip() or config.ai_api_key or config.gigachat_credentials
    if is_masked_secret(api_key):
        # The UI echoes the placeholder back when it has not been edited.
        api_key = config.ai_api_key or config.gigachat_credentials
    ok, models, error = await AIService.list_models(provider, api_base, api_key)
    return {"ok": ok, "models": models, "error": error, "provider": provider}