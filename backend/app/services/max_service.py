"""Service for Max Bot API (https://dev.max.ru/docs-api).

Follows the documented MAX Bot API:
  * send message: ``POST /messages?chat_id=<id>`` with body ``{"text", "notify"}``;
  * token is passed only via the ``Authorization`` header (query tokens are gone);
  * idempotent GET requests are retried (network errors + 5xx); POST is never
    retried so a failed send cannot be duplicated.
"""

import asyncio
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from app.core.security import prune_ttl_cache, secret_fingerprint

logger = logging.getLogger(__name__)

BASE_URL = "https://platform-api2.max.ru"

# MAX message text limit (docs recommend staying under the hard 4000-char cap).
MAX_MESSAGE_LENGTH = 3950

# Retry policy — applies to idempotent GET requests only.
GET_RETRIES = 2
GET_BACKOFF = 1.0

# Media limits.
MAX_ATTACHMENTS = 12
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# Extensions mapped to MAX attachment types.
_EXT_TO_TYPE = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image",
    ".mp4": "video", ".mov": "video", ".avi": "video", ".mkv": "video", ".webm": "video",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio",
    ".pdf": "file", ".doc": "file", ".docx": "file", ".xls": "file",
    ".xlsx": "file", ".zip": "file", ".rar": "file",
}


