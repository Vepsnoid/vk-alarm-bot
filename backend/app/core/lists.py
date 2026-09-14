"""Helpers for user-pasted multi-line lists (sources, channels, keywords).

Users paste ``durov\\npublic1`` (literal backslash-n), CRLF/CR text, or comma-,
semicolon- and tab-separated values. All of it must end up as one item per real
line: otherwise the whole list is treated as a single item — the UI shows
«Источники: 1» and the run tries to resolve the entire string as one source.
"""

import re
from typing import List, Optional

# List fields of a monitor and the extra separators accepted for each of them.
# Channels/IDs/links never contain a comma or semicolon, keywords might — so the
# extra separators are enabled only where they are unambiguous.
LIST_FIELDS = {
    "source_channels": ";,",
    "max_channels": ";,",
    "keywords": "",
    "minus_words": "",
}


def normalize_list_lines(value: Optional[str], extra_separators: str = "") -> Optional[str]:
    """Canonicalise a pasted list into one item per line (items stripped)."""
    if value is None:
        return None
    text = (
        value.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
    )
    separators = "[%s\t]+" % re.escape(extra_separators) if extra_separators else "[\t]+"
    pattern = re.compile(separators)
    items: List[str] = []
    for chunk in text.split("\n"):
        for part in pattern.split(chunk):
            item = part.strip()
            if item:
                items.append(item)
    return "\n".join(items)


def normalize_monitor_lists(payload: dict) -> dict:
    """Normalise every list field present in a monitor payload (in place)."""
    for key, separators in LIST_FIELDS.items():
        if isinstance(payload.get(key), str):
            payload[key] = normalize_list_lines(payload[key], separators)
    return payload
