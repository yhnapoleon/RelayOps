"""Agent tool layer — analytics-grade tools over an in-memory SQLite DB.

Real queries, real ``analytics_service`` math, deterministic seed data pinned
to June 2026 so period filters never depend on the wall clock.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.models.entities  # noqa: F401 — register every table on Base
from core.agent import tools
from core.agent.knowledge import HEALTH_SEVERITIES, HandlingState
from core.auth.jwt import CurrentUser
from core.models.constants import IssueStatus, IssueType
from core.models.database import Base
from core.models.entities import Issue, Job, JobExecution, Product, Project
from core.models.mmp_entities import MmpDriftSnapshot
from core.models.user import User, UserRole

JUNE = datetime(2026, 6, 5, 8, 0, 0)
MAY = datetime(2026, 5, 5, 8, 0, 0)


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


def _seed(s):
    s.add(User(id=1, username="boss", role=UserRole.ADMIN))
    s.add(Project(id=1, name="Inventory Scoring", owner_id=1))
    s.add(Product(id=1, project_id=1, name="NS Scoring"))
    s.add(Job(id=1, product_id=1, cml_job_name="ns_daily", control_m_job_name="NS_D01",
              schedule_cron="0 2 * * *", mmp_model_id="model-7"))

    # June executions: fail, fail(run-2 → dismissed as FP), success, fail
    for i, (run, status) in enumerate(
        [("run-1", "failed"), ("run-2", "failed"), ("run-3", "success"), ("run-4", "failed")]
    ):
        s.add(JobExecution(job_id=1, status=status, cml_run_id=run,
                           timestamp=JUNE + timedelta(days=i)))
    # May executions: all success
    for i in range(3):
        s.add(JobExecution(job_id=1, status="success", cml_run_id=f"may-{i}",
                           timestamp=MAY + timedelta(days=i)))

    s.add(Issue(id=1, type=IssueType.JOB_FAILED, status=IssueStatus.OPEN,
                title="ns_daily failed", product_id=1, job_id=1, created_by=1,
                created_at=JUNE, sla_deadline=JUNE + timedelta(hours=1)))
    s.add(Issue(id=2, type=IssueType.JOB_FAILED, status=IssueStatus.IN_PROGRESS,
                title="ns_daily failed again", product_id=1, job_id=1, created_by=1,
                assignee_id=1, created_at=JUNE + timedelta(days=1),
                action_summary_json={
                    "started_at": "2026-06-06T09:00:00",
                    "steps": [{"title": "查日志", "recorded_at": "2026-06-06T09:10:00"}],
                    "escalations": [{"target": "ops@example.com", "recorded_at": "2026-06-06T10:00:00"}],
                }))
    s.add(Issue(id=3, type=IssueType.JOB_FAILED, status=IssueStatus.RESOLVED,
                title="resolved one", product_id=1, job_id=1, created_by=1,
                created_at=JUNE + timedelta(days=2),
                resolved_at=JUNE + timedelta(days=2, minutes=30),
                sla_deadline=JUNE + timedelta(days=2, hours=1),
                resolution_description="rerun fixed it"))
    s.add(Issue(id=4, type=IssueType.JOB_FAILED, status=IssueStatus.FALSE_POSITIVE,
                title="dismissed", product_id=1, job_id=1, created_by=1,
                created_at=JUNE + timedelta(days=3), dedup_key="run-2",
                resolution_description="known blip"))
    s.add(Issue(id=5, type=IssueType.MMP_DRIFT, status=IssueStatus.OPEN,
                title="model drifted", product_id=1, job_id=1, created_by=1,
                created_at=JUNE + timedelta(days=3),
                external_url="https://mmp/project/7/models"))
    s.add(MmpDriftSnapshot(job_id=1, drifted=True, observed_at=JUNE + timedelta(days=3),
                           drift_details="pmetric drifted"))
    s.commit()


# ── enum validation ───────────────────────────────────────────────────


def test_list_issues_rejects_bad_status(session, admin):
    out = tools.list_issues(session, admin, status="opened")
    assert out[0]["error"].startswith("status")
    assert out[0]["valid_values"] == IssueStatus.ALL


def test_issue_stats_rejects_bad_type(session, admin):
    out = tools.issue_stats(session, admin, issue_type="job_explode")
    assert "error" in out and out["valid_values"] == IssueType.ALL


# ── handling state surfaced everywhere ────────────────────────────────


def test_list_issues_carries_handling_state(session, admin):
    rows = tools.list_issues(session, admin, days=0, limit=50)
    by_id = {r["issue_id"]: r for r in rows}
    assert by_id[1]["handling_state"] == HandlingState.UNCLAIMED
    assert by_id[2]["handling_state"] == HandlingState.ESCALATED_WAITING
    assert by_id[4]["handling_state"] == HandlingState.FALSE_POSITIVE


def test_issue_detail_timeline_is_chronological(session, admin):
    out = tools.issue_detail(session, admin, issue_id=2)
    assert out["handling_state"] == HandlingState.ESCALATED_WAITING
    assert "ops@example.com" in out["handling_detail"]
    events = [e["event"] for e in out["action_timeline"]]
    assert events == ["start_working", "record_step", "escalate"]
    assert out["related"]["job"]["cml_job_name"] == "ns_daily"


def test_issue_detail_denies_unknown_issue(session, admin):
    assert "error" in tools.issue_detail(session, admin, issue_id=999)


def test_issue_detail_mmp_issue_surfaces_live_binding(session, admin):
    # Issue 5 is MMP_DRIFT on job 1 → detail must hand the agent the MMP
    # binding + a note telling it to read approval_status etc. live from MMP,
    # so it never answers "not recorded" from the Ops record.
    out = tools.issue_detail(session, admin, issue_id=5)
    assert "mmp" in out
    assert out["mmp"]["model_name"] == "model-7"
    assert "mmp_live_model_raw" in out["mmp"]["note"]
    # Non-MMP issue has no mmp block.
    assert "mmp" not in tools.issue_detail(session, admin, issue_id=1)


# ── issue resolution playbook (deterministic scenario matching) ───────


@pytest.fixture
def resolution_session():
    """Own DB: a job with two runbook scenarios + issues of different types,
    so we can pin the issue→scenario matching that relayops_issue_resolution does."""
    from core.models.job_entities import JobFailureScenario

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(id=1, username="boss", role=UserRole.ADMIN))
    s.add(Project(id=1, name="NS", owner_id=1))
    s.add(Product(id=1, project_id=1, name="NS Scoring"))
    s.add(Job(id=11, product_id=1, cml_job_name="Inventory_Scoring_Model_GE",
              owner_contact="JamieRiver@example.com"))
    s.add(JobFailureScenario(
        job_id=11, scenario_type="triggered_but_failed", scenario_name="run failed",
        action_steps=["check logs", "rerun"], verification_steps=["run Check Scoring"],
        escalation_target="Control-M team"))
    s.add(JobFailureScenario(
        job_id=11, scenario_type="mmp_drift_detected", scenario_name="drift",
        action_steps=[], verification_steps=[], escalation_target=""))
    # The #68 reproduction: MMP run-pending-approval on the same job.
    s.add(Issue(id=68, type=IssueType.MMP_RUN_PENDING_APPROVAL, status=IssueStatus.OPEN,
                title="MMP run pending approval", product_id=1, job_id=11, created_by=1,
                created_at=JUNE, external_url="https://mmp/run/13073"))
    # A job_failed on the same job → should match triggered_but_failed.
    s.add(Issue(id=70, type=IssueType.JOB_FAILED, status=IssueStatus.OPEN,
                title="job failed", product_id=1, job_id=11, created_by=1, created_at=JUNE))
    s.commit()
    yield s
    s.close()


def test_resolution_pending_approval_has_no_runbook_match(resolution_session, admin):
    """#68 bug: must NOT attach an unrelated scenario; platform-workflow regime."""
    out = tools.issue_resolution(resolution_session, admin, issue_id=68)
    assert out["handling_mode"] == "platform_workflow"
    assert out["matched_scenarios"] == []          # the core fix — no guessed match
    assert "MMP" in out["handling_note"]
    assert out["external_url"] == "https://mmp/run/13073"


