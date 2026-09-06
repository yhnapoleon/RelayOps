"""Issue diagnosis — staged pipeline: triage routing, evidence budget,
programmatic self-check (retry then enforce), SSE event contract."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.models.entities  # noqa: F401
from core.agent import diagnose as dx
from core.agent import diagnose_graph as dg
from core.agent.diagnose import (
    DiagnosisReport,
    EmailDraft,
    ISSUE_SCENARIO_HINTS,
    RootCause,
    build_prompt,
    run_diagnose,
)
from core.auth.jwt import CurrentUser
from core.models.database import Base
from core.models.entities import IssueType, JobFailureScenario
from core.models.user import UserRole

from test_agent_tools import _seed


@pytest.fixture
def actor() -> CurrentUser:
    return CurrentUser(username="boss", user_id=1, role=UserRole.ADMIN)


@pytest.fixture
def db(monkeypatch):
    """In-memory DB + get_db patch so every pipeline node sees the seed."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    seed_session = factory()
    _seed(seed_session)
    seed_session.add(JobFailureScenario(
        job_id=1, scenario_type="triggered_but_failed", scenario_name="Rerun after upstream",
        condition_description="job fails with upstream table missing",
        action_steps=["check upstream table", "rerun ns_daily"],
        verification_steps=["status becomes success"],
        escalation_target="ops-team@example.com",
    ))
    seed_session.commit()
    seed_session.close()

    class _Db:
        def get_session(self):
            return factory()

    monkeypatch.setattr(dg, "get_db", lambda: _Db())
    return factory


class _FakeStructured:
    def __init__(self, reports):
        self.reports = list(reports)
        self.calls = 0

    def invoke(self, messages):
        report = self.reports[min(self.calls, len(self.reports) - 1)]
        self.calls += 1
        return report


class _FakeModel:
    def __init__(self, reports):
        self.structured = _FakeStructured(reports)

    def with_structured_output(self, schema):
        return self.structured


def _good_report() -> DiagnosisReport:
    return DiagnosisReport(
        summary="ns_daily 失败，疑似上游表未就绪。",
        root_causes=[RootCause(hypothesis="上游未就绪", confidence="high",
                               evidence="cml job ns_daily run-1 failed")],
        recommended_steps=["按 runbook 检查上游表", "重跑 ns_daily"],
        escalation_needed=True,
        escalation_target="ops-team@example.com",
        email_draft=EmailDraft(to="ops-team@example.com", subject="[Ops] ns_daily failed", body="..."),
    )


def _bad_report() -> DiagnosisReport:
    return DiagnosisReport(
        summary="编造的报告",
        root_causes=[RootCause(hypothesis="幽灵 job 失败", confidence="high",
                               evidence="ghost_job_xyz crashed at #99999")],
        escalation_needed=True,
        escalation_target="fake@example.invalid",
        email_draft=EmailDraft(to="fake@example.invalid", subject="x", body="y"),
    )


# ── triage decision tree ──────────────────────────────────────────────


def test_route_branch_matrix():
    assert dg.route_branch(IssueType.JOB_FAILED, True, False) == "job"
    assert dg.route_branch(IssueType.APP_OFFLINE, False, True) == "app"
    assert dg.route_branch(IssueType.MMP_DRIFT, True, False) == "mmp"
    assert dg.route_branch(IssueType.HANDOVER_REVIEW, False, False) == "governance"
    assert dg.route_branch(IssueType.JOB_FAILED, False, False) == "plain"


def test_triage_checklist_mentions_branch_evidence():
    state = {"issue_type": IssueType.JOB_FAILED, "job_id": 1, "app_id": None}
    out = dg.triage(state)
    assert out["branch"] == "job"
    assert any("CML" in c for c in out["checklist"])
    assert any("历史相似" in c for c in out["checklist"])


# ── evidence budget ───────────────────────────────────────────────────


def test_budget_drops_unmatched_scenarios_first():
    big = "x" * 4000
    state = {"bundle": {"scenarios": [
        {"matched": True, "scenario_name": "keep", "condition": big},
        {"matched": False, "scenario_name": "drop1", "condition": big},
        {"matched": False, "scenario_name": "drop2", "condition": big},
    ]}}
    out = dg.apply_budget(state)
    names = [s["scenario_name"] for s in out["bundle"]["scenarios"]]
    assert "keep" in names
    assert out["bundle"]["_truncated_sections"] == ["scenarios"]


def test_budget_keeps_newest_history():
    state = {"bundle": {"similar_resolved_issues": [
        {"issue_id": i, "resolution": "y" * 3000} for i in range(10)
    ]}}
    out = dg.apply_budget(state)
    kept = [r["issue_id"] for r in out["bundle"]["similar_resolved_issues"]]
    assert kept == sorted(kept) and kept[0] == 0  # tail (oldest) trimmed


# ── self check ────────────────────────────────────────────────────────


