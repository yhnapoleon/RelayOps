"""Shared parsing for list-endpoint query filters.

The UI's filter dropdowns are multi-select, so a filter arrives as a
comma-separated list (``?status=open,in_progress``). A bare single value is
still valid — it just parses to a one-element list — which keeps every
pre-existing caller, bookmark and integration working unchanged.
"""
from __future__ import annotations

from typing import List, Optional


def parse_filter_values(raw: Optional[str]) -> List[str]:
    """Split a comma-separated filter param into distinct, ordered values.

    Blank entries are dropped so ``"open,,resolved"`` and a trailing comma
    behave like the clean list. Returns ``[]`` for None/empty, meaning "no
    constraint" — callers should skip the filter entirely rather than
    querying for an empty ``IN ()``.
    """
    if not raw:
        return []
    seen: set[str] = set()
    values: List[str] = []
    for part in raw.split(","):
        value = part.strip()
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values
