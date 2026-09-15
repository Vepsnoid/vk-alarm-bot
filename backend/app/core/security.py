"""Security utilities."""

import bcrypt
import hashlib
import hmac
import time
from datetime import datetime, timedelta
from typing import Dict, Optional
from jose import JWTError, jwt
from app.core.config import get_settings

settings = get_settings()

# bcrypt never hashes more than 72 bytes of input. Newer ``bcrypt`` releases
# (5.x) raise a ValueError instead of truncating silently, so the limit is
# applied explicitly here and reported to the user when a NEW password exceeds
# it (see ``password_byte_error``).
BCRYPT_MAX_BYTES = 72


def _bcrypt_bytes(password: str) -> bytes:
    """Encode a password for bcrypt, truncated to bcrypt's 72-byte limit."""
    return (password or "").encode("utf-8")[:BCRYPT_MAX_BYTES]


def password_byte_error(password: str) -> Optional[str]:
    """Return a message when a new password would be silently truncated."""
    if len((password or "").encode("utf-8")) > BCRYPT_MAX_BYTES:
        return (
            f"Пароль слишком длинный: максимум {BCRYPT_MAX_BYTES} байт "
            "(символы вне ASCII занимают больше одного байта)"
        )
    return None


def is_password_hash(value: str) -> bool:
    """Whether a stored value is a bcrypt hash and not a legacy plaintext value."""
    return bool(value) and value[:4] in ("$2a$", "$2b$", "$2y$")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a stored value.

    A bcrypt hash is checked with bcrypt. Non-hash values are compared as plain
    text (constant-time): that path exists only for ``ADMIN_PASSWORD`` read from
    ``.env`` and for legacy rows, which ``hash_legacy_passwords()`` rewrites to
    bcrypt on startup.
    """
    if not hashed_password:
        return False
    if is_password_hash(hashed_password):
        try:
            return bcrypt.checkpw(_bcrypt_bytes(plain_password), hashed_password.encode("utf-8"))
        except Exception:
            return False
    return hmac.compare_digest(
        (plain_password or "").encode("utf-8"), hashed_password.encode("utf-8")
    )


def get_password_hash(password: str) -> str:
    """Hash a password with bcrypt (72-byte truncation as a safety net)."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(_bcrypt_bytes(password), salt).decode("utf-8")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=24)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.secret_key, algorithm="HS256")


def decode_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return payload
    except JWTError:
        return None


def token_version_ok(payload: dict, token_version: Optional[int]) -> bool:
    """Whether a JWT was issued after the last password change / logout.

    Changing the password (or calling ``/api/auth/logout``) increments
    ``User.token_version``; the claim below carries the value the token was
    issued with, so older tokens stop working immediately instead of staying
    valid for the remaining lifetime of the JWT.
    """
    try:
        issued = int(payload.get("tv") or 0)
    except (TypeError, ValueError):
        return False
    return issued == int(token_version or 1)


def is_env_fallback_token(payload: dict) -> bool:
    """Whether a JWT was minted by the ``.env``-admin recovery login.

    The recovery branch of ``get_current_user`` has no database row to compare
    ``token_version`` against (that is the whole point of the branch), so it only
    accepts tokens that the recovery login itself issued — those carry an explicit
    ``tv = 0``. A token without the claim (issued before token versions existed)
    or with any other version is refused there as well, otherwise removing or
    renaming the admin row would revive older admin tokens.
    """
    return payload.get("tv") == 0


def secret_fingerprint(value: str) -> str:
    """Short one-way fingerprint of a secret, safe to use as a cache key.

    Token caches used to keep the API token itself as the dictionary key; a
    fingerprint lets long-running processes cache results without holding the
    secret in memory (and without it leaking through a debug dump of the cache).
    """
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:32]


CACHE_MAX_ENTRIES = 8


def prune_ttl_cache(cache: Dict[str, tuple], ttl: float, max_entries: int = CACHE_MAX_ENTRIES) -> None:
    """Drop expired (and, when needed, the oldest) entries of a TTL cache.

    ``cache`` maps a fingerprint to ``(monotonic_timestamp, value)``. TTL alone
    only stops *using* stale entries — they still accumulated forever when tokens
    were rotated many times, so the cache is trimmed on every write.
    """
    if len(cache) <= max_entries:
        return
    now = time.monotonic()
    for key in [key for key, entry in cache.items() if not entry or now - entry[0] >= ttl]:
        cache.pop(key, None)
    while len(cache) > max_entries:
        oldest = min(cache, key=lambda key: cache[key][0])
        cache.pop(oldest, None)
