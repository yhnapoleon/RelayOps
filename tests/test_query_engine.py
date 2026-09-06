"""Generic query engine — spec validation, multi-dim grouping, FP-corrected
execution metrics, RBAC scoping. Runs over the same June-2026 seed as
test_agent_tools."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.models.entities  # noqa: F401
from core.agent.query_engine import run_query
from core.auth.jwt import CurrentUser
from core.models.constants import IssueType
from core.models.database import Base
from core.models.entities import Issue
from core.models.user import UserRole

from test_agent_tools import _seed


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    _seed(s)
    yield s
    s.close()


@pytest.fixture
def admin() -> CurrentUser:
    return CurrentUser(username="boss", user_id=1, role=UserRole.ADMIN)


# ── spec validation ───────────────────────────────────────────────────


def test_rejects_unknown_entity(session, admin):
    out = run_query(session, admin, {"entity": "users"})
    assert out["valid_values"] == ["issues", "executions"]


def test_rejects_unknown_dimension(session, admin):
    out = run_query(session, admin, {"group_by": ["galaxy"]})
    assert "galaxy" in out["error"]


def test_rejects_exec_only_dim_for_issues(session, admin):
    # scenario_type is issue-only; executions must reject it.
    out = run_query(session, admin, {"entity": "executions",
                                     "group_by": ["scenario_type"]})
    assert "error" in out


def test_rejects_more_than_two_dims(session, admin):
    out = run_query(session, admin, {"group_by": ["type", "status", "day"]})
    assert "error" in out


# ── issues ────────────────────────────────────────────────────────────


def test_total_row_when_no_group_by(session, admin):
    out = run_query(session, admin, {"year": 2026, "month": 6})
    rows = out["data"]["rows"]
    assert len(rows) == 1
    assert rows[0]["count"] == 5
    assert rows[0]["open_count"] == 3   # open(1,5) + in_progress(2)


def test_two_dim_cross_grouping_with_labels(session, admin):
    out = run_query(session, admin, {
        "group_by": ["job", "type"], "year": 2026, "month": 6})
    rows = out["data"]["rows"]
    by_key = {(r["job"], r["type"]): r for r in rows}
    assert by_key[("ns_daily", IssueType.JOB_FAILED)]["count"] == 4
    assert by_key[("ns_daily", IssueType.MMP_DRIFT)]["count"] == 1
    assert by_key[("ns_daily", IssueType.JOB_FAILED)]["job_id"] == 1


def test_has_scenario_filter(session, admin):
    session.query(Issue).filter(Issue.id == 3).update(
        {"selected_scenario_type": "triggered_but_failed",
         "selected_scenario_name": "rerun"})
    session.commit()
    out = run_query(session, admin, {
        "has_scenario": True, "year": 2026, "month": 6})
    assert out["data"]["rows"][0]["count"] == 1
    out2 = run_query(session, admin, {
        "has_scenario": False, "year": 2026, "month": 6})
    assert out2["data"]["rows"][0]["count"] == 4


def test_title_contains_filter(session, admin):
    out = run_query(session, admin, {
        "title_contains": "drifted", "year": 2026, "month": 6})
    assert out["data"]["rows"][0]["count"] == 1


def test_day_dim_zero_fills(session, admin):
    out = run_query(session, admin, {"group_by": ["day"], "year": 2026, "month": 6})
    by_day = {r["day"]: r["count"] for r in out["data"]["rows"]}
    assert by_day["2026-06-05"] == 1
    assert by_day["2026-06-08"] == 2
    assert by_day.get("2026-06-01") == 0


# ── executions (FP-corrected) ─────────────────────────────────────────


def test_execution_totals_fp_corrected(session, admin):
    out = run_query(session, admin, {
        "entity": "executions", "year": 2026, "month": 6})
    row = out["data"]["rows"][0]
    # 4 June runs; run-2 failed but dismissed as FP → 2 counted failures.
    assert row["runs"] == 4
    assert row["failures"] == 2
    assert row["failure_rate_percent"] == 50


def test_execution_grouping_by_job_carries_label(session, admin):
    out = run_query(session, admin, {
        "entity": "executions", "group_by": ["job"], "year": 2026, "month": 6})
    row = out["data"]["rows"][0]
    assert row["job"] == "ns_daily"
    assert row["job_id"] == 1


def test_execution_status_filter_failed(session, admin):
    out = run_query(session, admin, {
        "entity": "executions", "status": ["failed"], "year": 2026, "month": 6})
    row = out["data"]["rows"][0]
    assert row["runs"] == 3            # 3 raw failed runs scanned
    assert row["failures"] == 2        # one is the dismissed FP


# ── RBAC ──────────────────────────────────────────────────────────────


def test_non_member_sees_nothing(session):
    from core.models.user import User

    session.add(User(id=9, username="outsider", role=UserRole.REGULAR_USER))
    session.commit()
    outsider = CurrentUser(username="outsider", user_id=9, role=UserRole.REGULAR_USER)
    out = run_query(session, outsider, {"year": 2026, "month": 6})
    assert out["data"]["rows"] == [{"count": 0, "open_count": 0}]
    out2 = run_query(session, outsider, {"entity": "executions",
                                         "year": 2026, "month": 6})
    assert out2["data"]["rows"] == [{"runs": 0, "failures": 0,
                                     "failure_rate_percent": 0}]
