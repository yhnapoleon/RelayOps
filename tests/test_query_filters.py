"""Multi-value query-filter parsing (api.query_filters).

The UI's filter dropdowns are multi-select and serialize as a comma-separated
param, so these cases pin the two things callers rely on: an empty result
means "no constraint" (never an empty ``IN ()``), and a plain single value
still works so older bookmarks / integrations don't break.
"""
from api.query_filters import parse_filter_values


def test_missing_or_blank_means_no_constraint():
    assert parse_filter_values(None) == []
    assert parse_filter_values("") == []
    assert parse_filter_values("   ") == []
    # A param of only separators must not degrade into [""], which would
    # filter for the empty string and silently return nothing.
    assert parse_filter_values(",") == []
    assert parse_filter_values(",,") == []


def test_single_value_still_parses():
    assert parse_filter_values("open") == ["open"]


def test_comma_separated_values_keep_order():
    assert parse_filter_values("open,in_progress,resolved") == [
        "open",
        "in_progress",
        "resolved",
    ]


def test_whitespace_and_blank_entries_are_tolerated():
    assert parse_filter_values(" open , resolved ") == ["open", "resolved"]
    assert parse_filter_values("open,,resolved,") == ["open", "resolved"]


def test_duplicates_collapse():
    assert parse_filter_values("open,open,resolved") == ["open", "resolved"]
