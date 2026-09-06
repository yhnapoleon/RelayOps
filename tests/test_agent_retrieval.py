"""FTS5 retrieval sidecar — CJK bigrams, BM25 search, hybrid ranking,
RBAC re-filter in the tool layer, backfill."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.models.entities  # noqa: F401
from core.agent import retrieval, tools
from core.auth.jwt import CurrentUser
from core.models.database import Base
from core.models.user import UserRole

from test_agent_tools import _seed


@pytest.fixture(autouse=True)
def _fts_tmp(monkeypatch):
    """Point the sidecar index at a temp file and re-enable FTS per test.

    (tempfile.mkdtemp instead of pytest tmp_path — the pytest-of-* basetemp
    is permission-locked on this Windows setup.)"""
    import shutil
    import tempfile
    from pathlib import Path

    tmpdir = Path(tempfile.mkdtemp(prefix="relayops-fts-"))
    monkeypatch.setattr(retrieval, "_fts_path", lambda: tmpdir / "fts.sqlite3")
    monkeypatch.setattr(retrieval, "_fts_disabled", False)
    yield
    shutil.rmtree(tmpdir, ignore_errors=True)


def _resolved_issue(**overrides):
    base = dict(
        id=101, type="job_failed", job_id=1, app_id=None, product_id=1,
        status="resolved", selected_scenario_type="", action_summary_json=None,
        title="ns_daily failed: upstream kerberos ticket expired",
        description="job ns_daily failed at 02:00, kerberos authentication error",
        resolution_description="续期 kerberos ticket 后重跑 ns_daily 成功",
        resolved_at=datetime(2026, 6, 1, 10, 0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── text preparation ──────────────────────────────────────────────────


def test_cjk_ngrams_expand_chinese_runs():
    out = retrieval._cjk_ngrams("模型漂移 detected")
    assert "模型" in out and "型漂" in out and "漂移" in out
    assert "detected" in out


def test_match_query_is_safe_or_query():
    q = retrieval._match_query('kerberos "ticket" AND (boom)')
    assert q.startswith('"') and " OR " in q
    assert "AND" not in q.replace('"AND"', "")  # operators only inside quotes


def test_empty_query_yields_no_match():
    assert retrieval._match_query("。！？") == ""
    assert retrieval.search_resolutions("") == []


# ── index + search round trip ─────────────────────────────────────────


def test_index_and_search_english_and_chinese():
    assert retrieval.index_issue(_resolved_issue()) is True

    hits = retrieval.search_resolutions("kerberos ticket expired")
    assert hits and hits[0]["issue_id"] == 101
    assert "续期" in hits[0]["resolution"]  # display text, not bigram soup

    hits_cn = retrieval.search_resolutions("漂移")
    assert hits_cn == []  # no drift content indexed

    assert retrieval.index_issue(_resolved_issue(
        id=102, type="mmp_drift", title="模型漂移告警",
        resolution_description="确认模型漂移为数据季节性，标记误报")) is True
    hits_cn = retrieval.search_resolutions("模型漂移")
    assert hits_cn and hits_cn[0]["issue_id"] == 102


def test_issue_without_resolution_not_indexed():
    assert retrieval.index_issue(_resolved_issue(resolution_description="")) is False


def test_reindex_replaces_old_row():
    retrieval.index_issue(_resolved_issue())
    retrieval.index_issue(_resolved_issue(resolution_description="新的处置 kerberos renewed"))
    hits = retrieval.search_resolutions("kerberos")
    assert len([h for h in hits if h["issue_id"] == 101]) == 1
    assert "新的处置" in hits[0]["resolution"]


def test_issue_type_filter():
    retrieval.index_issue(_resolved_issue())
    retrieval.index_issue(_resolved_issue(id=102, type="app_offline",
                                          title="app down kerberos",
                                          resolution_description="restarted app, kerberos ok"))
    hits = retrieval.search_resolutions("kerberos", issue_type="app_offline")
    assert {h["issue_id"] for h in hits} == {102}


# ── tiered similar retrieval (P2) ────────────────────────────────────


def _current(**overrides):
    base = dict(id=1, type="job_failed", job_id=1, app_id=None, product_id=1,
                selected_scenario_type="",
                title="ns_daily failed upstream missing",
                description="upstream missing")
    base.update(overrides)
    return SimpleNamespace(**base)


def test_retrieve_similar_prefers_same_job_over_pure_text():
    retrieval.index_issue(_resolved_issue(
        id=201, job_id=1, type="job_failed",
        title="ns_daily failed upstream missing",
        resolution_description="rerun after upstream landed"))
    retrieval.index_issue(_resolved_issue(
        id=202, job_id=99, type="job_failed",
        title="other_job failed upstream missing badly upstream missing",
        resolution_description="patched other_job"))

    out = retrieval.retrieve_similar(None, _current(), limit=2)
    assert out[0]["issue_id"] == 201
    assert "同一 Job" in out[0]["match_reason"]


def test_signature_tier_beats_plain_same_type():
    # Different jobs, same issue type; only 301 shares the kerberos signature.
    retrieval.index_issue(_resolved_issue(
        id=301, job_id=50, title="etl_x failed kinit error",
        description="kerberos ticket could not be renewed",
        resolution_description="renewed keytab"))
    retrieval.index_issue(_resolved_issue(
        id=302, job_id=51, title="etl_y failed mysteriously",
        description="no idea", resolution_description="rerun worked"))

    current = _current(job_id=999, title="new_job failed kerberos auth",
                       description="kinit failure")
    out = retrieval.retrieve_similar(None, current, limit=2)
    assert out[0]["issue_id"] == 301
    assert "kerberos" in out[0]["match_reason"]
    # 302 only qualifies via the broader same-type tier.
    by_id = {h["issue_id"]: h for h in out}
    assert "同类型" in by_id[302]["match_reason"]


def test_false_positive_records_rank_last_and_are_flagged():
    retrieval.index_issue(_resolved_issue(
        id=401, job_id=1, status="false_positive",
        title="ns_daily failed blip",
        resolution_description="known monitoring blip, dismissed"))
    retrieval.index_issue(_resolved_issue(
        id=402, job_id=1,
        title="ns_daily failed for real",
        resolution_description="restarted upstream feed"))

    out = retrieval.retrieve_similar(None, _current(), limit=2)
    assert [h["issue_id"] for h in out] == [402, 401]
    assert "误报关单" in out[1]["match_reason"]


def test_scenario_type_tier():
    retrieval.index_issue(_resolved_issue(
        id=501, job_id=70, selected_scenario_type="dependency_failed",
        title="dep job failed", resolution_description="waited for upstream"))
    current = _current(job_id=999, selected_scenario_type="dependency_failed",
                       title="totally different words", description="")
    out = retrieval.retrieve_similar(None, current, limit=2)
    assert out[0]["issue_id"] == 501
    assert "同场景类型" in out[0]["match_reason"]


def test_schema_version_bump_rebuilds_index():
    retrieval.index_issue(_resolved_issue())
    assert retrieval.search_resolutions("kerberos")
    # Simulate a stale layout: stamp an old user_version and reconnect.
    import sqlite3

    raw = sqlite3.connect(str(retrieval._fts_path()))
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    assert retrieval.search_resolutions("kerberos") == []  # rebuilt empty
    conn = sqlite3.connect(str(retrieval._fts_path()))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == retrieval._SCHEMA_VERSION
    conn.close()


def test_runbook_search():
    retrieval.index_scenarios([
        (1, "job", 1, "triggered_but_failed", "Kerberos renewal",
         "fails when kerberos ticket expires", "renew ticket\nrerun job", "ops@example.com"),
        (2, "app", 1, "offline", "重启应用", "应用离线时", "在 CML 重启", ""),
    ])
    hits = retrieval.search_runbooks("kerberos")
    assert hits and hits[0]["scenario_id"] == 1
    hits_cn = retrieval.search_runbooks("重启")
    assert hits_cn and hits_cn[0]["scenario_id"] == 2


# ── tool layer RBAC re-filter ─────────────────────────────────────────


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    _seed(s)
    yield s
    s.close()


def test_search_tool_filters_invisible_issues(db_session):
    # Index two resolutions: issue 3 exists in the DB (visible to admin),
    # issue 999 doesn't exist → must be filtered out even though FTS hits it.
    retrieval.index_issue(_resolved_issue(
        id=3, title="resolved one kerberos", resolution_description="rerun fixed it kerberos"))
    retrieval.index_issue(_resolved_issue(
        id=999, title="ghost kerberos", resolution_description="secret kerberos fix"))

    admin = CurrentUser(username="boss", user_id=1, role=UserRole.ADMIN)
    out = tools.search_knowledge(db_session, admin, query="kerberos")
    ids = {h["issue_id"] for h in out["data"]["hits"]}
    assert ids == {3}


def test_search_tool_validates_enums(db_session):
    admin = CurrentUser(username="boss", user_id=1, role=UserRole.ADMIN)
    bad = tools.search_knowledge(db_session, admin, query="x", target="everything")
    assert bad["valid_values"] == ["resolutions", "runbooks"]
    empty = tools.search_knowledge(db_session, admin, query="  ")
    assert "error" in empty


# ── backfill ──────────────────────────────────────────────────────────


def test_backfill_indexes_resolved_issues_and_scenarios(db_session, monkeypatch):
    from core.infrastructure import resolution_fts_backfill as bf
    from core.models.entities import JobFailureScenario

    db_session.add(JobFailureScenario(
        job_id=1, scenario_type="triggered_but_failed", scenario_name="Kerberos renewal",
        condition_description="kerberos expiry", action_steps=["renew", "rerun"],
        escalation_target="ops@example.com"))
    db_session.commit()

    class _Db:
        def get_session(self):
            return db_session

    # Keep the test session open across the backfill's close().
    monkeypatch.setattr(db_session, "close", lambda: None)
    result = bf.run_backfill(db=_Db())
    # Seeded terminal issues with resolution: #3 (resolved) + #4 (false_positive).
    assert result["issues"] == 2
    assert result["scenarios"] == 1
    assert retrieval.search_runbooks("kerberos")[0]["scenario_name"] == "Kerberos renewal"
    assert {h["issue_id"] for h in retrieval.search_resolutions("rerun")} >= {3}