def _guess_attachment_type(filename: str) -> str:
    """Guess a MAX attachment type from a filename extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXT_TO_TYPE.get(f".{ext}", "file")


class MaxBotError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        self.status_code = status_code
        super().__init__(message)


class MaxService:
    # MAX chat ids are numeric and may be negative for groups/channels
    # (e.g. -78905088689474), so sign is allowed.
    _CHAT_ID_RE = re.compile(r"^-?\d+$")

    # ``validate_token`` is polled by the UI on every settings page load; cache the
    # verdict so page views do not call the MAX API each time.
    _token_cache: Dict[str, tuple] = {}
    TOKEN_TTL = 60.0
    # ``GET /me`` (bot identity) is cached with the same TTL.
    _me_cache: Dict[str, tuple] = {}
    ME_TTL = 60.0

    def __init__(self, token: str = ""):
        self.token = token
        self.client = httpx.AsyncClient(timeout=30.0, verify=False, headers={"Authorization": token, "Content-Type": "application/json", "Accept": "application/json"})
        # Separate client for file uploads (Content-Type is set per request).
        self.upload_client = httpx.AsyncClient(timeout=120.0, verify=False, headers={"Authorization": token, "Accept": "application/json"})
        # Reason of the last ``media_attachment_from_url`` failure (mirrors
        # ``VKService.last_attachment_error``): the caller needs to know whether a
        # missing attachment is worth retrying or is permanently unusable.
        self.last_attachment_error: Optional[str] = None
        self.last_attachment_transient: bool = False

    async def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None, retries: Optional[int] = None) -> Any:
        """Perform a MAX API request.

        Only idempotent GET requests are retried (network errors and HTTP 5xx).
        POST requests are never retried to avoid duplicating a sent message.
        """
        url = f"{BASE_URL}/{path.lstrip('/')}"
        is_get = method.upper() == "GET"
        attempts = (GET_RETRIES if is_get else 0) if retries is None else retries
        last_exc: Optional[Exception] = None
        for attempt in range(attempts + 1):
            try:
                if is_get:
                    resp = await self.client.get(url, params=params)
                else:
                    resp = await self.client.post(url, params=params, json=body)
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < attempts:
                    wait = GET_BACKOFF * (2 ** attempt)
                    logger.warning("Max network error on %s %s (attempt %d/%d): %s. Retry in %.1fs", method, path, attempt + 1, attempts + 1, exc, wait)
                    await asyncio.sleep(wait)
                    continue
                raise MaxBotError(f"Сетевая ошибка {method} {path}: {exc}") from exc

            if resp.status_code >= 500 and attempt < attempts:
                wait = GET_BACKOFF * (2 ** attempt)
                logger.warning("Max %s %s returned HTTP %d (attempt %d/%d). Retry in %.1fs", method, path, resp.status_code, attempt + 1, attempts + 1, wait)
                await asyncio.sleep(wait)
                continue
            if resp.is_success:
                try:
                    return resp.json()
                except ValueError:
                    return {}
            raise MaxBotError(f"HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)
        raise MaxBotError(f"Не удалось выполнить {method} {path}") from last_exc

    async def check_membership(self, chat_id: int) -> Optional[bool]:
        try:
            await self._request("GET", f"chats/{chat_id}/members/me")
            return True
        except MaxBotError as exc:
            if exc.status_code in (403, 404):
                return False
            logger.warning("Max membership check failed for chat %s: %s", chat_id, exc)
            return None
        except Exception as exc:
            logger.warning("Max membership check failed for chat %s: %s", chat_id, exc)
            return None

    async def send_message(self, chat_id: str, text: str, attachments: Optional[List[Dict[str, Any]]] = None, notify: bool = True) -> bool:
        """Send a message: ``POST /messages?chat_id=<id>`` (documented MAX API).

        The chat id goes into the query string and the payload carries ``text``,
        ``notify`` and (optionally) ready-to-use ``attachments`` objects.
        """
        payload: Dict[str, Any] = {"text": text, "notify": notify}
        if attachments:
            payload["attachments"] = attachments[:MAX_ATTACHMENTS]
        await self._request("POST", "messages", params={"chat_id": int(chat_id)}, body=payload)
        return True

    async def send_post_to_chat(self, chat_id: str, text: str, original_url: Optional[str] = None, attachments: Optional[List[Dict[str, Any]]] = None) -> bool:
        max_text = text or ""
        if original_url:
            max_text += f"\n\nИсточник: {original_url}"
        return await self.send_message(chat_id, max_text[:MAX_MESSAGE_LENGTH], attachments=attachments)

    async def upload_file(self, file_bytes: bytes, filename: str) -> Optional[str]:
        """Upload a file to MAX (``POST /uploads?type=...``) and return its token."""
        file_type = _guess_attachment_type(filename)
        for attempt in range(GET_RETRIES + 2):
            try:
                resp = await self.upload_client.post(
                    f"{BASE_URL}/uploads",
                    params={"type": file_type},
                    files={"file": (filename, file_bytes)},
                )
                if resp.is_success:
                    data = resp.json()
                    return data.get("token") or data.get("id")
                if resp.status_code == 413:
                    raise MaxBotError(f"Файл {filename} слишком большой для Max", 413)
                if resp.status_code == 429:
                    await asyncio.sleep(2.0 * (attempt + 1) + random.uniform(0, 1))
                    continue
                logger.warning("Max upload error %s: %s", resp.status_code, resp.text[:200])
            except httpx.RequestError as exc:
                logger.warning("Max upload request error: %s", exc)
            await asyncio.sleep(2.0 * (attempt + 1))
        return None

    async def media_attachment_from_url(self, url: str, max_size: int = MAX_FILE_SIZE) -> Optional[Dict[str, Any]]:
        """Download a file by URL, upload it to MAX and return an attachment dict.

        Returns ``{"type": "image", "payload": {"token": "..."}}`` or ``None``.
        The reason of a failure is kept in ``last_attachment_error`` together with
        ``last_attachment_transient``: a network hiccup or a 5xx is worth retrying,
        while an oversized/deleted file would fail identically every time.
        """
        self.last_attachment_error = None
        self.last_attachment_transient = False
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as download_client:
                resp = await download_client.get(url)
                resp.raise_for_status()
                file_bytes = resp.content
                if len(file_bytes) > max_size:
                    self.last_attachment_error = f"файл больше {max_size // (1024 * 1024)} МБ"
                    logger.warning("Media size unsuitable for Max (%d bytes): %s", len(file_bytes), url)
                    return None
                if len(file_bytes) < 100:
                    self.last_attachment_error = "файл пустой или повреждён"
                    logger.warning("Media looks broken for Max (%d bytes): %s", len(file_bytes), url)
                    return None
                content_disp = resp.headers.get("content-disposition", "")
                match = re.search(r'filename="?([^"]+)"?', content_disp)
                if match:
                    filename = match.group(1)
                else:
                    url_path = url.split("?")[0]
                    filename = url_path.rsplit("/", 1)[-1] or "file"
            token = await self.upload_file(file_bytes, filename)
            if not token:
                # ``upload_file`` already retried network/5xx/429 internally, so the
                # caller should try the whole post again later.
                self.last_attachment_error = "загрузка файла в Max не удалась"
                self.last_attachment_transient = True
                logger.warning("Media upload failed for %s", url)
                return None
            return {"type": _guess_attachment_type(filename), "payload": {"token": token}}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            # 404/403 — фото удалено/закрыто, повтор не поможет; 5xx/429 — временно.
            self.last_attachment_error = f"HTTP {status} при скачивании"
            self.last_attachment_transient = status >= 500 or status == 429
            logger.warning("Failed to download media for Max from %s: HTTP %s", url, status)
            return None
        except httpx.RequestError as exc:
            self.last_attachment_error = f"сеть при скачивании: {exc}"
            self.last_attachment_transient = True
            logger.warning("Failed to download media for Max from %s: %s", url, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            self.last_attachment_error = str(exc)
            # Неизвестная ошибка — попробуем ещё раз, хуже не будет.
            self.last_attachment_transient = True
            logger.warning("Failed to prepare Max media from %s: %s", url, exc)
            return None

    @staticmethod
    def _extract_chat_candidates(data: Any) -> List[Dict[str, Any]]:
        """Tolerantly collect chat-like objects from an arbitrary MAX response."""
        found: List[Dict[str, Any]] = []
        stack: List[Any] = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("chat_id") is not None or node.get("id") is not None:
                    found.append(node)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return found

    async def resolve_chat_id(self, channel_name: str) -> Optional[str]:
        """Resolve a channel name/title to a chat_id via the subscriptions list.

        ``GET /chats`` is deprecated by MAX (since June 2026), so the documented
        ``GET /subscriptions`` listing is used instead.

        An exact match (username, link tail or title) wins over a substring match:
        with subscriptions «Новости Карелии» and «Новости Карелии | Официально» the
        old ``name in title`` rule picked whichever the API returned first. A
        substring that matches several chats is refused (``None``) instead of
        guessing — the caller then logs that the target could not be recognised.
        """
        name = (channel_name or "").lower().strip()
        if not name:
            return None
        try:
            data = await self._request("GET", "subscriptions")
        except Exception as exc:
            logger.warning("Failed to list Max subscriptions: %s", exc)
            return None
        partial: List[str] = []
        for chat in self._extract_chat_candidates(data):
            raw_id = chat.get("chat_id") or chat.get("id")
            if raw_id is None:
                continue
            cid = str(raw_id)
            username = (chat.get("username") or "").lower().strip()
            link = (chat.get("link") or "").lower().strip()
            link_tail = link.rstrip("/").split("/")[-1] if link else ""
            title = (chat.get("title") or "").lower().strip()
            if name == username or name == link_tail or name == title:
                return cid
            if title and name in title:
                partial.append(cid)
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            logger.warning(
                "Max: название '%s' неоднозначно — подходит чатов: %d. Укажите chat_id или ссылку.",
                channel_name,
                len(partial),
            )
        return None

    async def parse_chat_id(self, channel: str) -> Optional[str]:
        """Resolve a configured target to a numeric chat_id.

        Supports numeric ids and links whose last path segment is numeric
        (``https://max.ru/chat/123``). Names/titles are resolved best-effort via
        the subscriptions list; when that fails ``None`` is returned so the
        caller logs a clear warning instead of sending to a wrong target.
        """
        channel = (channel or "").strip()
        if not channel:
            return None
        if self._CHAT_ID_RE.match(channel):
            return channel
        if "max.ru" in channel or "//" in channel:
            last = channel.rstrip("/").split("/")[-1].split("?")[0]
            if self._CHAT_ID_RE.match(last):
                return last
            channel = last
        return await self.resolve_chat_id(channel)

    @classmethod
    async def get_me_info(cls, token: str) -> Optional[Dict[str, Any]]:
        """Return the bot the token belongs to (``GET /me``), cached for ME_TTL.

        MAX resolves the sender from the token alone, so this is only used to show
        *which* bot the configured token belongs to (useful when several bots
        exist and only one of them is a member of the target channel).
        """
        if not token or not token.strip():
            return None
        token = token.strip()
        cache_key = secret_fingerprint(token)
        cached = cls._me_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < cls.ME_TTL:
            return cached[1]
        info: Optional[Dict[str, Any]] = None
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False, headers={"Authorization": token, "Accept": "application/json"}) as client:
                resp = await client.get(f"{BASE_URL}/me")
                if resp.is_success:
                    info = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Max GET /me failed: %s", exc)
        cls._me_cache[cache_key] = (time.monotonic(), info)
        prune_ttl_cache(cls._me_cache, cls.ME_TTL)
        return info

    @staticmethod
    def describe_bot(info: Optional[Dict[str, Any]]) -> Optional[str]:
        """Human-readable identity of the bot a token belongs to."""
        if not info:
            return None
        parts = []
        name = info.get("name") or info.get("first_name")
        if name:
            parts.append(str(name))
        if info.get("username"):
            parts.append("@" + str(info["username"]))
        bot_id = info.get("user_id") or info.get("id")
        if bot_id:
            parts.append("id " + str(bot_id))
        return " · ".join(parts) or None

    @classmethod
    async def validate_token(cls, token: str) -> bool:
        # Cached for TOKEN_TTL seconds: the UI polls this on every page load.
        if not token or not token.strip():
            return False
        token = token.strip()
        cache_key = secret_fingerprint(token)
        cached = cls._token_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < cls.TOKEN_TTL:
            return cached[1]
        result = await cls._validate_token_live(token)
        cls._token_cache[cache_key] = (time.monotonic(), result)
        prune_ttl_cache(cls._token_cache, cls.TOKEN_TTL)
        return result

    @classmethod
    async def _validate_token_live(cls, token: str) -> bool:
        if not token or not token.strip():
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False, headers={"Authorization": token.strip(), "Accept": "application/json"}) as client:
                resp = await client.get(f"{BASE_URL}/me")
                return resp.is_success
        except Exception:
            return False

    async def aclose(self):
        await self.client.aclose()
        await self.upload_client.aclose()