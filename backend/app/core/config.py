"""Application configuration."""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import set_key
from pydantic import Field
from pydantic_settings import BaseSettings

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_PROJECT_ENV_FILE = _BACKEND_DIR.parent / ".env"
_ENV_FILE = _PROJECT_ENV_FILE if _PROJECT_ENV_FILE.exists() else _BACKEND_DIR / ".env"


class Settings(BaseSettings):
    """Application settings."""

    secret_key: str = ""
    database_url: str = "sqlite+aiosqlite:///./vk_alarm.db"
    admin_username: str = ""
    admin_password: str = ""
    vk_api_version: str = "5.199"
    vk_service_token: str = ""
    ai_provider: str = "gigachat"
    ai_api_key: str = ""
    ai_api_base: str = "https://api.openai.com/v1"
    ai_model: str = "GigaChat"
    ai_scope: str = "GIGACHAT_API_PERS"
    gigachat_credentials: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat"
    max_bot_token: str = ""

    class Config:
        env_file = str(_ENV_FILE)
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


def set_vk_service_token(token: str) -> None:
    """Persist and immediately apply the VK service token."""
    set_key(str(_ENV_FILE), "VK_SERVICE_TOKEN", token, quote_mode="never")
    os.environ["VK_SERVICE_TOKEN"] = token
    get_settings.cache_clear()


def set_max_bot_token(token: str) -> None:
    """Persist and immediately apply the Max bot token."""
    set_key(str(_ENV_FILE), "MAX_BOT_TOKEN", token, quote_mode="never")
    os.environ["MAX_BOT_TOKEN"] = token
    get_settings.cache_clear()


def set_ai_api_key(token: str) -> None:
    """Persist and immediately apply the AI API key/credentials."""
    set_key(str(_ENV_FILE), "AI_API_KEY", token, quote_mode="never")
    os.environ["AI_API_KEY"] = token
    get_settings.cache_clear()


def set_ai_settings(
    provider: str | None = None,
    model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
) -> None:
    """Persist and immediately apply AI provider/model/base/key settings.

    Supports any OpenAI-compatible endpoint (OpenAI, DeepSeek, OpenRouter, ...)
    as well as Sber GigaChat. Empty/``None`` values are ignored so partial
    updates do not wipe previously stored values.
    """
    values = {
        "AI_PROVIDER": provider,
        "AI_MODEL": model,
        "AI_API_BASE": api_base,
        "AI_API_KEY": api_key,
    }
    for env_key, value in values.items():
        if value is None:
            continue
        clean = value.strip()
        if clean == "" and env_key != "AI_API_KEY":
            continue
        set_key(str(_ENV_FILE), env_key, clean, quote_mode="never")
        os.environ[env_key] = clean
    get_settings.cache_clear()