"""Artifact channel — dataset registry, spec validation, SSE ordering.

Datasets resolve through the RBAC-scoped tool layer over the same seeded
SQLite DB as test_agent_tools; the chat test pins that artifacts stream as
their own SSE events and the model only ever sees the confirmation.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.models.entities  # noqa: F401
from core.agent import artifacts
from core.auth.jwt import CurrentUser
from core.models.database import Base
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


# ── registry & validation ─────────────────────────────────────────────


def test_unknown_dataset_lists_valid_values(session, admin):
    artifact, confirmation = artifacts.render(session, admin, dataset="nope")
    assert artifact is None
    assert "product_failure_trend" in confirmation["valid_values"]


def test_failure_trend_line_chart(session, admin):
    artifact, confirmation = artifacts.render(
        session, admin, dataset="product_failure_trend",
        params={"product_id": 1, "year": 2026, "month": 6})
    assert artifact["kind"] == "chart"
    assert artifact["chart"]["type"] == "line"
    assert artifact["chart"]["x_key"] == "date"
    assert len(artifact["rows"]) == 30
    assert confirmation["row_count"] == 30
    assert "rows" not in confirmation  # data never enters model context
    assert confirmation["title"] == artifact["title"]


def test_issue_type_distribution_pie(session, admin):
    artifact, _ = artifacts.render(
        session, admin, dataset="issue_type_distribution",
        params={"year": 2026, "month": 6})
    assert artifact["chart"]["type"] == "pie"
    counts = {r["type"]: r["count"] for r in artifact["rows"]}
    assert counts == {"job_failed": 4, "mmp_drift": 1}


def test_status_distribution_splits_false_positive(session, admin):
    artifact, _ = artifacts.render(
        session, admin, dataset="issue_status_distribution",
        params={"year": 2026, "month": 6})
    rows = {r["status"]: r["count"] for r in artifact["rows"]}
    assert rows["open"] == 2 and rows["false_positive"] == 1


def test_chart_type_override_must_be_allowed(session, admin):
    artifact, confirmation = artifacts.render(
        session, admin, dataset="product_failure_trend",
        params={"product_id": 1, "year": 2026, "month": 6}, chart_type="pie")
    assert artifact is None
    assert "pie" not in confirmation["valid_values"]

    artifact, _ = artifacts.render(
        session, admin, dataset="product_failure_trend",
        params={"product_id": 1, "year": 2026, "month": 6}, chart_type="table")
    assert artifact["kind"] == "table" and artifact["chart"] is None


def test_dataset_error_propagates(session, admin):
    artifact, confirmation = artifacts.render(
        session, admin, dataset="product_failure_trend", params={"product_id": 999})
    assert artifact is None and "error" in confirmation


def test_period_comparison_table(session, admin):
    artifact, _ = artifacts.render(
        session, admin, dataset="period_comparison",
        params={"a_year": 2026, "a_month": 5, "b_year": 2026, "b_month": 6,
                "scope": "product", "target_id": 1})
    assert artifact["kind"] == "table"
    metrics = {r["metric"]: r for r in artifact["rows"]}
    assert metrics["Failure rate %"]["delta"] == 50


def test_issue_breakdown_day_line_chart(session, admin):
    artifact, confirmation = artifacts.render(
        session, admin, dataset="issue_breakdown",
        params={"group_by": "day", "year": 2026, "month": 6})
    assert artifact["kind"] == "chart"
    assert artifact["chart"]["type"] == "line"
    by_date = {r["label"]: r["count"] for r in artifact["rows"]}
    assert by_date["2026-06-05"] == 1
    assert by_date["2026-06-08"] == 2
    assert by_date.get("2026-06-01") == 0       # zero-filled quiet day
    assert "Daily Issue count" in artifact["title"]
    assert "rows" not in confirmation


def test_issue_breakdown_by_job_bar_chart(session, admin):
    artifact, _ = artifacts.render(
        session, admin, dataset="issue_breakdown",
        params={"group_by": "job", "year": 2026, "month": 6,
                "issue_type": "job_failed"})
    assert artifact["chart"]["type"] == "bar"
    assert artifact["rows"][0]["label"] == "ns_daily"
    assert artifact["rows"][0]["count"] == 4


def test_sla_trend_walks_months_backwards(session, admin):
    artifact, _ = artifacts.render(
        session, admin, dataset="sla_compliance_trend",
        params={"year": 2026, "month": 6, "months": 3})
    months = [r["month"] for r in artifact["rows"]]
    assert months == ["2026-04", "2026-05", "2026-06"]


def test_spec_rejects_rows_missing_keys():
    art = artifacts.Artifact(
        id="x", kind="chart", title="t",
        chart=artifacts.ChartSpec(type="line", x_key="date",
                                  series=[artifacts.ChartSeries(key="value")]),
        rows=[{"date": "2026-06-01"}],  # no "value"
    )
    with pytest.raises(ValueError):
        artifacts._validate_rows_against_spec(art)


# ── nav (quick-jump) buttons ──────────────────────────────────────────


def test_nav_buttons_resolve_labels_and_parent_project(session, admin):
    artifact, confirmation = artifacts.render_nav(
        session, admin, project_ids=[1], product_ids=[1])
    assert artifact["kind"] == "nav"
    by_target = {(b["target"], b["id"]): b for b in artifact["buttons"]}
    assert by_target[("project", 1)]["label"] == "Inventory Scoring"
    product_btn = by_target[("product", 1)]
    assert product_btn["label"] == "NS Scoring"
    assert product_btn["project_id"] == 1  # frontend needs the parent id
    assert confirmation["skipped"] == []


def test_nav_buttons_skip_unknown_ids(session, admin):
    artifact, confirmation = artifacts.render_nav(
        session, admin, project_ids=[1, 999], product_ids=[888])
    assert len(artifact["buttons"]) == 1
    assert set(confirmation["skipped"]) == {"project:999", "product:888"}


def test_nav_buttons_rbac_blocks_invisible(session):
    from core.models.user import UserRole

    outsider = CurrentUser(username="stranger", user_id=99, role=UserRole.REGULAR_USER)
    artifact, confirmation = artifacts.render_nav(
        session, outsider, project_ids=[1], product_ids=[1])
    assert artifact is None
    assert set(confirmation["skipped"]) == {"project:1", "product:1"}


def test_nav_buttons_require_some_id(session, admin):
    artifact, confirmation = artifacts.render_nav(session, admin)
    assert artifact is None and "error" in confirmation


def test_nav_buttons_dedupe(session, admin):
    artifact, _ = artifacts.render_nav(session, admin, project_ids=[1, 1, 1])
    assert len(artifact["buttons"]) == 1


def test_nav_artifact_schema_requires_buttons():
    with pytest.raises(Exception):
        artifacts.Artifact(id="x", kind="nav", title="t", buttons=[])


# ── SSE ordering through the chat loop ────────────────────────────────


def test_chat_streams_artifact_events(monkeypatch):
    pytest.importorskip("langgraph")
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool

    import core.agent.tools as tools_module
    from core.agent.chat import run_turn
    from test_chat_agent import ScriptedModel

    captured: dict = {}

    def _fake_build(actor, artifact_sink=None):
        captured["sink"] = artifact_sink

        @tool
        def fake_chart() -> dict:
            """Fake chart tool."""
            artifact_sink.append({"id": "a1", "kind": "chart", "title": "趋势",
                                  "rows": [{"date": "d", "v": 1}]})
            return {"artifact_id": "a1", "row_count": 1}

        return [fake_chart]

    monkeypatch.setattr(tools_module, "build_langchain_tools", _fake_build)
    actor = CurrentUser(username="t", user_id=9, role="relayops_member")
    model = ScriptedModel(script=[
        AIMessage(content="", tool_calls=[{"name": "fake_chart", "args": {}, "id": "c1"}]),
        AIMessage(content="见图《趋势》。"),
    ])
    events = list(run_turn(actor, "画个图", thread_id=f"art-{datetime.utcnow().timestamp()}", model=model))
    kinds = [e["event"] for e in events]
    assert "artifact" in kinds
    assert kinds.index("tool_result") < kinds.index("artifact") < kinds.index("answer")
    artifact_event = next(e for e in events if e["event"] == "artifact")
    assert artifact_event["data"]["id"] == "a1"
    assert artifact_event["data"]["rows"] == [{"date": "d", "v": 1}]
