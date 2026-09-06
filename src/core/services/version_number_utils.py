"""Helpers for validating and comparing dotted product version strings."""

from __future__ import annotations

import re
from typing import Iterable

VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,2}$")


def normalize_version_number(value: str | None) -> str | None:
    """Normalize a version string while preserving semantic numeric order."""
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if not VERSION_RE.fullmatch(trimmed):
        raise ValueError("invalid_version_number")
    parts = [str(int(part)) for part in trimmed.split(".")]
    return ".".join(parts)


def version_key(value: str | None) -> tuple[int, ...]:
    """Build a sortable tuple for a semantic version string."""
    normalized = normalize_version_number(value)
    if normalized is None:
        return tuple()
    return tuple(int(part) for part in normalized.split("."))


def compare_version_numbers(left: str | None, right: str | None) -> int:
    """Compare two normalized version strings."""
    left_key = version_key(left)
    right_key = version_key(right)
    max_len = max(len(left_key), len(right_key))
    left_pad = left_key + (0,) * (max_len - len(left_key))
    right_pad = right_key + (0,) * (max_len - len(right_key))
    if left_pad < right_pad:
        return -1
    if left_pad > right_pad:
        return 1
    return 0


def max_version_number(values: Iterable[str | None]) -> str | None:
    """Return the highest version string from a sequence."""
    normalized_values = [normalize_version_number(value) for value in values]
    normalized_values = [value for value in normalized_values if value is not None]
    if not normalized_values:
        return None
    return max(normalized_values, key=version_key)


def next_version_number(current: str | None) -> str:
    """Bump the last segment of the current version, defaulting to v1."""
    normalized = normalize_version_number(current)
    if normalized is None:
        return "1"
    parts = [int(part) for part in normalized.split(".")]
    parts[-1] += 1
    return ".".join(str(part) for part in parts)