def test_resolution_job_failed_matches_scenario_and_contacts(resolution_session, admin):
    out = tools.issue_resolution(resolution_session, admin, issue_id=70)
    assert out["handling_mode"] == "runbook_scenario"
    matched_types = {s["scenario_type"] for s in out["matched_scenarios"]}
    assert "triggered_but_failed" in matched_types
    assert "mmp_drift_detected" not in matched_types   # not a hint for job_failed
    # Closed contact set = matched scenario escalation_target + owner_contact.
    assert out["allowed_contacts"] == ["Control-M team", "JamieRiver@example.com"]
    scen = next(s for s in out["matched_scenarios"] if s["scenario_type"] == "triggered_but_failed")
    assert scen["runbook_coverage"]["has_action_steps"] is True


def test_resolution_denies_unknown_issue(resolution_session, admin):
    assert "error" in tools.issue_resolution(resolution_session, admin, issue_id=999)


# ── issue stats ───────────────────────────────────────────────────────


def test_issue_stats_envelope_and_counts(session, admin):
    out = tools.issue_stats(session, admin, year=2026, month=6)
    assert out["meta"]["period"]["granularity"] == "month"
    assert out["meta"]["row_count"] == 5
    stats = out["data"]
    assert stats["total"] == 5
    assert stats["open_count"] == 2          # issues 1 and 5
    assert stats["false_positive_count"] == 1
    assert stats["false_positive_rate_percent"] == 20
    assert stats["sla_compliance_rate"] == 100  # the one resolved-with-SLA issue


