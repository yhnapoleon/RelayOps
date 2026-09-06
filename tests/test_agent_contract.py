"""S5.5 / S5.7 — contract backward-compatibility (P8) + dependency discipline.

P8: the SSE sequence stays valid (meta first, done last, ≤1 answer) even when a
client ignores the v2-new events (write_proposal / form_sync / guide_step) — i.e.
the additions are purely additive.

Dependency discipline: the new agent modules introduce no new third-party Python
dependency, and the modules that must degrade gracefully (no langgraph/langchain
at import time) keep those imports guarded inside functions.
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "core" / "agent"

# Events introduced across S0–S4 (additive). A v1 client ignores these.
NEW_EVENTS = ("write_proposal", "form_sync", "guide_step", "artifact")


def _valid_sequence(events) -> bool:
    """The frozen contract: 'meta' first, 'done' last, at most one 'answer'."""
    kinds = [e["event"] for e in events]
    if not kinds or kinds[0] != "meta" or kinds[-1] != "done":
        return False
    if kinds.count("answer") > 1:
        return False
    return True


def _drop_new(events):
    return [e for e in events if e["event"] not in NEW_EVENTS]


# ── P8 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("events", [
    # a write turn
    [{"event": "meta", "data": {}},
     {"event": "write_proposal", "data": {}},
     {"event": "answer", "data": {"text": "ok"}},
     {"event": "done", "data": {}}],
    # a guide turn with options + a deeplink
    [{"event": "meta", "data": {}},
     {"event": "guide_step", "data": {}},
     {"event": "guide_step", "data": {}},
     {"event": "answer", "data": {"text": "pick one"}},
     {"event": "done", "data": {}}],
    # an onboarding turn (form_sync only, no answer-less edge)
    [{"event": "meta", "data": {}},
     {"event": "form_sync", "data": {}},
     {"event": "answer", "data": {"text": "opened"}},
     {"event": "done", "data": {}}],
    # a qa turn with an artifact
    [{"event": "meta", "data": {}},
     {"event": "tool_call", "data": {}},
     {"event": "artifact", "data": {}},
     {"event": "answer", "data": {"text": "chart"}},
     {"event": "done", "data": {}}],
])
def test_sequence_valid_with_and_without_new_events(events):
    # Valid as-is, and still valid for a client that drops every v2-new event.
    assert _valid_sequence(events)
    assert _valid_sequence(_drop_new(events))


def test_dropping_new_events_preserves_answer_and_bounds():
    events = [{"event": "meta", "data": {}},
              {"event": "guide_step", "data": {}},
              {"event": "answer", "data": {"text": "hi"}},
              {"event": "done", "data": {}}]
    stripped = _drop_new(events)
    assert [e["event"] for e in stripped] == ["meta", "answer", "done"]


# ── S5.7 zero new deps + guarded imports ─────────────────────────────────

_NEW_MODULES = (
    "assistant", "write", "write_tools", "write_schemas",
    "write_proposal_store", "sla_advisor", "ui_capability", "guide",
)

# Third-party roots already vendored in the project (no NEW dep may appear).
_ALLOWED_THIRD_PARTY = {
    "pydantic", "croniter", "sqlalchemy", "fastapi",
    "langgraph", "langchain", "langchain_core",
}

# Modules that must import cleanly without the LLM stack present — they may only
# touch langgraph/langchain inside functions (guarded), never at module top.
_MUST_DEGRADE = {"assistant", "write_schemas", "sla_advisor", "ui_capability"}
_HEAVY = {"langgraph", "langchain", "langchain_core"}


def _top_level_import_roots(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = set()
    for node in tree.body:  # module-body only → top-level imports
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("mod", _NEW_MODULES)
def test_no_new_third_party_dependency(mod):
    roots = _top_level_import_roots(SRC / f"{mod}.py")
    third_party = {r for r in roots if r not in ("core", "api", "__future__")
                   and not _is_stdlib(r)}
    unexpected = third_party - _ALLOWED_THIRD_PARTY
    assert not unexpected, f"{mod} introduces new dependency: {unexpected}"


@pytest.mark.parametrize("mod", sorted(_MUST_DEGRADE))
def test_degrade_modules_guard_heavy_imports(mod):
    roots = _top_level_import_roots(SRC / f"{mod}.py")
    leaked = roots & _HEAVY
    assert not leaked, f"{mod} imports {leaked} at module top (must be guarded inside functions)"


def _is_stdlib(name: str) -> bool:
    import sys
    if name in getattr(sys, "stdlib_module_names", set()):
        return True
    return name in {"typing", "re", "uuid", "hashlib", "json", "datetime", "math",
                    "dataclasses", "ast", "pathlib", "functools", "itertools"}
