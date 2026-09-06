"""S3 — the SLA red line: deterministic cron/threshold recommendation.

Every recommended number must come from real run history, never the LLM. These
pin: the median-based computation, the "not enough samples → no advice" guard,
the age floor, the conservative-cron rule, the job_sla proposal shape
(commit_path=version_flow), and the commit dispatch into job_service.update.
"""
import math
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.models.entities  # noqa: F401 — register tables
from core.agent.sla_advisor import recommend_sla
from core.auth.jwt import CurrentUser
from core.models.database import Base
from core.models.entities import Job, JobExecution, Product, Project
from core.models.user import UserRole

NOW = datetime(2026, 6, 12, 0, 0)


def _admin():
    return CurrentUser(username="boss", user_id=1, role=UserRole.ADMIN)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_job(s, *, cron="0 0 * * *", preset=None, interval_min=60, n=10):
    s.add(Project(id=1, name="P", owner_id=1))
    s.add(Product(id=1, project_id=1, name="Prod"))
    s.add(Job(id=1, product_id=1, schedule_cron=cron, sla_preset=preset))
    # n successful runs spaced interval_min apart (status normalised "completed").
    for i in range(n):
        s.add(JobExecution(job_id=1, status="completed", cml_run_id=f"r{i}",
                           timestamp=NOW + timedelta(minutes=i * interval_min)))
    s.commit()


# ── recommend_sla computation ────────────────────────────────────────────


def test_threshold_from_real_cadence(factory):
    s = factory()
    _seed_job(s, cron="0 0 * * *", interval_min=60, n=10)  # daily cron, hourly reality
    rec = recommend_sla(s, 1)
    # observed median = 60; normal factor 1.5 → threshold 90.
    assert rec.observed_interval_minutes == 60
    assert rec.sample_run_count == 9
    assert rec.recommended_threshold_minutes == math.ceil(60 * 1.5)
    s.close()


def test_insufficient_samples_gives_no_advice(factory):
    s = factory()
    _seed_job(s, interval_min=60, n=3)  # only 2 gaps < min_samples(5)
    rec = recommend_sla(s, 1)
    assert rec.recommended_threshold_minutes is None
    assert rec.recommended_cron is None
    assert any("样本不足" in w for w in rec.warnings)
    s.close()


def test_age_floor_raises_threshold(factory):
    s = factory()
    _seed_job(s, interval_min=60, n=10)
    rec = recommend_sla(s, 1, this_alert_age_minutes=500)
    # ceil(60*1.5)=90 < 500 → floored at the alert age.
    assert rec.recommended_threshold_minutes >= 500
    s.close()


def test_conservative_cron_only_when_mapping_is_clean(factory):
    croniter = pytest.importorskip("croniter")  # noqa: F841
    s = factory()
    _seed_job(s, cron="0 0 * * *", interval_min=60, n=10)  # real cadence hourly
    rec = recommend_sla(s, 1)
    assert rec.recommended_cron == "0 * * * *"
    s.close()


# ── P5: SLA never makes it up (parametrized; hypothesis optional) ─────────


@pytest.mark.parametrize("interval,n", [(30, 8), (60, 10), (90, 7), (240, 6), (17, 9)])
def test_p5_recommendation_is_grounded(factory, interval, n):
    from core.agent.sla_advisor import _cron_is_valid

    s = factory()
    _seed_job(s, cron="0 0 * * *", preset=None, interval_min=interval, n=n)
    rec = recommend_sla(s, 1)
    # threshold: None, or exactly ceil(observed × factor) (no age passed).
    if rec.recommended_threshold_minutes is not None:
        assert rec.recommended_threshold_minutes == math.ceil(rec.observed_interval_minutes * 1.5)
    # cron: None, or a croniter-valid expression.
    assert rec.recommended_cron is None or _cron_is_valid(rec.recommended_cron)
    s.close()


# ── draft_job_sla proposal + commit dispatch ─────────────────────────────


def test_draft_job_sla_builds_version_flow_proposal(factory):
    from core.agent.write_tools import draft_job_sla
    s = factory()
    _seed_job(s, cron="0 0 * * *", interval_min=60, n=10)
    out = draft_job_sla(s, _admin(), job_id=1)
    assert out["kind"] == "job_sla"
    assert out["commit_path"] == "version_flow"
    # rationale must cite the deterministic method + sample count (no LLM number).
    rationale = out["changes"][0]["rationale"]
    assert "样本数=" in rationale and "median" in rationale
    # the threshold change targets the JobUpdate field name.
    assert any(c["field_path"] == "sla_custom_minutes" for c in out["changes"])
    s.close()


def test_draft_job_sla_no_advice_returns_note(factory):
    from core.agent.write_tools import draft_job_sla
    s = factory()
    _seed_job(s, interval_min=60, n=3)  # insufficient samples
    out = draft_job_sla(s, _admin(), job_id=1)
    assert "kind" not in out and "note" in out
    s.close()


def test_commit_job_sla_dispatches_to_job_service(monkeypatch):
    from api.routers import agent_actions
    from core.agent.write_schemas import FieldChange, WriteProposal

    captured = {}

    class _Job:
        id = 1

    def _fake_update(session, *, job_id, body, actor):
        captured["job_id"] = job_id
        captured["sla_custom_minutes"] = body.sla_custom_minutes
        captured["schedule_cron"] = body.schedule_cron
        return _Job()

    import core.services.job_service as job_service
    monkeypatch.setattr(job_service, "update", _fake_update)

    proposal = WriteProposal(
        kind="job_sla", entity_type="job", entity_id=1, title="x",
        changes=[
            FieldChange(field_path="sla_custom_minutes", label="t", old_value="2160", new_value="90", rationale="m"),
            FieldChange(field_path="schedule_cron", label="c", old_value="0 0 * * *", new_value="0 * * * *", rationale="m"),
        ],
        commit_path="version_flow",
    )
    result = agent_actions._commit_proposal(proposal, _admin(), session=None)
    assert result == {"entity": "job", "job_id": 1, "commit_path": "version_flow"}
    assert captured == {"job_id": 1, "sla_custom_minutes": 90, "schedule_cron": "0 * * * *"}
