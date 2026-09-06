"""Structural guardrails: shared language head,
output sanitizer, and the per-turn tool dedup / soft cap.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.models.entities  # noqa: F401 — register tables on Base
from core.agent import chat, tools
from core.auth.jwt import CurrentUser
from core.models.database import Base
from core.models.entities import Issue, Job, Product, Project
from core.models.constants import IssueStatus, IssueType
from core.models.user import User, UserRole


# ── shared language head + prompt format strings ──────────────────────


def test_prompts_format_with_shared_lang_rule():
    # Both branches must accept the shared rule key — guards against a brace /
    # placeholder drift breaking graph build at runtime.
    from core.agent.guide import GUIDE_SYSTEM

    assert chat.CHAT_SYSTEM.format(username="u", role="admin", domain_card="x",
                                   lang_rule=chat.SHARED_LANG_RULE)
    assert GUIDE_SYSTEM.format(username="u", role="admin",
                               lang_rule=chat.SHARED_LANG_RULE)


def test_shared_lang_rule_used_by_both_branches():
    import core.agent.guide as guide
    # Same object, one source of truth.
    assert guide.SHARED_LANG_RULE is chat.SHARED_LANG_RULE


# ── output sanitizer ──────────────────────────────────────────────────


def test_sanitizer_strips_leaked_nav_json():
    text = '前往这些产品：\n```json\n{"product_ids": [1, 2]}\n```\n请点击。'
    out = chat._sanitize_answer(text)
    assert "product_ids" not in out
    assert "前往这些产品" in out and "请点击" in out


def test_sanitizer_strips_inline_button_object():
    out = chat._sanitize_answer('看这里 {"project_ids":[3]} 就好')
    assert "project_ids" not in out


def test_sanitizer_keeps_normal_prose_and_tables():
    text = "Issue #5 仍 open。\n\n| id | status |\n|----|--------|\n| 5 | open |"
    assert chat._sanitize_answer(text) == text


# ── per-turn tool dedup + soft cap ────────────────────────────────────


@pytest.fixture
def fake_db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    s.add(User(id=1, username="boss", role=UserRole.ADMIN))
    s.add(Project(id=1, name="P", owner_id=1))
    s.add(Product(id=1, project_id=1, name="Prod"))
    s.add(Job(id=1, product_id=1, cml_job_name="daily8", schedule_cron="0 8 * * *"))
    for i in range(8):
        s.add(Issue(id=i + 1, type=IssueType.JOB_FAILED, status=IssueStatus.OPEN,
                    title=f"f{i}", product_id=1, job_id=1, created_by=1,
                    created_at=datetime(2026, 6, 5, 8, 0, 0)))
    s.commit()
    s.close()

    class _FakeDB:
        def get_session(self):
            return SessionLocal()

    monkeypatch.setattr(tools, "get_db", lambda: _FakeDB())
    return SessionLocal


@pytest.fixture
def admin():
    return CurrentUser(username="boss", user_id=1, role=UserRole.ADMIN)


def _tool(toolset, name):
    return {t.name: t for t in toolset}[name]


def test_exact_repeat_returns_cached_object(fake_db, admin):
    pytest.importorskip("langchain_core")
    toolset = tools.build_langchain_tools(admin)
    sla = _tool(toolset, "relayops_job_sla_config")
    r1 = sla.invoke({"job_id": 1})
    r2 = sla.invoke({"job_id": 1})
    assert r1 is r2  # second call served from the per-turn cache, no re-query


def test_runaway_backstop_blocks_only_past_the_cap(fake_db, admin):
    pytest.importorskip("langchain_core")
    toolset = tools.build_langchain_tools(admin)
    lst = _tool(toolset, "relayops_list_issues")
    limit = tools._PER_TOOL_CALL_LIMIT
    results = [lst.invoke({"days": d}) for d in range(1, limit + 2)]
    # Everything up to the cap succeeds; only the (limit+1)-th is refused.
    assert isinstance(results[-1], dict) and "safety cap" in results[-1]["error"]
    assert not (isinstance(results[0], dict) and "error" in results[0])


def test_legitimate_fanout_not_broken_by_cap(fake_db, admin):
    # The 2nd-round regression: "check every job" hit a low cap and returned no
    # data. The backstop must sit well above a dozen distinct per-job calls.
    pytest.importorskip("langchain_core")
    assert tools._PER_TOOL_CALL_LIMIT >= 20
    toolset = tools.build_langchain_tools(admin)
    sla = _tool(toolset, "relayops_job_sla_config")
    out = [sla.invoke({"job_id": jid}) for jid in range(1, 13)]
    assert all(not (isinstance(r, dict) and "safety cap" in r.get("error", "")) for r in out)


# ── guide: no room to fabricate tabs ──────


def test_strip_markdown_tables_removes_table_keeps_prose():
    text = "可见页面如下：\n\n| Key | 页面 |\n|-----|------|\n| x | y |\n\n点选其一。"
    out = chat._strip_markdown_tables(text)
    assert "|" not in out
    assert "可见页面如下" in out and "点选其一" in out


def test_guide_offer_tabs_returns_real_tabs_and_is_idempotent(admin):
    # Root-cause fix: hand the model the REAL tab list (so it can't invent one),
    # and push the clickable card only ONCE per turn (no 3× spam).
    pytest.importorskip("langchain_core")
    from core.agent.guide import _build_guide_tools

    sink: list = []
    tools_list = _build_guide_tools(admin, None, guide_sink=sink)
    offer = {t.name: t for t in tools_list}["guide_offer_tabs"]

    r1 = offer.invoke({})
    keys = {t["key"] for t in r1["tabs"]}
    assert "ai-assistant" in keys and "projects" in keys  # real keys, from code
    offer.invoke({})  # second call same turn
    cards = [x for x in sink if x.get("_kind") == "guide_step"]
    assert len(cards) == 1  # card pushed once, not per call
