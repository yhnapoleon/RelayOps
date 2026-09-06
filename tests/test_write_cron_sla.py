"""Pillar D — user-specified cron / staleness
threshold write tools, and the capability-matrix binding that kills the
"切到写入模式却拿不到草案" 空头支票.

Reuses the existing job_sla commit path (kind="job_sla" → schedule_cron /
sla_custom_minutes), so no commit/schema change is needed — these tools just
accept a USER-given value instead of the auto-recomputed one.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.models.entities  # noqa: F401 — register tables
from core.auth.jwt import CurrentUser
from core.models.database import Base
from core.models.entities import Job, Product, Project
from core.models.user import UserRole

OWNER_ID = 99


def _admin():
    return CurrentUser(username="boss", user_id=1, role=UserRole.ADMIN)


@pytest.fixture
def job_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine)
    s = f()
    s.add(Project(id=1, name="FORECAST", owner_id=OWNER_ID, is_system=0))
    s.add(Product(id=1, project_id=1, name="Forecast"))
    s.add(Job(id=1, product_id=1, cml_job_name="Trigger Recommend",
              schedule_cron="0 14 * * 5"))
    s.commit()
    s.close()
    return f


# ── draft_edit_cron ───────────────────────────────────────────────────


def test_draft_edit_cron_proposes_user_value(job_factory):
    from core.agent.write_tools import draft_edit_cron
    s = job_factory()
    out = draft_edit_cron(s, _admin(), job_id=1, new_cron="0 18 * * 5")
    assert out["kind"] == "job_sla" and out["commit_path"] == "version_flow"
    ch = {c["field_path"]: c["new_value"] for c in out["changes"]}
    assert ch["schedule_cron"] == "0 18 * * 5"
    assert s.get(Job, 1).schedule_cron == "0 14 * * 5"  # P2: read-only preview
    s.close()


def test_draft_edit_cron_rejects_invalid_cron(job_factory):
    from core.agent.write_tools import draft_edit_cron
    s = job_factory()
    assert "error" in draft_edit_cron(s, _admin(), job_id=1, new_cron="not a cron")
    s.close()


def test_draft_edit_cron_same_value_is_noop_note(job_factory):
    from core.agent.write_tools import draft_edit_cron
    s = job_factory()
    out = draft_edit_cron(s, _admin(), job_id=1, new_cron="0 14 * * 5")
    assert "note" in out and "changes" not in out
    s.close()


# ── draft_set_sla_threshold ───────────────────────────────────────────


def test_draft_set_sla_threshold_proposes_custom_minutes(job_factory):
    from core.agent.write_tools import draft_set_sla_threshold
    s = job_factory()
    out = draft_set_sla_threshold(s, _admin(), job_id=1, custom_minutes=300)
    assert out["kind"] == "job_sla" and out["commit_path"] == "version_flow"
    ch = {c["field_path"]: c["new_value"] for c in out["changes"]}
    assert ch["sla_custom_minutes"] == "300"
    s.close()


def test_draft_set_sla_threshold_rejects_nonpositive(job_factory):
    from core.agent.write_tools import draft_set_sla_threshold
    s = job_factory()
    assert "error" in draft_set_sla_threshold(s, _admin(), job_id=1, custom_minutes=0)
    s.close()


# ── pool + capability-matrix binding (no 空头支票) ─────────────────────


def test_write_pool_exposes_cron_and_threshold_tools():
    pytest.importorskip("langchain_core")
    from core.agent.write_tools import build_write_tools
    names = {t.name for t in build_write_tools(_admin(), proposal_sink=[])}
    assert {"draft_edit_cron_tool", "draft_set_sla_threshold_tool"} <= names


def test_capability_matrix_write_caps_have_backing_tool():
    # Capability consistency: every CAP_WRITE capability must name draft tools
    # that actually exist — so the dual-track "能力轨" can never over-promise.
    pytest.importorskip("langchain_core")
    from core.agent import knowledge
    from core.agent.write_tools import build_write_tools
    pool = {t.name for t in build_write_tools(_admin(), proposal_sink=[])}
    for cap, tool_names in knowledge.WRITE_CAPABILITY_TOOLS.items():
        for tn in tool_names:
            assert tn in pool, f"capability {cap!r} names missing write tool {tn}"