def test_issue_stats_respects_period(session, admin):
    out = tools.issue_stats(session, admin, year=2026, month=5)
    assert out["data"]["total"] == 0


# ── product health (false-positive corrected) ─────────────────────────


def test_product_health_corrects_false_positive_run(session, admin):
    out = tools.product_health(session, admin, year=2026, month=6)
    item = out["data"]["items"][0]
    assert item["product_id"] == 1
    # 4 runs, 3 raw failures, run-2 dismissed → 2 counted failures (50%)
    assert item["job_runs_total"] == 4
    assert item["job_failures"] == 2
    assert item["failure_rate_percent"] == 50
    assert item["severity"] in HEALTH_SEVERITIES
    assert out["data"]["anomaly_rules"]["min_runs"] >= 1


def test_drilldown_daily_trend_covers_whole_month(session, admin):
    out = tools.product_health_drilldown(session, admin, product_id=1, year=2026, month=6)
    assert len(out["data"]["daily_trend"]) == 30
    assert out["data"]["summary"]["job_failures"] == 2
    job_row = out["data"]["jobs"][0]
    assert job_row["job_name"] == "NS_D01"


def test_drilldown_denies_unknown_product(session, admin):
    assert "error" in tools.product_health_drilldown(session, admin, product_id=99)


# ── job execution history ─────────────────────────────────────────────


def test_job_execution_history_summary(session, admin, monkeypatch):
    out = tools.job_execution_history(session, admin, job_id=1, days=36500)
    summary = out["data"]["summary"]
    assert summary["total_runs"] == 7        # 4 June + 3 May
    assert summary["failures"] == 2          # FP-corrected
    assert summary["failure_rate_percent"] == round(2 / 7 * 100)
    rows = out["data"]["executions"]
    assert rows[0]["at"] > rows[-1]["at"]    # newest first
    dismissed = next(r for r in rows if not r["is_failure"] and r["status"] == "failed")
    assert dismissed  # run-2 visible but not a failure


# ── MMP overview ──────────────────────────────────────────────────────


def test_mmp_overview_counts_and_drift(session, admin):
    out = tools.mmp_overview(session, admin)
    data = out["data"]
    assert data["open_issue_counts_by_type"] == {IssueType.MMP_DRIFT: 1}
    assert data["open_issues"][0]["external_url"].startswith("https://mmp/")
    drift = data["model_drift_status"][0]
    assert drift["mmp_model_id"] == "model-7" and drift["latest_drifted"] is True


# ── period comparison ─────────────────────────────────────────────────