def test_self_check_passes_clean_report():
    state = {
        "report": _good_report(),
        "bundle": {"scenarios": [{"escalation_target": "ops-team@example.com"}],
                   "job": {"owner_contact": "", "cml_job_name": "ns_daily"},
                   "issue": {"id": 1, "title": "ns_daily run-1 failed"}},
    }
    out = dg.self_check(state)
    assert out["needs_retry"] is False
    assert out["report"].escalation_needed is True
    assert out.get("warnings", []) == []


def test_self_check_requests_one_retry_then_enforces():
    bundle = {"scenarios": [{"escalation_target": "ops-team@example.com"}], "issue": {"id": 1}}
    state = {"report": _bad_report(), "bundle": bundle}
    out = dg.self_check(state)
    assert out["needs_retry"] is True and out["retried"] is True
    assert any("escalation_target" in v for v in out["violations"])

    # Second pass with the same bad report → strip in code.
    out["report"] = _bad_report()
    out = dg.self_check(out)
    assert out["needs_retry"] is False
    report = out["report"]
    assert report.escalation_needed is False
    assert report.escalation_target == "" and report.email_draft is None
    assert report.root_causes == []  # ungrounded ghost_job_xyz dropped
    assert out["warnings"]


def test_evidence_grounding_keeps_pure_prose():
    assert dg._evidence_grounded("纯中文描述没有可验证实体", "{}") is True
    assert dg._evidence_grounded("ns_daily failed", '{"job": "ns_daily"}') is True
    assert dg._evidence_grounded("ghost_job_xyz", '{"job": "ns_daily"}') is False


# ── full pipeline over the seeded DB ──────────────────────────────────


def test_pipeline_end_to_end_with_retry(db, actor):
    model = _FakeModel([_bad_report(), _good_report()])
    steps = list(dg.run_pipeline(actor, 1, model=model))
    stages = [s["stage"] for s in steps if "stage" in s]
    assert stages[0] == "collecting"
    assert stages.count("analyzing") == 2  # initial + self-check retry
    payload = steps[-1]["payload"]
    assert model.structured.calls == 2
    assert payload["escalation_target"] == "ops-team@example.com"
    assert payload["handling_state"] == "unclaimed"  # issue 1: open, no assignee
    assert any("CML" in c for c in payload["evidence_checklist"])
    assert payload["warnings"] == []
    # Embedded artifacts: job branch ships the execution timeline table.
    titles = [a["title"] for a in payload["artifacts"]]
    assert any("执行时间线" in t for t in titles)
    timeline = next(a for a in payload["artifacts"] if "执行时间线" in a["title"])
    assert timeline["kind"] == "table" and len(timeline["rows"]) == 7


def test_pipeline_collects_job_branch_evidence(db, actor):
    model = _FakeModel([_good_report()])
    state = {"actor": actor, "issue_id": 1, "model": model}
    state = dg.collect_base(state)
    state = dg.triage(state)
    assert state["branch"] == "job"
    state = dg.enrich_job(state)
    assert len(state["bundle"]["recent_executions"]) == 7  # all seeded runs, newest first
    assert state["bundle"]["scenarios"][0]["matched"] is True
    state = dg.retrieve_history(state)
    assert "similar_resolved_issues" in state["bundle"]


def test_run_diagnose_event_sequence(db, actor, monkeypatch):
    monkeypatch.setattr(dx, "_persist_run", lambda *a, **k: None)
    model = _FakeModel([_good_report()])
    events = list(run_diagnose(actor, 1, model=model))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "stage" and kinds[-1] == "done"
    assert kinds[-2] == "report"
    stage_values = [e["data"]["stage"] for e in events if e["event"] == "stage"]
    assert stage_values[0] == "collecting" and "analyzing" in stage_values
    report = next(e for e in events if e["event"] == "report")["data"]
    assert report["email_draft"]["subject"] == "[Ops] ns_daily failed"
    assert report["evidence_checklist"]


def test_run_diagnose_failure_yields_error_event(actor, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("llm down")
        yield  # pragma: no cover

    monkeypatch.setattr(dg, "run_pipeline", _boom)
    events = list(run_diagnose(actor, 7))
    kinds = [e["event"] for e in events]
    assert "report" not in kinds
    assert "error" in kinds and kinds[-1] == "done"
    assert "llm down" in next(e for e in events if e["event"] == "error")["data"]["message"]


# ── prompt assembly ───────────────────────────────────────────────────


def test_build_prompt_truncates_giant_bundles():
    bundle = {"noise": "x" * 100_000}
    prompt = build_prompt(bundle)
    assert len(prompt) < 35_000
    assert "(truncated)" in prompt


def test_build_prompt_carries_checklist_and_violations():
    prompt = build_prompt({"issue": {}}, checklist=["查执行历史"],
                          violations=["上次编造了联系人"])
    assert "查执行历史" in prompt
    assert "上次编造了联系人" in prompt


def test_scenario_hints_only_use_known_issue_types():
    known = {v for k, v in vars(IssueType).items() if isinstance(v, str) and not k.startswith("_")}
    assert set(ISSUE_SCENARIO_HINTS.keys()) <= known


def test_report_schema_roundtrip():
    r = _good_report()
    assert DiagnosisReport.model_validate(r.model_dump()) == r
