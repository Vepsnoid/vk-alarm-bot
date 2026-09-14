"""Helpers for keeping secrets out of logs and user-facing messages."""

import re
from typing import Any


_ACCESS_TOKEN_PATTERN = re.compile(
    r"((?:['\"]?access_token['\"]?\s*[:=]\s*['\"]?))[^'\"\s,}\]]+",
    re.IGNORECASE,
)
_BEARER_TOKEN_PATTERN = re.compile(r"(Bearer\s+)[^\s,}\]]+", re.IGNORECASE)


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