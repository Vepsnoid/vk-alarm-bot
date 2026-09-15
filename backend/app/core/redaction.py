"""Helpers for keeping secrets out of logs and user-facing messages."""

import logging
import re
from typing import Any, Optional


_ACCESS_TOKEN_PATTERN = re.compile(
    r"((?:['\"]?access_token['\"]?\s*[:=]\s*['\"]?))[^'\"\s,}\]]+",
    re.IGNORECASE,
)
_BEARER_TOKEN_PATTERN = re.compile(r"(Bearer\s+)[^\s,}\]]+", re.IGNORECASE)

# Placeholder returned by the settings API instead of the stored secret.
MASK_PREFIX = "••••"


def mask_secret(value: Optional[str]) -> Optional[str]:
    """Return a display-only placeholder (``••••1234``) for a configured secret.

    The settings form is still usable (the admin sees *that* a secret is stored
    and can replace it), but the real value is never handed to the browser:
    localStorage tokens, XSS or a proxied response would otherwise leak the
    VK/Max/AI credentials.
    """
    if not value:
        return None
    tail = value[-4:] if len(value) >= 8 else ""
    return f"{MASK_PREFIX}{tail}"


def is_masked_secret(value: Optional[str]) -> bool:
    """Whether a value submitted by the UI is a mask and not a real secret."""
    return isinstance(value, str) and value.startswith(MASK_PREFIX)


def redact_sensitive_data(value: Any) -> Any:
    """Recursively replace access tokens in values intended for storage or display."""
    if isinstance(value, str):
        value = _ACCESS_TOKEN_PATTERN.sub(r"\1[REDACTED]", value)
        return _BEARER_TOKEN_PATTERN.sub(r"\1[REDACTED]", value)
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() == "access_token" else redact_sensitive_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    return value


class RedactingFilter(logging.Filter):
    """Last-resort redaction applied to every log record.

    ``httpx`` logs full request URLs (including ``access_token`` in the query
    string) at INFO level; ``main`` raises that level, but this filter makes the
    guarantee independent of library log levels: even a record produced by
    another library, or by a future ``logger.info(response.url)``, cannot print
    a token.

    Note: this is defence in depth, not an absolute guarantee — the patterns are
    the ones the application actually uses (``access_token=`` / ``Bearer``), so a
    brand-new secret format must be added to ``redact_sensitive_data`` explicitly.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_sensitive_data(record.msg)
            if isinstance(record.args, dict):
                record.args = redact_sensitive_data(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_sensitive_data(arg) for arg in record.args)
        except Exception:  # noqa: BLE001 - logging must never break the app
            pass
        return True


def install_log_redaction(*loggers: logging.Logger) -> RedactingFilter:
    """Attach a :class:`RedactingFilter` to the given loggers and their handlers.

    Handlers are patched too, because a filter attached to a logger only sees
    records created on that logger — records propagated from child loggers
    (``app.*``, ``httpx``, ``uvicorn``) are checked by the handlers instead.
    """
    flt = RedactingFilter()
    targets = loggers or (logging.getLogger(),)
    for target in targets:
        target.addFilter(flt)
        for handler in target.handlers:
            handler.addFilter(flt)
    return flt