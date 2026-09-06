"""S0 — the thin write vertical slice (propose → confirm).

Covers the slice's correctness properties without a real LLM/HTTP:
  * P1 route safety — light LLM unavailable never routes to write;
  * P2 read-only preview — a draft tool mutates nothing;
  * P9 terminal protection — terminal issues yield an error, not a proposal;
  * P6 token ownership / expiry, P3 single-consume — the persisted store;
  * commit dispatch maps a proposal to the right service write;
  * the write turn emits a write_proposal carrying a persisted token.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.models.entities  # noqa: F401 — register tables
from core.auth.jwt import CurrentUser
from core.models.constants import IssueStatus, IssueType
from core.models.database import Base
from core.models.entities import Issue
from core.models.user import UserRole

NOW = datetime(2026, 6, 12, 9, 0)


def _actor(role=UserRole.RELAYOPS_MEMBER, uid=42, username="dora"):
    return CurrentUser(username=username, user_id=uid, role=role)


@pytest.fixture
def factory():
    # StaticPool + one shared connection so the tool's session (langgraph may
    # run it on another thread) sees the same in-memory DB.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine)
    s = f()
    s.add(Issue(id=1, type=IssueType.JOB_FAILED, status=IssueStatus.OPEN,
                title="ns_daily failed", product_id=1, job_id=1, created_by=1,
                assignee_id=42, created_at=NOW))
    s.add(Issue(id=2, type=IssueType.JOB_FAILED, status=IssueStatus.RESOLVED,
                title="already done", product_id=1, job_id=1, created_by=1,
                assignee_id=42, created_at=NOW))
    s.commit()
    s.close()
    return f


# ── P1: classify_intent route safety ────────────────────────────────────


def test_classify_falls_back_to_qa_when_llm_unavailable(monkeypatch):
    from core.agent import assistant
    from core.agent import llm

    def _boom(*a, **k):
        raise llm.LlmNotConfiguredError("no gateway")

    monkeypatch.setattr(llm, "get_chat_model", _boom)
    # Ambiguous (no keyword / no data fast-path) text + unavailable LLM → must
    # be qa, never write (P1).
    intent, reason = assistant.classify_intent("帮我看看那个东西好不好", _actor())
    assert intent == "qa"
    assert reason == "fallback_qa"


def test_typed_change_request_never_auto_writes(monkeypatch):
    from core.agent import assistant
    from core.agent import llm

    # Write is mode-only: a typed change request must stay read-only even with
    # the LLM unavailable — it is NEVER silently routed to a write proposal (P1).
    monkeypatch.setattr(llm, "get_chat_model", lambda *a, **k: (_ for _ in ()).throw(
        llm.LlmNotConfiguredError("x")))
    intent, _reason = assistant.classify_intent("把 #123 标记为误报", _actor())
    assert intent != "write"


def test_classify_mode_short_circuits():
    from core.agent import assistant
    intent, reason = assistant.classify_intent("anything", _actor(), mode="write")
    assert (intent, reason) == ("write", "manual:write")


# ── P2 / P9: draft_false_positive read-only preview + terminal guard ─────


def test_draft_false_positive_proposes_without_mutating(factory):
    from core.agent.write_tools import draft_false_positive
    s = factory()
    result = draft_false_positive(s, _actor(), issue_id=1, resolution_description="schedule noise")
    assert result["kind"] == "false_positive"
    assert result["requires_resolution"] is True
    assert result["changes"][0]["new_value"] == IssueStatus.FALSE_POSITIVE
    # P2: nothing changed on the row.
    assert s.get(Issue, 1).status == IssueStatus.OPEN
    s.close()


def test_draft_false_positive_terminal_is_error(factory):
    from core.agent.write_tools import draft_false_positive
    s = factory()
    result = draft_false_positive(s, _actor(), issue_id=2, resolution_description="x")
    assert "error" in result and "terminal" in result["error"].lower()
    s.close()


def test_draft_false_positive_requires_reason(factory):
    from core.agent.write_tools import draft_false_positive
    s = factory()
    result = draft_false_positive(s, _actor(), issue_id=1, resolution_description="  ")
    assert "clarification" in result
    s.close()


def test_draft_false_positive_rbac_denies_unrelated_regular_user(factory):
    from core.agent.write_tools import draft_false_positive
    s = factory()
    stranger = _actor(role=UserRole.REGULAR_USER, uid=7, username="bob")
    result = draft_false_positive(s, stranger, issue_id=1, resolution_description="x")
    assert result.get("rbac_ok") is False
    s.close()


def test_draft_false_positive_not_found(factory):
    from core.agent.write_tools import draft_false_positive
    s = factory()
    result = draft_false_positive(s, _actor(), issue_id=999, resolution_description="x")
    assert "error" in result and "not found" in result["error"].lower()
    s.close()


# ── P6 / P3: persisted proposal store ───────────────────────────────────


def _proposal():
    from core.agent.write_schemas import FieldChange, WriteProposal
    return WriteProposal(
        kind="false_positive", entity_type="issue", entity_id=1,
        title="Mark Issue #1 as false positive",
        changes=[FieldChange(field_path="status", label="Status",
                             old_value="open", new_value="false_positive",
                             rationale="noise")],
        requires_resolution=True,
    )


def test_store_roundtrip_and_ownership(factory):
    from core.agent import write_proposal_store as store
    s = factory()
    token = store.put(s, actor_id=42, proposal=_proposal())

    # Owner can take; the token is stamped into the dump.
    p, reason = store.take_valid(s, token=token, actor_id=42)
    assert reason == store.TAKE_OK and p.confirm_token == token
    # P6: a different actor is forbidden.
    _, reason = store.take_valid(s, token=token, actor_id=7)
    assert reason == store.TAKE_FORBIDDEN
    s.close()


def test_store_single_consume_and_replay(factory):
    from core.agent import write_proposal_store as store
    s = factory()
    token = store.put(s, actor_id=42, proposal=_proposal())
    # P3: first consume claims, second fails.
    assert store.consume(s, token=token) is True
    assert store.consume(s, token=token) is False
    # After consume, replay is rejected.
    _, reason = store.take_valid(s, token=token, actor_id=42)
    assert reason == store.TAKE_CONSUMED
    s.close()


def test_store_expiry(factory):
    from core.agent import write_proposal_store as store
    s = factory()
    token = store.put(s, actor_id=42, proposal=_proposal(), ttl_seconds=-10)
    _, reason = store.take_valid(s, token=token, actor_id=42)
    assert reason == store.TAKE_EXPIRED
    s.close()


# ── commit dispatch ─────────────────────────────────────────────────────


def test_commit_proposal_dispatches_false_positive(monkeypatch):
    from api.routers import agent_actions

    captured = {}

    class _Issue:
        id = 1
        status = IssueStatus.FALSE_POSITIVE

    def _fake_update_issue(**kwargs):
        captured.update(kwargs)
        return _Issue()

    monkeypatch.setattr(agent_actions.issue_service, "update_issue", _fake_update_issue)
    result = agent_actions._commit_proposal(_proposal(), _actor(), session=None)

    assert result == {"entity": "issue", "issue_id": 1, "new_status": IssueStatus.FALSE_POSITIVE}
    assert captured["new_status"] == IssueStatus.FALSE_POSITIVE
    assert captured["resolution_description"] == "noise"
    assert captured["is_elevated"] is True  # relayops_member is elevated


def test_commit_proposal_dispatches_record_step(monkeypatch):
    from api.routers import agent_actions
    from core.agent.write_schemas import FieldChange, WriteProposal
    from core.models.constants import IssueActionType

    captured = {}

    class _Issue:
        id = 3
        status = IssueStatus.IN_PROGRESS

    def _fake_run_action(**kwargs):
        captured.update(kwargs)
        return _Issue()

    monkeypatch.setattr(agent_actions.issue_service, "run_action", _fake_run_action)
    proposal = WriteProposal(
        kind="record_step", entity_type="issue", entity_id=3,
        title="Record a handling step on Issue #3",
        changes=[FieldChange(field_path="action_summary.steps", label="Step",
                             old_value="", new_value="checked logs", rationale="")],
    )
    result = agent_actions._commit_proposal(proposal, _actor(), session=None)
    assert result == {"entity": "issue", "issue_id": 3, "action": "record_step"}
    assert captured["action"] == IssueActionType.RECORD_STEP
    assert captured["notes"] == "checked logs"


# ── S2: new issue tools + tool-pool boundary ─────────────────────────────


def test_draft_resolve_issue_proposes(factory):
    from core.agent.write_tools import draft_resolve_issue
    s = factory()
    out = draft_resolve_issue(s, _actor(), issue_id=1, resolution_description="reran job")
    assert out["kind"] == "resolve_issue"
    assert out["changes"][0]["new_value"] == IssueStatus.RESOLVED
    assert s.get(Issue, 1).status == IssueStatus.OPEN  # P2 read-only
    s.close()


def test_draft_update_status_rejects_terminal_target(factory):
    from core.agent.write_tools import draft_update_issue_status
    s = factory()
    out = draft_update_issue_status(s, _actor(), issue_id=1, new_status=IssueStatus.RESOLVED)
    assert "error" in out and IssueStatus.IN_PROGRESS in out["valid_values"]
    # An active transition is fine.
    ok = draft_update_issue_status(s, _actor(), issue_id=1, new_status=IssueStatus.IN_PROGRESS)
    assert ok["kind"] == "update_issue_status"
    s.close()


def test_draft_record_step_requires_note(factory):
    from core.agent.write_tools import draft_record_step
    s = factory()
    assert "clarification" in draft_record_step(s, _actor(), issue_id=1, step_note="  ")
    ok = draft_record_step(s, _actor(), issue_id=1, step_note="checked logs")
    assert ok["kind"] == "record_step" and ok["changes"][0]["new_value"] == "checked logs"
    s.close()


def test_new_tools_inherit_terminal_and_rbac_guards(factory):
    from core.agent.write_tools import draft_resolve_issue, draft_record_step
    s = factory()
    # P9: terminal issue (#2 resolved) yields an error, not a proposal.
    assert "error" in draft_resolve_issue(s, _actor(), issue_id=2, resolution_description="x")
    # P4: unrelated regular_user is denied.
    stranger = _actor(role=UserRole.REGULAR_USER, uid=7, username="bob")
    assert draft_record_step(s, stranger, issue_id=1, step_note="x").get("rbac_ok") is False
    s.close()


def test_tool_pool_only_exposes_t1_tools():
    """P10/P14: the write tool pool contains only T1 (reversible, single-write)
    tools — no owner-transfer / delete / approval / new-entity / admin tools
    exist for the agent to call, regardless of the actor's UI permissions."""
    from core.agent.write_tools import build_write_tools
    tools = build_write_tools(_actor(), proposal_sink=[])
    names = {t.name for t in tools}
    assert names == {
        "draft_false_positive_tool",
        "draft_resolve_issue_tool",
        "draft_update_issue_status_tool",
        "draft_record_step_tool",
        "draft_job_sla_tool",
        "draft_edit_cron_tool",
        "draft_set_sla_threshold_tool",
        "draft_edit_project_tool",
        "draft_edit_product_tool",
        "draft_set_member_role_tool",
    }
    # The high-risk operations stay out of the pool entirely (guide-only T2/T3):
    # owner transfer, any delete, handover/version approve·reject, new-entity
    # creation, LDAP, global-role change.
    forbidden = ("owner", "transfer", "delete", "approve", "reject", "create",
                 "ldap", "global")
    assert not any(any(f in n for f in forbidden) for n in names)