def test_compare_periods_deltas(session, admin):
    out = tools.compare_periods(
        session, admin,
        a_year=2026, a_month=5, b_year=2026, b_month=6,
        scope="product", target_id=1,
    )
    deltas = out["data"]["deltas"]
    assert deltas["issue_total"] == {"a": 0, "b": 5, "delta": 5}
    assert deltas["failure_rate_percent"]["a"] == 0
    assert deltas["failure_rate_percent"]["b"] == 50
    assert deltas["failure_rate_percent"]["delta"] == 50


def test_compare_periods_requires_target_for_scoped(session, admin):
    assert "error" in tools.compare_periods(session, admin, scope="product")
    bad = tools.compare_periods(session, admin, scope="galaxy")
    assert bad["valid_values"] == ["all", "project", "product"]


# ── glossary tool ─────────────────────────────────────────────────────


def test_domain_glossary_returns_full_card(session, admin):
    out = tools.domain_glossary(session, admin)
    assert "mmp_pending_review" in out["glossary"]
    assert "anomaly rules" in out["glossary"]


# ── langchain wrapping exposes everything ─────────────────────────────


def test_build_langchain_tools_exposes_all(admin):
    pytest.importorskip("langchain_core")
    names = {t.name for t in tools.build_langchain_tools(admin)}
    assert names == {
        "relayops_projects_overview", "relayops_product_assets", "relayops_list_issues",
        "relayops_query", "relayops_issue_breakdown",
        "relayops_on_duty_now", "relayops_job_runbook", "relayops_app_runbook",
        "relayops_domain_glossary", "relayops_issue_detail", "relayops_issue_resolution", "relayops_issue_stats",
        "relayops_product_health", "relayops_product_health_drilldown",
        "relayops_job_execution_history", "relayops_job_sla_config", "relayops_job_schedule_adherence",
        "relayops_mmp_overview", "relayops_compare_periods",
        "relayops_search_resolutions", "relayops_render_chart", "relayops_nav_buttons",
        "cml_live_projects", "cml_live_jobs", "cml_live_job_runs",
        "cml_live_apps", "mmp_live_projects", "mmp_live_project_status",
        "mmp_live_model_raw", "cml_get_raw", "mmp_get_raw",
    }


# ── issue breakdown (single-dimension aggregation) ────────────────────


def test_issue_breakdown_by_job_resolves_names(session, admin):
    out = tools.issue_breakdown(session, admin, group_by="job", year=2026, month=6,
                                issue_type=IssueType.JOB_FAILED)
    rows = out["data"]["rows"]
    assert rows[0]["job_id"] == 1
    assert rows[0]["label"] == "ns_daily"
    assert rows[0]["count"] == 4             # issues 1-4 are job_failed on job 1
    assert rows[0]["open_count"] == 2        # open + in_progress


def test_issue_breakdown_by_day_zero_fills(session, admin):
    out = tools.issue_breakdown(session, admin, group_by="day", year=2026, month=6)
    rows = out["data"]["rows"]
    by_date = {r["label"]: r["count"] for r in rows}
    assert by_date["2026-06-05"] == 1
    assert by_date["2026-06-08"] == 2        # issues 4 and 5
    assert by_date.get("2026-06-04") == 0    # zero-filled quiet day
    assert rows == sorted(rows, key=lambda r: r["label"])


def test_issue_breakdown_rejects_bad_dimension(session, admin):
    out = tools.issue_breakdown(session, admin, group_by="galaxy")
    assert out["valid_values"] == tools._BREAKDOWN_DIMS


def test_list_issues_filters_by_job_and_carries_scenario(session, admin):
    rows = tools.list_issues(session, admin, days=0, job_id=1, limit=50)
    assert {r["issue_id"] for r in rows} == {1, 2, 3, 4, 5}
    assert all("selected_scenario_type" in r for r in rows)


def test_product_assets_carries_per_asset_stats(session, admin):
    out = tools.product_assets(session, admin, product_id=1)
    job = out["jobs"][0]
    assert job["stats"]["open_issues"] == 3      # issues 1, 2 (open/in_progress) + 5
    assert job["stats"]["runs_30d"] >= 0         # shape present even off-window
    assert "last_run_status" in job["stats"]
