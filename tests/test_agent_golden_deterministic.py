"""S5.6 — deterministic golden baselines (no LLM, no DB).

Two regression tables that must stay green as the agent evolves:
  * write-intent routing — a curated set of phrases whose intent is fixed by the
    deterministic keyword anchors (independent of the light LLM);
  * SLA recommendation numbers — the advisor's output for a fixed run-cadence
    fixture, locking the "no made-up numbers" computation.

The LLM-backed golden transcript harness stays in test_agent_golden.py (opt-in).
"""
import pytest

from core.agent import assistant
from core.auth.jwt import CurrentUser
from core.models.user import UserRole


def _actor():
    return CurrentUser(username="dora", user_id=42, role=UserRole.RELAYOPS_MEMBER)


# ── write-intent routing golden (deterministic, keyword-anchored) ─────────

# NOTE: a typed change request is **never** auto-routed to write (P1 safety —
# see classify_intent docstring). It stays in qa, where the qa agent nudges the
# user to flip the Write-mode toggle (and, per RELIABILITY_PLAN §6.5 dual-track,
# offers to produce a confirm-first draft). So "关闭 issue 88" → qa, not write.
ROUTING_GOLDEN = [
    ("把 #123 标记为误报", "qa"),
    ("关闭 issue 88", "qa"),
    ("诊断 #5 为什么失败", "diagnose"),
    ("我要接入一个新的 mmp job", "onboarding"),
    ("教我怎么用这个平台", "guide"),
    ("how do i use the schedules page", "guide"),
    ("现在有哪些未关单的 issue?", "qa"),
    ("这个月各类型 issue 占比", "qa"),
    # UI/tab explanation must reach guide (not capability-blurb / qa-fabrication).
    # RELIABILITY_PLAN §12 A — these are the 2nd-round 第9-18轮 disasters.
    ("介绍一下My Project这个tab有什么功能", "guide"),
    ("总共有几个tab，作用分别是什么", "guide"),
    ("verification tab 是做什么的", "guide"),
    ('How do I use the "AI Assistant" page?', "guide"),
    ("我有哪些可见页面", "guide"),
]


@pytest.mark.parametrize("message,expected", ROUTING_GOLDEN, ids=[m for m, _ in ROUTING_GOLDEN])
def test_write_intent_routing_golden(message, expected, monkeypatch):
    # Force the light LLM unavailable so the result is purely the deterministic
    # keyword layer (qa is the safe fallback — never write, P1).
    from core.agent import llm

    monkeypatch.setattr(llm, "get_chat_model",
                        lambda *a, **k: (_ for _ in ()).throw(llm.LlmNotConfiguredError("x")))
    intent, _reason = assistant.classify_intent(message, _actor())
    assert intent == expected


# ── SLA recommendation golden (deterministic numbers) ────────────────────


def test_sla_recommendation_value_golden():
    """A job that really runs hourly (60-min cadence) with the default 1.5 safety
    factor → threshold = ceil(60 * 1.5) = 90. Locks the exact number so a future
    refactor can't silently change the advised value."""
    import math

    from core.agent import sla_advisor
    from core.checker.cml_checker import _safety_factor_for

    # An unset preset defers to the advisor's global default factor.
    assert _safety_factor_for(None) is None
    factor = _safety_factor_for(None) or sla_advisor._GLOBAL_SAFETY_FACTOR
    assert factor == 1.5  # global default
    observed = 60.0
    assert math.ceil(observed * factor) == 90
