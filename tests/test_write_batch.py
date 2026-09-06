"""Write mode: batch proposals + read tools bound (bug fixes).

Three reported write-mode defects this guards against:
  1. Batch edits silently dropped — the turn kept only the FIRST proposal.
     Now every distinct proposal is emitted with its own confirm token.
  2. (prompt) language mirroring — not unit-testable here; covered by the
     prompt rewrite.
  3. Read tools "forgotten" — write mode bound only draft_* tools, so the model
     couldn't look up which entities to act on. Now the read tools are bound
     alongside the write tools.
"""
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

from datetime import datetime

NOW = datetime(2026, 6, 12, 9, 0)


def _actor(role=UserRole.RELAYOPS_MEMBER, uid=42, username="dora"):
    return CurrentUser(username=username, user_id=uid, role=role)


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine)
    s = f()
    for iid in (10, 11):
        s.add(Issue(id=iid, type=IssueType.JOB_FAILED, status=IssueStatus.OPEN,
                    title=f"issue {iid}", product_id=1, job_id=1, created_by=1,
                    assignee_id=42, created_at=NOW))
    s.commit()
    s.close()
    return f


# ── Fix #3: read tools are bound in write mode ───────────────────────────


def test_write_graph_binds_read_and_write_tools(monkeypatch):
    pytest.importorskip("langgraph")
    import langgraph.prebuilt as prebuilt
    from core.agent import write

    captured = {}

    def _fake_create(model, tools, **kwargs):
        captured["tools"] = tools
        return object()

    monkeypatch.setattr(prebuilt, "create_react_agent", _fake_create)
    # A truthy model avoids get_chat_model(); create_react_agent is stubbed.
    write.build_write_graph(_actor(), model=object(), proposal_sink=[], artifact_sink=[])

    names = {t.name for t in captured["tools"]}
    # Write tools present …
    assert "draft_false_positive_tool" in names
    assert "draft_resolve_issue_tool" in names
    # … AND the read-only query tools the model needs to find batch targets.
    assert any(n.startswith("relayops_") for n in names), names
    assert "relayops_list_issues" in names


# ── Fix #1: a batch turn emits one proposal (with its own token) per change ──


def _scripted_model(tool_calls, final_text="Done."):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

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

    return ScriptedModel(script=[
        AIMessage(content="", tool_calls=tool_calls),
        AIMessage(content=final_text),
    ])


def _wire_db(monkeypatch, factory):
    import core.agent.write_tools as write_tools
    import core.models.database as db_module

    class _Db:
        def get_session(self):
            return factory()

    fake = _Db()
    monkeypatch.setattr(write_tools, "get_db", lambda: fake)
    monkeypatch.setattr(db_module, "get_db", lambda: fake)


def test_batch_turn_emits_one_proposal_per_issue(factory, monkeypatch):
    pytest.importorskip("langgraph")
    from core.agent import write, write_proposal_store as store

    _wire_db(monkeypatch, factory)

    model = _scripted_model([
        {"name": "draft_false_positive_tool",
         "args": {"issue_id": 10, "resolution_description": "noise"}, "id": "c1"},
        {"name": "draft_false_positive_tool",
         "args": {"issue_id": 11, "resolution_description": "noise"}, "id": "c2"},
    ])

    events = list(write.run_turn(_actor(), "把 #10 #11 都标记误报", thread_id="b1", model=model))

    proposals = [e for e in events if e["event"] == "write_proposal"]
    assert len(proposals) == 2, [e["event"] for e in events]
    ids = sorted(p["data"]["entity_id"] for p in proposals)
    assert ids == [10, 11]
    tokens = [p["data"]["confirm_token"] for p in proposals]
    assert all(tokens) and len(set(tokens)) == 2  # distinct, both persisted

    # Both tokens resolve, and neither issue was mutated (propose-only).
    s = factory()
    for token in tokens:
        _p, reason = store.take_valid(s, token=token, actor_id=42)
        assert reason == store.TAKE_OK
    assert s.get(Issue, 10).status == IssueStatus.OPEN
    assert s.get(Issue, 11).status == IssueStatus.OPEN
    s.close()


def test_duplicate_proposals_are_deduped(factory, monkeypatch):
    pytest.importorskip("langgraph")
    from core.agent import write

    _wire_db(monkeypatch, factory)

    # Model double-calls the SAME change — only one card should surface.
    model = _scripted_model([
        {"name": "draft_false_positive_tool",
         "args": {"issue_id": 10, "resolution_description": "noise"}, "id": "c1"},
        {"name": "draft_false_positive_tool",
         "args": {"issue_id": 10, "resolution_description": "noise"}, "id": "c2"},
    ])

    events = list(write.run_turn(_actor(), "标记 #10 误报", thread_id="b2", model=model))
    proposals = [e for e in events if e["event"] == "write_proposal"]
    assert len(proposals) == 1