# ── write turn emits a write_proposal with a persisted token ─────────────


def test_write_turn_emits_proposal_with_token(factory, monkeypatch):
    pytest.importorskip("langgraph")
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    import core.agent.write_tools as write_tools
    import core.models.database as db_module
    from core.agent import write, write_proposal_store as store

    class _Db:
        def get_session(self):
            return factory()

    fake = _Db()
    monkeypatch.setattr(write_tools, "get_db", lambda: fake)
    monkeypatch.setattr(db_module, "get_db", lambda: fake)

    class ScriptedModel(BaseChatModel):
        script: list
        calls: int = 0

        @property
        def _llm_type(self):
            return "scripted"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            msg = self.script[min(self.calls, len(self.script) - 1)]
            self.calls += 1
            return ChatResult(generations=[ChatGeneration(message=msg)])

        def bind_tools(self, tools, **kwargs):
            return self

    model = ScriptedModel(script=[
        AIMessage(content="", tool_calls=[{
            "name": "draft_false_positive_tool",
            "args": {"issue_id": 1, "resolution_description": "schedule noise"},
            "id": "c1"}]),
        AIMessage(content="已生成待确认的误报标记。"),
    ])

    events = list(write.run_turn(_actor(), "把 #1 标记为误报", thread_id="t1", model=model))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    wp = next(e for e in events if e["event"] == "write_proposal")
    token = wp["data"]["confirm_token"]
    assert token and wp["data"]["kind"] == "false_positive"

    # Token is persisted and resolvable; the issue was NOT mutated (propose-only).
    s = factory()
    p, reason = store.take_valid(s, token=token, actor_id=42)
    assert reason == store.TAKE_OK and p.entity_id == 1
    assert s.get(Issue, 1).status == IssueStatus.OPEN
    s.close()
