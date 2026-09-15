"""Numeric bounds shared by the API layer and the processing pipeline.

The frontend already limits these fields, but the API is reachable directly, so
the same bounds are enforced on the server (pydantic) and applied once more
inside the processor: rows saved by an older version can still hold out-of-range
values (e.g. a negative check interval, which would make the stream «due» on
every scheduler tick and hammer the VK API).
"""

from typing import Any, Optional

# Check frequency of a stream in minutes (same range as the UI: 1 min .. 24 h).
MIN_CHECK_INTERVAL_MINUTES = 1
MAX_CHECK_INTERVAL_MINUTES = 1440
DEFAULT_CHECK_INTERVAL_MINUTES = 15

# Limit for the AI answer/rewrite length in characters, as offered in the UI.
MIN_AI_MAX_LENGTH = 100
MAX_AI_MAX_LENGTH = 15000
DEFAULT_AI_MAX_LENGTH = 5000

# Engagement-rate window (percent), applied before the AI step.
MIN_ER_PERCENT = 0.0
MAX_ER_PERCENT = 100.0


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Clamp ``value`` into ``[minimum, maximum]`` (``default`` when unusable)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    """Clamp a float setting (legacy rows may hold ``None`` or garbage)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def er_range_error(min_er: Any, max_er: Any) -> Optional[str]:
    """Return a human-readable message when the ER window is inverted."""
    try:
        low, high = float(min_er), float(max_er)
    except (TypeError, ValueError):
        return None
    if low > high:
        return f"Минимальный ER ({low:g}%) не может быть больше максимального ({high:g}%)"
    return None
