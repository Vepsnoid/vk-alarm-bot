"""VK API service."""

import asyncio
import httpx
import logging
import random
import re
import time
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.config import get_settings
from app.core.redaction import redact_sensitive_data
from app.core.security import prune_ttl_cache, secret_fingerprint

logger = logging.getLogger(__name__)

# Browser-like headers to avoid bot fingerprinting by VK
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


class VKAPIError(Exception):
    """Safe representation of an error returned by VK API."""

    def __init__(self, error_code: Optional[int], error_message: str):
        self.error_code = error_code
        self.error_message = redact_sensitive_data(error_message)
        super().__init__(self.__str__())

    def __str__(self) -> str:
        if self.error_code is None:
            return f"VK API Error: {self.error_message}"
        return f"VK API Error {self.error_code}: {self.error_message}"


class VKService:
    """Service for interacting with VK API."""

    BASE_URL = "https://api.vk.com/method"
    MAX_VIDEO_SIZE = 500 * 1024 * 1024
    # VK wall.get returns at most 100 items per request.
    MAX_PAGE_SIZE = 100
    # On the very first run only a small baseline is taken (no backlog flood).
    FIRST_RUN_MAX_POSTS = 30

    # Friendly hints for common VK API error codes (used in logs / notifications).
    VK_ERROR_HINTS = {
        5: "авторизация не удалась (проверьте токен)",
        6: "слишком много запросов в секунду",
        9: "flood control",
        10: "внутренняя ошибка VK",
        14: "требуется капча",
        15: "доступ к стене закрыт",
        18: "объект заблокирован",
        29: "достигнут лимит запросов",
        30: "приватная страница",
        100: "неверные параметры запроса",
        200: "доступ запрещён",
    }

    # --- Shared throttling / anti-blocking state (shared across all instances) ---
    _rate_lock: Optional[asyncio.Lock] = None
    _last_request_ts: float = 0.0
    _cooldown_until: float = 0.0
    # The UI polls the token status on every settings page load, so the verdict is
    # cached and the calls themselves go through the same throttle/cooldown budget.
    _token_status_cache: Dict[str, tuple] = {}
    TOKEN_STATUS_TTL = 60.0
    MIN_REQUEST_INTERVAL = 0.34  # ~3 requests/sec — VK's documented API limit
    MAX_COOLDOWN_SECONDS = 300.0

    @classmethod
    def _get_rate_lock(cls) -> asyncio.Lock:
        if cls._rate_lock is None:
            cls._rate_lock = asyncio.Lock()
        return cls._rate_lock

    @classmethod
    async def _throttle(cls) -> None:
        """Global rate limiter: spaces requests out and honours an active cooldown.

        Both guards are class-level, so even several VKService instances (one per
        monitor run) share the same 3 req/s budget and the same cool-down.
        """
        now = time.monotonic()
        if cls._cooldown_until > now:
            await asyncio.sleep(cls._cooldown_until - now)
        async with cls._get_rate_lock():
            now = time.monotonic()
            wait = cls.MIN_REQUEST_INTERVAL - (now - cls._last_request_ts)
            if wait > 0:
                # Random jitter makes the request rhythm look less machine-like.
                await asyncio.sleep(wait + random.uniform(0, 0.25))
            cls._last_request_ts = time.monotonic()

    @classmethod
    def _enter_cooldown(cls, seconds: float) -> None:
        """Pause all VK requests for a while (after flood control / captcha)."""
        cls._cooldown_until = max(
            cls._cooldown_until, time.monotonic() + min(seconds, cls.MAX_COOLDOWN_SECONDS)
        )

    @classmethod
    def cooldown_remaining(cls) -> float:
        """Seconds left before VK requests are allowed again (0.0 when not throttled).

        Used by the processor to explain long silent periods in the stream log.
        """
        return max(0.0, cls._cooldown_until - time.monotonic())

    @staticmethod
    def _is_valid_token(token: str) -> bool:
        """Simple validation for VK API token.
        VK tokens are non‑empty strings (may contain letters, digits, '_', '-', '.')
        and typically have a length of at least 30 characters.
        Format example: vk1.a.XXXXXXXX...
        """
        if not token or not isinstance(token, str):
            return False
        token = token.strip()
        return bool(re.fullmatch(r"[A-Za-z0-9_.\-]{30,}", token))

    async def validate_token(self) -> bool:
        """Check if the stored VK token is still valid by making a lightweight API call.
        Returns True if token is valid, False otherwise.
        """
        try:
            resp = await self._make_request("users.get", {})
            # If the call succeeds without error, the token is valid
            if isinstance(resp, list) and len(resp) > 0:
                return True
        except Exception:
            pass
        return False

    @classmethod
    async def get_token_status(cls, token: str) -> tuple[bool, bool]:
        # Cached: the UI asks for the token status on every settings page load, so
        # repeated checks must not spend VK API quota (and they are throttled too).
        if not cls._is_valid_token(token):
            return False, False
        cache_key = secret_fingerprint(token)
        cached = cls._token_status_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < cls.TOKEN_STATUS_TTL:
            return cached[1]
        status = await cls._fetch_token_status(token)
        cls._token_status_cache[cache_key] = (time.monotonic(), status)
        prune_ttl_cache(cls._token_status_cache, cls.TOKEN_STATUS_TTL)
        return status

    @classmethod
    async def _fetch_token_status(cls, token: str) -> tuple[bool, bool]:
        """Return whether a VK token works and whether its owner is blocked."""
        if not cls._is_valid_token(token):
            return False, False
        try:
            settings = get_settings()
            async with httpx.AsyncClient(timeout=10.0, headers=DEFAULT_HEADERS) as client:
                await cls._throttle()
                token_response = await client.get(
                    f"{cls.BASE_URL}/users.get",
                    params={"access_token": token, "v": settings.vk_api_version},
                )
                token_data = token_response.json()
                token_error = token_data.get("error", {})
                if token_error:
                    message = token_error.get("error_msg", "").lower()
                    return False, "user is blocked" in message
                if "response" not in token_data:
                    return False, False

                await cls._throttle()
                account_response = await client.get(
                    f"{cls.BASE_URL}/account.getProfileInfo",
                    params={"access_token": token, "v": settings.vk_api_version},
                )
                account_data = account_response.json()
                account_error = account_data.get("error", {})
                if account_error:
                    message = account_error.get("error_msg", "").lower()
                    return True, "user is blocked" in message
                return True, False
        except Exception:
            return False, False

    @classmethod
    async def validate_token_value(cls, token: str) -> bool:
        """Check whether a token is accepted by VK API."""
        token_valid, _ = await cls.get_token_status(token)
        return token_valid

    def __init__(self, db_session: Optional[AsyncSession] = None):
        settings = get_settings()
        self.token = settings.vk_service_token or ""
        self.api_version = settings.vk_api_version
        self.db_session = db_session
        self.client = httpx.AsyncClient(timeout=30.0, headers=DEFAULT_HEADERS)
        self.last_attachment_error: Optional[str] = None

    async def init_settings(self, db: AsyncSession):
        """Initialize settings dynamically from database."""
        # VK token comes from .env only (via config), no DB override
        pass

    async def _make_request(self, method: str, params: Dict[str, Any], *, use_post: bool = False) -> Dict:
        """Make VK API request with automatic rate-limit retries."""
        params.update({"access_token": self.token, "v": self.api_version})

        max_retries = 5
        for attempt in range(max_retries):
            # Global throttle keeps us under VK's 3 requests/sec limit and
            # respects any active cool-down (flood control / captcha).
            await self._throttle()

            try:
                if use_post:
                    response = await self.client.post(f"{self.BASE_URL}/{method}", data=params)
                else:
                    response = await self.client.get(f"{self.BASE_URL}/{method}", params=params)
            except Exception as exc:
                if attempt < max_retries - 1:
                    wait_time = 1.0 * (attempt + 1)
                    logger.warning(
                        "VK API network error on %s: %s. Retrying in %.1fs (attempt %d/%d)...",
                        method,
                        exc,
                        wait_time,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise Exception(f"VK API network error on {method}: {exc}") from exc

            # Retry on server errors (5xx)
            if response.status_code >= 500:
                if attempt < max_retries - 1:
                    wait_time = 1.0 * (attempt + 1)
                    logger.warning(
                        "VK API server error %d on %s. Retrying in %.1fs (attempt %d/%d)...",
                        response.status_code,
                        method,
                        wait_time,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise Exception(
                    f"VK API server error {response.status_code} on {method}"
                )

            # Handle empty response body
            if not response.content or not response.content.strip():
                if attempt < max_retries - 1:
                    wait_time = 1.0 * (attempt + 1)
                    logger.warning(
                        "VK API returned empty response for %s (HTTP %d). Retrying in %.1fs (attempt %d/%d)...",
                        method,
                        response.status_code,
                        wait_time,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise Exception(
                    f"VK API returned empty response for {method} (HTTP {response.status_code})"
                )

            # Parse JSON with error handling
            try:
                data = response.json()
            except Exception as exc:
                body_preview = response.text[:200] if response.text else "(empty)"
                if attempt < max_retries - 1:
                    wait_time = 1.0 * (attempt + 1)
                    logger.warning(
                        "VK API returned invalid JSON for %s (HTTP %d): %s. Body: %s. Retrying in %.1fs (attempt %d/%d)...",
                        method,
                        response.status_code,
                        exc,
                        body_preview,
                        wait_time,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise Exception(
                    f"VK API returned invalid JSON for {method} (HTTP {response.status_code}): {body_preview}"
                ) from exc

            if "error" in data:
                error_obj = data["error"]
                error_code = error_obj.get("error_code") if isinstance(error_obj, dict) else None
                error_message = (
                    error_obj.get("error_msg", "Unknown VK API error")
                    if isinstance(error_obj, dict)
                    else "Unknown VK API error"
                )

                # 14 = captcha required. Retrying immediately only makes the block
                # worse, so trigger a long cool-down and surface the error.
                if error_code == 14:
                    self._enter_cooldown(600.0)
                    logger.critical(
                        "VK API требует капчу (error_code 14) на %s. Запросы приостановлены. %s",
                        method,
                        error_message,
                    )
                    raise VKAPIError(error_code, error_message)

                # Transient rate limits: 6 (too many requests per second),
                # 9 (flood control), 29 (rate limit reached).
                if error_code in (6, 9, 29) and attempt < max_retries - 1:
                    base = {6: 1.0, 9: 5.0, 29: 10.0}.get(error_code, 1.0)
                    wait_time = base * (2 ** attempt) + random.uniform(0, 1.5)
                    if error_code in (9, 29):
                        # Flood control / rate limit: pause the whole service too.
                        self._enter_cooldown(wait_time)
                    logger.warning(
                        "VK API ограничение (error_code %s) на %s. Пауза %.1fs (попытка %d/%d)...",
                        error_code,
                        method,
                        wait_time,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(wait_time)
                    continue

                raise VKAPIError(error_code, error_message)

            return data.get("response", {})

    async def aclose(self) -> None:
        """Release network connections held by the VK client."""
        await self.client.aclose()

    async def resolve_screen_name(self, screen_name: str) -> Optional[int]:
        """Resolve screen name to numeric ID."""
        try:
            result = await self._make_request(
                "utils.resolveScreenName", {"screen_name": screen_name}
            )
            if result:
                return result.get("object_id")
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_post_owner(raw: str) -> Optional[int]:
        """Owner id of a link whose post id hides in the query (``?w=wall-1_2``)."""
        match = re.search(r"[?#][^\s]*?[wz]=wall(-?\d+)_\d+", raw or "", re.IGNORECASE)
        return int(match.group(1)) if match else None

    # Punctuation that often wraps a link copied from a post: "(vk.com/durov),".
    _LINK_TRIM = "()[]{}<>«»“”„‘’'\".,;:!?"

    @staticmethod
    def _normalize_source(source: str) -> str:
        """Strip the VK URL wrapper and the surrounding copy-paste punctuation.

        Accepts vk.com / vk.ru / vkontakte.ru with any subdomain (m., www., new.)
        and with or without the scheme; the query, anchor, slashes, a leading
        ``@`` and wrapping punctuation are removed
        (``m.vk.com/@durov`` -> ``durov``, ``(vk.com/durov),`` -> ``durov``).
        """
        value = (source or "").strip().strip(VKService._LINK_TRIM).strip()
        value = re.sub(
            r"^(?:https?://)?(?:[a-z0-9-]+\.)*(?:vk\.(?:com|ru)|vkontakte\.ru)/?",
            "",
            value,
            flags=re.IGNORECASE,
        )
        value = value.split("?")[0].split("#")[0]
        value = value.strip().strip("/").strip(VKService._LINK_TRIM).strip()
        return value.lstrip("@").strip()

    @classmethod
    def describe_unresolved_source(cls, source: str) -> str:
        """Explain why a source cannot be used (for logs and UI messages)."""
        value = cls._normalize_source(source)
        if not value:
            return "пустое значение — укажите ссылку или короткое имя"
        if "vk.cc/" in (source or "").lower() or value.lower().startswith("vk.cc"):
            return "короткие ссылки vk.cc не поддерживаются — вставьте полный адрес"
        if re.match(
            r"^(photo|video|audio|podcast|article|poll|topic|market|page|app|doc|clip|story|board|note)[-_]?\d",
            value,
            re.IGNORECASE,
        ):
            return "это ссылка на отдельный объект (фото/видео/статья…), а нужна страница сообщества или профиля"
        return "ссылка или имя не распознаны — проверьте адрес"

    async def resolve_owner_id(self, source: str) -> Optional[int]:
        """Resolve a VK source (domain, URL or id) to an owner_id.

        Groups get a negative id, users a positive one. Returns ``None`` when the
        source cannot be resolved.
        """
        if not source:
            return None
        # Links copied from the feed hide the post id in the query
        # (``?w=wall-1_2`` / ``#z=wall-1_2``): the post's owner is the source.
        post_owner = self._extract_post_owner(source)
        if post_owner is not None:
            return post_owner
        value = self._normalize_source(source)
        if not value:
            return None
        wall_match = re.search(r"wall(-?\d+)_\d+", value)
        if wall_match:
            value = wall_match.group(1)
        if re.fullmatch(r"-\d+", value):
            return int(value)
        if re.fullmatch(r"public\d+", value, re.IGNORECASE):
            return -int(value[6:])
        if re.fullmatch(r"club\d+", value, re.IGNORECASE):
            return -int(value[4:])
        if re.fullmatch(r"event\d+", value, re.IGNORECASE):
            return -int(value[5:])
        if re.fullmatch(r"id\d+", value, re.IGNORECASE):
            return int(value[2:])
        if value.isdigit():
            return int(value)
        try:
            result = await self._make_request(
                "utils.resolveScreenName", {"screen_name": value}
            )
            if result:
                object_id = result.get("object_id")
                if object_id is None:
                    return None
                if result.get("type") == "group":
                    return -int(object_id)
                return int(object_id)
        except Exception:
            pass
        return None


    async def get_group_id(self, domain: str) -> Optional[int]:
        """Get group ID from domain, screen name or full URL."""
        # Clean URL if full VK URL was passed
        domain = domain.strip()
        domain = re.sub(
            r"^https?://(www\.)?vk\.(com|ru)/", "", domain, flags=re.IGNORECASE
        )
        domain = domain.rstrip("/")

        # Handle numeric IDs directly (e.g. -12345 or public12345 or club12345)
        if domain.startswith("-") and domain[1:].isdigit():
            return int(domain)
        if domain.startswith("public") and domain[6:].isdigit():
            return -int(domain[6:])
        if domain.startswith("club") and domain[4:].isdigit():
            return -int(domain[4:])

        try:
            result = await self._make_request(
                "groups.getById", {"group_id": domain, "fields": "screen_name"}
            )
            if result and len(result) > 0:
                return -result[0]["id"]
        except Exception:
            pass

        resolved = await self.resolve_screen_name(domain)
        if resolved:
            return -resolved

        return None

    async def get_wall_posts(
        self,
        owner_id: int,
        count: int = 100,
        offset: int = 0,
        filter_type: str = "owner",
    ) -> List[Dict]:
        """Get wall posts from a group/user."""
        result = await self._make_request(
            "wall.get",
            {
                "owner_id": owner_id,
                "count": count,
                "offset": offset,
                "filter": filter_type,
                "extended": 1,
            },
        )
        items = result.get("items", [])
        # Filter out pinned post if it was retrieved as first item, or sort items by date descending
        return sorted(items, key=lambda x: x.get("date", 0), reverse=True)

    @classmethod
    def describe_error(cls, code: Optional[int], message: str = "") -> str:
        """Append a human-readable hint to a VK API error message."""
        hint = cls.VK_ERROR_HINTS.get(code)
        return f"VK error {code}: {message}" + (f" — {hint}" if hint else "")

    async def check_access(self, owner_id: int) -> Tuple[bool, Optional[str]]:
        """Lightweight pre-check that a wall is readable.

        Returns ``(ok, reason)``. Maps VK error codes (15/18/30/…) to a clear
        reason so closed/blocked/private sources are skipped with a good message.
        """
        try:
            await self._make_request("wall.get", {"owner_id": owner_id, "count": 1, "filter": "owner"})
            return True, None
        except VKAPIError as exc:
            return False, self.describe_error(exc.error_code, exc.error_message)
        except Exception as exc:  # network / unexpected
            return False, str(exc)

    async def get_posts_since_last(
        self,
        owner_id: int,
        last_id: Optional[int] = None,
        max_posts: int = 100,
        page_size: int = MAX_PAGE_SIZE,
        stats: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Return wall posts newer than ``last_id``.

        Paginates ``wall.get`` by ``offset`` (up to 100 items per page) so a burst
        of more than one page of new posts is not silently skipped. Pagination is
        stopped safely: an out-of-order/older or pinned post in the middle of the
        wall does not end collection prematurely — we stop only when an entire
        page is already known (all ids <= ``last_id``) or the pages run out.

        On the very first run (``last_id is None``) only the newest page is taken
        so the whole wall history is not fetched.

        ``stats`` (optional, filled in place) reports what the cap did:
        ``fetched`` — how many posts are returned, ``skipped`` — how many posts
        that were left out because of ``max_posts`` (a lower bound: one extra page
        is fetched to measure the surplus). The caller uses it to warn about a
        burst instead of dropping the surplus silently.
        """
        page_size = max(1, min(self.MAX_PAGE_SIZE, page_size))
        cap = max(1, max_posts)

        if last_id is None:
            # First run: establish a small baseline instead of flooding with the
            # channel's backlog (pagination is intentionally skipped here).
            limit = max(1, min(page_size, cap, self.FIRST_RUN_MAX_POSTS))
            items = await self.get_wall_posts(owner_id, count=limit, filter_type="owner")
            return [
                self._build_post_dict(item, owner_id)
                for item in items[:limit]
                if item.get("id") is not None
            ]

        last_id_int = int(last_id)
        collected: Dict[int, Dict[str, Any]] = {}
        offset = 0
        # One page beyond the cap is fetched on purpose: the surplus is what gets
        # left behind (the cursor moves past it), so it must be measurable.
        fetch_limit = cap + page_size
        while len(collected) < fetch_limit:
            items = await self.get_wall_posts(owner_id, count=page_size, offset=offset, filter_type="owner")
            if not items:
                break

            known_ids = [int(item["id"]) for item in items if item.get("id") is not None]
            # A whole page of already-seen posts means newer pages are exhausted.
            if known_ids and all(pid <= last_id_int for pid in known_ids):
                break

            for item in items:
                post_id = item.get("id")
                if post_id is None or int(post_id) <= last_id_int:
                    continue
                collected[int(post_id)] = self._build_post_dict(item, owner_id)

            offset += len(items)
            if len(items) < page_size:
                break

        posts = sorted(collected.values(), key=lambda p: int(p["id"]), reverse=True)
        kept = posts[:cap]
        if stats is not None:
            stats["fetched"] = len(kept)
            # ``skipped`` is a lower bound: pagination stopped at cap + page_size,
            # so anything beyond that is unknown.
            stats["skipped"] = len(posts) - len(kept)
        return kept

    def _build_post_dict(self, item: Dict[str, Any], owner_id: int) -> Dict[str, Any]:
        """Normalise a raw VK wall item into the internal post representation."""
        likes = (item.get("likes") or {}).get("count", 0)
        reposts = (item.get("reposts") or {}).get("count", 0)
        comments = (item.get("comments") or {}).get("count", 0)
        views = (item.get("views") or {}).get("count", 0)
        post_id = item.get("id")
        date_ts = item.get("date")
        return {
            "id": post_id,
            "owner_id": owner_id,
            "url": f"https://vk.com/wall{owner_id}_{post_id}",
            "text": item.get("text", "") or "",
            "date": datetime.utcfromtimestamp(date_ts) if date_ts else None,
            "attachments": item.get("attachments", []) or [],
            "likes": likes,
            "reposts": reposts,
            "comments": comments,
            "views": views,
            "er": self.calculate_er(likes, reposts, comments, views),
        }


    @staticmethod
    def _extract_wall_items(result) -> list:
        """Extract post items from wall.getById response.
        VK API v5.131+ returns {"items": [...]}, older returns [post1, post2].
        """
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("items", [])
        return []

    async def get_post_stats(self, owner_id: int, post_id: int) -> Dict[str, int]:
        """Get detailed post statistics."""
        try:
            result = await self._make_request(
                "wall.getById", {"posts": f"{owner_id}_{post_id}", "extended": 1}
            )
            items = self._extract_wall_items(result)
            if items:
                post = items[0]
                likes = post.get("likes", {}).get("count", 0)
                reposts = post.get("reposts", {}).get("count", 0)
                comments = post.get("comments", {}).get("count", 0)
                views = post.get("views", {}).get("count", 0)
                return {
                    "likes": likes,
                    "reposts": reposts,
                    "comments": comments,
                    "views": views,
                }
        except Exception:
            pass
        return {"likes": 0, "reposts": 0, "comments": 0, "views": 0}

    async def post_to_wall(
        self,
        owner_id: int,
        message: str,
        attachments: Optional[str] = None,
        from_group: bool = True,
    ) -> Dict:
        """Post to a wall."""
        # For VK wall.post: owner_id is negative for groups (e.g. -12345)
        params = {
            "owner_id": owner_id,
            "message": message,
            "from_group": 1 if from_group else 0,
        }
        if attachments:
            params["attachments"] = attachments

        return await self._make_request("wall.post", params, use_post=True)

    async def get_post_data(self, owner_id: int, post_id: int) -> Optional[Dict]:
        """Get full post data including attachments with URLs."""
        try:
            result = await self._make_request(
                "wall.getById", {"posts": f"{owner_id}_{post_id}"}
            )
            items = self._extract_wall_items(result)
            if items:
                return items[0]
        except Exception:
            pass
        return None

    async def download_file(
        self, url: str, max_size: Optional[int] = None
    ) -> Optional[bytes]:
        """Download file by URL."""
        try:
            response = await self.client.get(url, follow_redirects=True)
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if max_size and content_length and int(content_length) > max_size:
                logger.warning(
                    "Attachment from %s exceeds the %s byte limit", url, max_size
                )
                return None
            if max_size and len(response.content) > max_size:
                logger.warning(
                    "Attachment from %s exceeds the %s byte limit", url, max_size
                )
                return None
            return response.content
        except Exception as exc:
            self.last_attachment_error = str(exc)
            logger.warning("Could not download attachment from %s", url, exc_info=True)
            return None

    async def upload_wall_photo(
        self, group_id: int, photo_bytes: bytes
    ) -> Optional[str]:
        """Upload photo to VK group wall and return attachment string.
        Uses photos.getWallUploadServer -> upload -> photos.saveWallPhoto flow.
        """
        try:
            abs_group_id = abs(group_id)
            # 1. Get upload server
            result = await self._make_request(
                "photos.getWallUploadServer", {"group_id": abs_group_id}
            )
            upload_url = result.get("upload_url")
            if not upload_url:
                return None

            # 2. Upload photo bytes to server
            files = {"photo": ("photo.jpg", photo_bytes, "image/jpeg")}
            response = await self.client.post(upload_url, files=files, timeout=60.0)
            response.raise_for_status()
            data = response.json()

            photo = data.get("photo")
            server = data.get("server")
            hash_value = data.get("hash")

            if not photo or not server or not hash_value:
                return None

            # 3. Save wall photo
            saved = await self._make_request(
                "photos.saveWallPhoto",
                {
                    "group_id": abs_group_id,
                    "photo": photo,
                    "server": str(server),
                    "hash": hash_value,
                },
                use_post=True,
            )

            if saved and len(saved) > 0:
                photo_obj = saved[0]
                return f"photo{photo_obj['owner_id']}_{photo_obj['id']}"

            return None
        except Exception as exc:
            self.last_attachment_error = str(exc)
            logger.warning(
                "Could not upload photo to group %s", group_id, exc_info=True
            )
            return None

    async def upload_wall_video(
        self, group_id: int, video_bytes: bytes, title: str = ""
    ) -> Optional[str]:
        """Upload a video file to a VK group and return its attachment string."""
        try:
            abs_group_id = abs(group_id)
            result = await self._make_request(
                "video.save",
                {
                    "group_id": abs_group_id,
                    "name": title[:300] if title else "video",
                    "is_private": 0,
                    "wallpost": 0,
                },
            )
            owner_id = result.get("owner_id") if result else None
            video_id = result.get("video_id") if result else None
            upload_url = result.get("upload_url") if result else None
            if owner_id is None or video_id is None or not upload_url:
                logger.warning(
                    "VK did not return an upload URL for video in group %s", group_id
                )
                return None

            response = await self.client.post(
                upload_url,
                files={"video_file": ("video.mp4", video_bytes, "video/mp4")},
                timeout=300.0,
            )
            response.raise_for_status()
            if response.content:
                upload_result = response.json()
                if upload_result.get("error"):
                    logger.warning(
                        "VK video upload failed for group %s: %s",
                        group_id,
                        upload_result["error"],
                    )
                    return None

            return f"video{owner_id}_{video_id}"
        except Exception as exc:
            self.last_attachment_error = str(exc)
            logger.warning("Could not save video to group %s", group_id, exc_info=True)
            return None

    async def get_video_download_url(self, video: Dict[str, Any]) -> Optional[str]:
        """Return the largest direct MP4 URL exposed by VK for a video."""
        video_data = video
        files = video_data.get("files", {})

        if not files:
            owner_id = video.get("owner_id")
            video_id = video.get("id")
            if owner_id is None or video_id is None:
                return None
            video_ref = f"{owner_id}_{video_id}"
            if video.get("access_key"):
                video_ref += f"_{video['access_key']}"
            try:
                result = await self._make_request("video.get", {"videos": video_ref})
                items = result.get("items", []) if isinstance(result, dict) else []
                if items:
                    files = items[0].get("files", {})
            except Exception as exc:
                self.last_attachment_error = str(exc)
                logger.warning(
                    "Could not retrieve direct file URL for video %s",
                    video_ref,
                    exc_info=True,
                )
                return None

        mp4_files = [
            (key, value)
            for key, value in files.items()
            if key.startswith("mp4_") and isinstance(value, str) and value
        ]
        if not mp4_files:
            self.last_attachment_error = "VK API response has no direct MP4 URL"
            return None

        def quality(item: tuple[str, str]) -> int:
            try:
                return int(item[0].split("_", maxsplit=1)[1])
            except (IndexError, ValueError):
                return 0

        return max(mp4_files, key=quality)[1]

    async def reupload_attachments(
        self,
        original_owner_id: int,
        original_post_id: int,
        target_group_id: int,
        selected_indices: Optional[List[int]] = None,
        events: Optional[List[Dict[str, Any]]] = None,
        fallback_attachments: Optional[str] = None,
    ) -> Optional[str]:
        """Re-fetch original post and recreate supported attachments in target group.

        Only attachments owned by the target group are returned to ``wall.post``.
        Unsupported or failed attachments are skipped so they cannot prevent the
        text and other valid attachments from being published.
        If post data cannot be loaded, fallback_attachments is returned.
        """
        events = events if events is not None else []
        post_data = await self.get_post_data(original_owner_id, original_post_id)
        if not post_data:
            if fallback_attachments:
                events.append(
                    {"status": "uploaded", "reason": "Original post data not available, using stored attachments"}
                )
                return fallback_attachments
            events.append(
                {"status": "skipped", "reason": "Original post could not be loaded"}
            )
            return None

        new_attachments = []
        for index, att in enumerate(post_data.get("attachments", [])):
            att_type = att.get("type")
            if selected_indices is not None and index not in selected_indices:
                events.append(
                    {
                        "index": index,
                        "type": att_type,
                        "status": "skipped",
                        "reason": "Removed during moderation",
                    }
                )
                continue

            if att_type == "photo":
                self.last_attachment_error = None
                photo = att.get("photo", {})
                sizes = photo.get("sizes", [])
                if sizes:
                    # Pick largest size
                    largest = max(
                        sizes, key=lambda s: s.get("width", 0) * s.get("height", 0)
                    )
                    url = largest.get("url")
                    if url:
                        photo_bytes = await self.download_file(url)
                        if photo_bytes and len(photo_bytes) > 100:
                            new_att = await self.upload_wall_photo(
                                target_group_id, photo_bytes
                            )
                            if new_att:
                                new_attachments.append(new_att)
                                events.append(
                                    {
                                        "index": index,
                                        "type": "photo",
                                        "status": "uploaded",
                                        "attachment": new_att,
                                    }
                                )
                            else:
                                events.append(
                                    {
                                        "index": index,
                                        "type": "photo",
                                        "status": "skipped",
                                        "reason": self.last_attachment_error
                                        or "VK rejected the photo upload",
                                    }
                                )
                        else:
                            events.append(
                                {
                                    "index": index,
                                    "type": "photo",
                                    "status": "skipped",
                                    "reason": "Photo download failed or file is too small",
                                }
                            )
                    else:
                        events.append(
                            {
                                "index": index,
                                "type": "photo",
                                "status": "skipped",
                                "reason": "Photo has no downloadable size",
                            }
                        )

            elif att_type == "video":
                self.last_attachment_error = None
                video = att.get("video", {})
                owner_id = video.get("owner_id")
                video_id = video.get("id")
                access_key = video.get("access_key")
                if owner_id is None or video_id is None:
                    logger.warning("Skipping video without owner_id or id")
                    events.append(
                        {
                            "index": index,
                            "type": "video",
                            "status": "skipped",
                            "reason": "Video has no owner_id or id",
                        }
                    )
                    continue

                orig_video_att = f"video{owner_id}_{video_id}"
                if access_key:
                    orig_video_att += f"_{access_key}"

                video_url = await self.get_video_download_url(video)
                if video_url:
                    video_bytes = await self.download_file(video_url, self.MAX_VIDEO_SIZE)
                    if video_bytes:
                        new_att = await self.upload_wall_video(
                            target_group_id,
                            video_bytes,
                            video.get("title", ""),
                        )
                        if new_att:
                            new_attachments.append(new_att)
                            events.append(
                                {
                                    "index": index,
                                    "type": "video",
                                    "status": "uploaded",
                                    "attachment": new_att,
                                }
                            )
                            continue

                # Direct attachment fallback (works for all VK videos without requiring MP4 download/re-upload)
                new_attachments.append(orig_video_att)
                events.append(
                    {
                        "index": index,
                        "type": "video",
                        "status": "uploaded",
                        "attachment": orig_video_att,
                        "reason": "Attached original video directly",
                    }
                )

            elif att_type in ("doc", "audio"):
                logger.warning(
                    "Skipping %s attachment: re-uploading it to the target group is not supported",
                    att_type,
                )
                events.append(
                    {
                        "index": index,
                        "type": att_type,
                        "status": "skipped",
                        "reason": "Re-uploading this attachment type is not supported",
                    }
                )

            else:
                logger.info(
                    "Skipping unsupported attachment type: %s",
                    att_type,
                )
                events.append(
                    {
                        "index": index,
                        "type": att_type or "unknown",
                        "status": "skipped",
                        "reason": f"Unsupported attachment type: {att_type}",
                    }
                )

            # Small random pause between attachment re-uploads to avoid a burst of media operations
            await asyncio.sleep(random.uniform(3, 8))

        return ",".join(new_attachments) if new_attachments else None

    @staticmethod
    def calculate_er(likes: int, reposts: int, comments: int, views: int) -> float:
        """Engagement rate: (likes + comments * 5 + reposts * 10) / (views + 2000) * 100."""
        return round((likes + comments * 5 + reposts * 10) / (views + 2000) * 100, 2)

    @staticmethod
    def check_keyword_match(text: str, keyword: str) -> bool:
        """Check if keyword matches text with stemming support."""
        if not text or not keyword:
            return False

        text_lower = text.lower()
        keyword_lower = keyword.lower().strip()

        if keyword_lower in text_lower:
            return True

        words = re.findall(r"[а-яА-Яa-zA-Z]+", text_lower)

        for word in words:
            if word.startswith(keyword_lower) and len(word) > len(keyword_lower):
                next_char = (
                    word[len(keyword_lower)] if len(word) > len(keyword_lower) else ""
                )
                if next_char in "аеёиоуыэюяйьъ":
                    return True

        return False

    @staticmethod
    def match_minus_word(text: str, minus_words: List[str]) -> Optional[str]:
        """Return the first minus (stop) word found in the text, or None."""
        if not text or not minus_words:
            return None
        text_lower = text.lower()
        for word in minus_words:
            word = word.lower().strip()
            if word and word in text_lower:
                return word
        return None

    @staticmethod
    def check_minus_words(text: str, minus_words: List[str]) -> bool:
        """Check if text contains any minus words."""
        return VKService.match_minus_word(text, minus_words) is not None

    @staticmethod
    def check_filters(
        text: str,
        keywords: List[str],
        minus_words: List[str],
        min_er: float = 0.0,
        max_er: float = 100.0,
        likes: int = 0,
        reposts: int = 0,
        comments: int = 0,
        views: int = 0,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Apply the pre-AI filters: minus words, required keywords and ER range.

        Returns ``(passed, category, detail)``. ``category`` is ``"minus"``,
        ``"keywords"`` or ``"er"`` when the post is rejected (``None`` otherwise),
        and ``detail`` explains the decision (matched word / ER range) so the UI
        can show why a post was dropped.
        """
        cleaned = VKService.clean_text(text or "")
        if minus_words:
            matched = VKService.match_minus_word(cleaned, minus_words)
            if matched:
                return False, "minus", f"стоп-слово: {matched}"
        if keywords and not any(
            VKService.check_keyword_match(cleaned, kw) for kw in keywords
        ):
            return False, "keywords", "нет ни одного ключевого слова"
        er = VKService.calculate_er(likes, reposts, comments, views)
        upper = max_er if max_er is not None else 100.0
        if er < (min_er or 0.0) or er > upper:
            return False, "er", f"ER {er}% вне диапазона {min_er or 0.0}-{upper}%"
        return True, None, None


    @staticmethod
    def clean_text(text: str) -> str:
        """Clean source mentions, promotional phrases, hashtags, and links from text."""
        if not text:
            return ""

        # Remove URLs
        text = re.sub(r"https?://[^\s\]\)]+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"t\.me/[^\s\]\)]+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"vk\.(?:com|ru)/[^\s\]\)]+", "", text, flags=re.IGNORECASE)
        text = re.sub(
            r"(?:bit\.ly|tinyurl\.com|goo\.gl)/[^\s\]\)]+",
            "",
            text,
            flags=re.IGNORECASE,
        )

        # Remove markdown links
        text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\(\)", r"\1", text)

        # Remove subscription CTAs and mentions
        patterns_to_remove = [
            r"❗️?.*подпишись(?:тесь)? на (?:наш|наше) (?:телеграм-канал|канал|сообщество|проект).*",
            r"подпишись(?:тесь)? на .*",
            r"присоединяйтесь? к (?:нам|нашему) (?:каналу|сообществу|группе)",
            r"следите? за (?:нами|новостями|обновлениями)",
            r"не забудьте подписаться.*",
            r"подписывайтесь на (?:наш|наше) (?:канал|сообщество)",
            r"читайте (?:нас|наш канал).*",
            r"больше новостей в (?:нашем|канале).*",
            r"присылайте (?:нам|на) свои (?:фото|видео|материалы|новости)",
            r"источник:?\s*[^\n.!?]+",
            r"по информации (?:от|из) [^\n.!?]+",
            r"как сообщает [^\n.!?]+",
        ]
        for pattern in patterns_to_remove:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)

        # Remove hashtags
        text = re.sub(r"#\w+(?:@\w+)?", "", text)

        # Fix spacing and strip
        text = re.sub(r"\s+", " ", text).strip()
        return text
