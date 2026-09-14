"""AI rewrite service supporting OpenAI and Sber GigaChat."""

import uuid
import httpx
import logging
import json
import time
from typing import Optional, Tuple, Dict, Any, List
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# When a prompt asks the model to pick out relevant posts, a post that does not
# match must be droppable: the model answers with one of these markers and the
# post is not published (no tokens wasted on rewriting it, no noise in Max).
AI_SKIP_MARKERS = ("skip", "пропустить", "пропуск", "нерелевантно", "не относится", "не подходит", "не найдено", "-", "нет")


def _is_skip_answer(content: str) -> bool:
    """Whether the model asked to skip (drop) the post altogether."""
    if not content:
        return False
    cleaned = content.strip().strip(" \t\r\n.!?,;:*_`\"'«»").lower()
    if not cleaned:
        return False
    if cleaned in AI_SKIP_MARKERS:
        return True
    return cleaned.startswith("skip") or cleaned.startswith("пропуст") or cleaned.startswith("нерелевант")


class AIService:
    """Service for AI text rewriting supporting OpenAI and Sber GigaChat."""

    _gigachat_token: Optional[str] = None
    # The UI asks for the AI status on every settings page load; cache the verdict
    # so page views neither block nor burn provider quota.
    _ai_status_cache: Dict[tuple, tuple] = {}
    AI_STATUS_TTL = 60.0

    def __init__(self):
        self.reload_config()

    # Maximum number of characters of the post text passed to the AI model.
    DEFAULT_MAX_LENGTH = 6000

    def reload_config(self):
        config = get_settings()
        self.provider = (config.ai_provider or "gigachat").lower()
        self.api_key = config.ai_api_key or config.gigachat_credentials
        self.api_base = config.ai_api_base
        self.model = config.ai_model or config.gigachat_model or "GigaChat"
        self.ai_max_length = self.DEFAULT_MAX_LENGTH

        self.gigachat_credentials = self.api_key
        self.gigachat_scope = (
            config.ai_scope or config.gigachat_scope or "GIGACHAT_API_PERS"
        )
        self.gigachat_model = self.model

    async def init_settings(self, db):
        self.reload_config()

    @classmethod
    async def _get_gigachat_access_token(
        cls, credentials: str, scope: str
    ) -> Optional[str]:
        """Obtain GigaChat Bearer token using Authorization Key or Client ID:Client Secret."""
        import base64

        clean_cred = credentials.strip()
        if ":" in clean_cred and not clean_cred.startswith("Basic "):
            clean_cred = base64.b64encode(clean_cred.encode("utf-8")).decode("utf-8")

        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {clean_cred}",
        }
        data = {"scope": scope}
        try:
            async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
                res = await client.post(url, headers=headers, data=data)
                res.raise_for_status()
                token_data = res.json()
                return token_data.get("access_token")
        except Exception as exc:
            logger.error("Failed to get GigaChat access token: %s", exc)
            return None

    @classmethod
    async def list_models(
        cls, provider: str, api_base: str, api_key: str
    ) -> Tuple[bool, List[str], Optional[str]]:
        """Fetch the list of model IDs available for the given provider/key.

        Uses the standard OpenAI-compatible ``GET {base}/models`` endpoint, or the
        GigaChat ``GET /api/v1/models`` endpoint. Returns ``(ok, models, error)``.
        """
        provider = (provider or "gigachat").lower()
        api_key = (api_key or "").strip()
        if not api_key:
            return False, [], "Не задан API-ключ"

        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

        if provider == "gigachat":
            config = get_settings()
            token = await cls._get_gigachat_access_token(
                api_key,
                config.ai_scope or config.gigachat_scope or "GIGACHAT_API_PERS",
            )
            if not token:
                return False, [], "Не удалось авторизоваться в GigaChat"
            headers["Authorization"] = f"Bearer {token}"
            url = "https://gigachat.devices.sberbank.ru/api/v1/models"
            verify = False
        else:
            base = (api_base or "").strip().rstrip("/")
            if not base:
                return False, [], "Не задан API Base URL"
            url = f"{base}/models"
            verify = True

        try:
            async with httpx.AsyncClient(timeout=15.0, verify=verify) as client:
                res = await client.get(url, headers=headers)
                res.raise_for_status()
                data = res.json()
        except Exception as exc:
            logger.warning("Failed to list models for %s: %s", provider, exc)
            return False, [], str(exc)

        items: Any = None
        if isinstance(data, dict):
            items = data.get("data")
            if items is None:
                items = data.get("models")
        if isinstance(data, list):
            items = data
        if not isinstance(items, list):
            return False, [], "Неожиданный формат ответа"

        models: List[str] = []
        for item in items:
            if isinstance(item, dict):
                mid = item.get("id") or item.get("name") or item.get("model")
            else:
                mid = item
            if mid:
                models.append(str(mid))
        models = sorted(dict.fromkeys(models), key=str.lower)
        return True, models, None


    @classmethod
    async def validate_ai_config(cls) -> bool:
        # Cached for AI_STATUS_TTL seconds: the UI polls this on page loads.
        config = get_settings()
        cache_key = (
            config.ai_provider,
            config.ai_api_base,
            config.ai_model,
            bool(config.ai_api_key or config.gigachat_credentials),
        )
        cached = cls._ai_status_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < cls.AI_STATUS_TTL:
            return cached[1]
        result = await cls._fetch_ai_status()
        cls._ai_status_cache[cache_key] = (time.monotonic(), result)
        return result

    @classmethod
    async def _fetch_ai_status(cls) -> bool:
        """Check if AI API settings in .env are valid (GigaChat, OpenAI or Custom AI)."""
        config = get_settings()
        api_key = config.ai_api_key or config.gigachat_credentials
        if not api_key:
            return False

        provider = (config.ai_provider or "gigachat").lower()

        if provider == "gigachat":
            token = await cls._get_gigachat_access_token(
                api_key,
                config.ai_scope or config.gigachat_scope or "GIGACHAT_API_PERS",
            )
            return bool(token)

        # OpenAI / Custom API validation
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    f"{config.ai_api_base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": config.ai_model,
                        "messages": [{"role": "user", "content": "test"}],
                        "max_tokens": 1,
                    },
                )
                return res.status_code in (200, 201)
        except Exception as exc:
            logger.warning("AI validation failed: %s", exc)
            return False

    async def rewrite_text(
        self,
        text: str,
        prompt: str,
        tone: str,
        max_length: int,
        fallback_to_original: bool,
    ) -> Tuple[str, bool, Optional[str]]:
        """Rewrite text using configured AI provider (GigaChat or OpenAI)."""
        self.reload_config()

        instruction = (
            prompt
            or "Перепиши текст, сохранив смысл, но сделав его более интересным и уникальным."
        )
        system_prompt = (
            "Ты — редактор публикаций для мессенджера. Выполни инструкцию над текстом поста.\n"
            "Верни ТОЛЬКО итоговый текст публикации — без пояснений, без markdown-обёрток, "
            "без кавычек и без служебных фраз.\n\n"
            f"Инструкция:\n{instruction}"
        )
        if tone:
            system_prompt += f"\n\nТон текста: {tone}."

        if self.provider == "gigachat":
            return await self._rewrite_gigachat(
                text, system_prompt, max_length, fallback_to_original
            )
        else:
            return await self._rewrite_openai(
                text, system_prompt, max_length, fallback_to_original
            )

    async def _rewrite_gigachat(
        self,
        text: str,
        system_prompt: str,
        max_length: int,
        fallback_to_original: bool,
    ) -> Tuple[str, bool, Optional[str]]:
        if not self.gigachat_credentials:
            return text, False, "GIGACHAT_CREDENTIALS не указаны в .env"

        token = await self._get_gigachat_access_token(
            self.gigachat_credentials, self.gigachat_scope
        )
        if not token:
            msg = "Не удалось получить токен доступа GigaChat"
            return (text, False, msg) if fallback_to_original else (text, False, msg)

        url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.gigachat_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Текст поста:\n\n" + text[: self.ai_max_length]},
            ],
            "temperature": 0.7,
            "max_tokens": 4096,
        }

        try:
            async with httpx.AsyncClient(verify=False, timeout=60.0) as client:
                res = await client.post(url, headers=headers, json=payload)
                res.raise_for_status()
                data = res.json()
                choices = data.get("choices", [])
                content = (
                    choices[0].get("message", {}).get("content", "") if choices else ""
                )
                rewritten = content.strip() if isinstance(content, str) else ""

            if _is_skip_answer(rewritten):
                # The model decided the post is out of scope: publish nothing.
                return "", True, None
            if not rewritten:
                raise ValueError("GigaChat вернул пустой ответ")
            if len(rewritten) > max_length:
                raise ValueError(
                    f"Ответ GigaChat превышает лимит в {max_length} символов"
                )
            return rewritten, True, None
        except Exception as exc:
            if fallback_to_original:
                return text, False, str(exc)
            raise

    async def _rewrite_openai(
        self,
        text: str,
        system_prompt: str,
        max_length: int,
        fallback_to_original: bool,
    ) -> Tuple[str, bool, Optional[str]]:
        if not self.api_key:
            return text, False, "AI API key is not configured"

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.api_base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {
                                "role": "user",
                                "content": "Текст поста:\n\n" + text[: self.ai_max_length],
                            },
                        ],
                        "temperature": 0.7,
                        "max_tokens": 4096,
                    },
                )
                response.raise_for_status()
                data = response.json()
                choices = data.get("choices", [])
                content = (
                    choices[0].get("message", {}).get("content", "") if choices else ""
                )
                rewritten = content.strip() if isinstance(content, str) else ""

            if _is_skip_answer(rewritten):
                # The model decided the post is out of scope: publish nothing.
                return "", True, None
            if not rewritten:
                raise ValueError("AI returned an empty response")
            if len(rewritten) > max_length:
                raise ValueError(
                    f"AI response exceeds the {max_length} character limit"
                )
            return rewritten, True, None
        except Exception as exc:
            if fallback_to_original:
                return text, False, str(exc)
            raise

    async def analyze_post(
        self, text: str, prompt: str, post_info: Optional[Dict[str, Any]] = None, fallback_to_original: bool = True,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Analyze a post using AI to determine if it matches the prompt.
        Returns: (matched: bool, analysis_result: str | None, error: str | None)"""
        if not self.api_key:
            return False, None, "AI API key is not configured"
        system_prompt = (
            'Ты — система фильтрации постов. Ответь ТОЛЬКО в формате JSON:\n'
            '{"matched": true/false, "reason": "пояснение на русском"}\n'
            'matched=true — пост соответствует критерию, matched=false — нет.'
        )
        user_message = f"Критерий фильтрации:\n{prompt}\n\n"
        if post_info:
            user_message += f"Статистика: лайки={post_info.get('likes',0)}, репосты={post_info.get('reposts',0)}, ER={post_info.get('er',0)}%\n\n"
        user_message += f"Текст поста:\n\n{text[:self.ai_max_length]}"
        if self.provider == "gigachat":
            return await self._analyze_gigachat(system_prompt, user_message, fallback_to_original)
        return await self._analyze_openai(system_prompt, user_message, fallback_to_original)

    async def _analyze_gigachat(self, system_prompt: str, user_message: str, fallback: bool) -> Tuple[bool, Optional[str], Optional[str]]:
        token = await self._get_gigachat_access_token(self.gigachat_credentials, self.gigachat_scope)
        if not token:
            return False, None, "Failed to get GigaChat token"
        payload = {"model": self.gigachat_model, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}], "temperature": 0.3, "max_tokens": 500}
        try:
            async with httpx.AsyncClient(verify=False, timeout=60.0) as client:
                res = await client.post("https://gigachat.devices.sberbank.ru/api/v1/chat/completions", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json=payload)
                res.raise_for_status()
                choices = res.json().get("choices", [])
                content = choices[0].get("message", {}).get("content", "").strip() if choices else ""
            if not content:
                raise ValueError("Empty response")
            result = self._parse_ai_response(content)
            if result:
                return result["matched"], result["reason"], None
            return False, content, "Could not parse AI response"
        except Exception as exc:
            if fallback:
                return True, None, str(exc)
            return False, None, str(exc)

    async def _analyze_openai(self, system_prompt: str, user_message: str, fallback: bool) -> Tuple[bool, Optional[str], Optional[str]]:
        if not self.api_key:
            return False, None, "AI API key is not configured"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(f"{self.api_base}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, json={"model": self.model, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}], "temperature": 0.3, "max_tokens": 500})
                response.raise_for_status()
                choices = response.json().get("choices", [])
                content = choices[0].get("message", {}).get("content", "").strip() if choices else ""
            if not content:
                raise ValueError("Empty response")
            result = self._parse_ai_response(content)
            if result:
                return result["matched"], result["reason"], None
            return False, content, "Could not parse AI response"
        except Exception as exc:
            if fallback:
                return True, None, str(exc)
            return False, None, str(exc)

    @staticmethod
    def _parse_ai_response(text: str) -> Optional[Dict[str, Any]]:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text.replace("```json", "").replace("```", "").strip()
        try:
            result = json.loads(text)
            if isinstance(result, dict) and "matched" in result:
                return result
        except json.JSONDecodeError:
            pass
        tl = text.lower()
        if "true" in tl and "false" not in tl:
            return {"matched": True, "reason": text}
        if "false" in tl and "true" not in tl:
            return {"matched": False, "reason": text}
        if "да" in tl and "нет" not in tl:
            return {"matched": True, "reason": text}
        if "нет" in tl and "да" not in tl:
            return {"matched": False, "reason": text}
        return None
