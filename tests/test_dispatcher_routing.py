"""S1 — dispatcher routing for diagnose + onboarding, and form staleness.

No real LLM/DB: classification is deterministic on keywords, the onboarding
branch is exercised with a stubbed draft, and the snapshot fingerprint check is
a pure function.
"""
import pytest

from core.agent import assistant
from core.auth.jwt import CurrentUser
from core.models.user import UserRole


def _actor():
    return CurrentUser(username="dora", user_id=42, role=UserRole.RELAYOPS_MEMBER)


# ── classify_intent extension ───────────────────────────────────────────


def test_classify_diagnose_keyword():
    assert assistant.classify_intent("诊断一下 #5 为什么失败", _actor()) == ("diagnose", "keyword")


def test_classify_onboarding_keyword():
    assert assistant.classify_intent("我要接入一个新的 mmp job", _actor()) == ("onboarding", "keyword")


def test_write_only_via_explicit_mode():
    # Write is reachable ONLY through the Write-mode toggle. A typed change
    # request stays read-only (qa nudges the user to switch) — never auto-write.
    assert assistant.classify_intent("把 #5 标记为误报", _actor()) == ("qa", "fastpath_qa")
    assert assistant.classify_intent("随便改点什么", _actor(), mode="write") == ("write", "manual:write")


def test_onboarding_via_explicit_mode():
    assert assistant.classify_intent("继续", _actor(), mode="onboarding") == ("onboarding", "manual:onboarding")


def test_classify_capability_keyword():
    assert assistant.classify_intent("你能做什么？", _actor()) == ("capability", "keyword")
    assert assistant.classify_intent("what can you do?", _actor()) == ("capability", "keyword")


def test_data_question_fastpaths_to_qa_without_llm():
    # Obvious data vocabulary → qa directly, no classifier round-trip (so the
    # dominant question path pays zero added latency).
    assert assistant.classify_intent("现在有 outstanding issue 吗", _actor()) == ("qa", "fastpath_qa")


def test_capability_turn_is_static_no_tools():
    events = list(assistant._capability_turn(_actor(), "t1", "你能做什么"))
    assert [e["event"] for e in events] == ["meta", "answer", "done"]
    assert "写入模式" in next(e for e in events if e["event"] == "answer")["data"]["text"]
    assert not any(e["event"] == "tool_call" for e in events)


def test_form_context_preamble_uses_live_payload():
    snap = {
        "draftId": 9,
        "dirty": True,
        "payload": {"project": {"name": "Inventory Scoring"}, "products": [{"name": "NS"}]},
        "clarifications": [{"field_path": "products[0].apps[0].application_url"}],
        "answers": {"products[0].apps[0].application_url": "https://x"},
    }
    pre = assistant._form_context_preamble(snap, None, _actor())
    assert pre is not None
    assert "草稿 #9" in pre and "未保存" in pre
    assert "Inventory Scoring" in pre  # live payload embedded
    assert "application_url" in pre  # clarification path surfaced


def test_form_context_preamble_none_when_no_form():
    assert assistant._form_context_preamble(None, None, _actor()) is None
    assert assistant._form_context_preamble({}, None, _actor()) is None


def test_parse_issue_ref():
    assert assistant._parse_issue_ref("诊断 #123 谢谢") == 123
    assert assistant._parse_issue_ref("diagnose issue 45 please") == 45
    assert assistant._parse_issue_ref("why did it fail?") is None


# ── onboarding branch ────────────────────────────────────────────────────


def test_onboarding_no_draft_opens_wizard():
    events = list(assistant._onboarding_turn(_actor(), "t1", draft_ref=None, form_snapshot=None))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    fs = next(e for e in events if e["event"] == "form_sync")
    assert fs["data"]["reason"] == "opened" and fs["data"]["draft_id"] is None


def test_onboarding_with_draft_summarizes(monkeypatch):
    import core.agent.draft_service as draft_service
    import core.models.database as db_module

    class _Draft:
        id = 7
        status = "ready"
        validation = {"clarifications": [{"field_path": "products[0].apps[0].application_url"}]}
        payload = {"name": "X"}

    monkeypatch.setattr(db_module, "get_db", lambda: None)
    monkeypatch.setattr(draft_service, "get_draft", lambda db, draft_id, *, actor: _Draft())

    events = list(assistant._onboarding_turn(_actor(), "t1", draft_ref=7, form_snapshot=None))
    fs = next(e for e in events if e["event"] == "form_sync")
    assert fs["data"] == {"draft_id": 7, "reason": "refreshed"}
    answer = next(e for e in events if e["event"] == "answer")["data"]["text"]
    assert "草稿 #7" in answer and "1 个待澄清" in answer


# ── form snapshot staleness (Req 17.2) ───────────────────────────────────


def test_payload_hash_staleness():
    class _Draft:
        payload = {"a": 1, "b": [2, 3]}

    fresh = assistant._payload_hash(_Draft.payload)
    assert assistant._snapshot_is_stale(_Draft(), {"hash": fresh}) is False
    assert assistant._snapshot_is_stale(_Draft(), {"hash": "deadbeef"}) is True
    # No snapshot / no hash → never considered stale.
    assert assistant._snapshot_is_stale(_Draft(), None) is False
    assert assistant._snapshot_is_stale(_Draft(), {}) is False
