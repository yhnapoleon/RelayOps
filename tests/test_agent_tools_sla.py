"""relayops_job_sla_config / relayops_job_schedule_adherence — deterministic SLA-threshold
and cron-adherence tools.

Every number is computed server-side (cron interpreted in SGT); these tests pin
the math so the chat agent can only relay it, never re-derive it.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.models.entities  # noqa: F401 — register tables on Base
from core.agent import tools
from core.auth.jwt import CurrentUser
from core.models.database import Base
from core.models.entities import Job, JobExecution, Product, Project
from core.models.user import User, UserRole


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
    s.add(Project(id=1, name="P", owner_id=1))
    s.add(Product(id=1, project_id=1, name="Prod"))
    # Job 1: daily 08:00 SGT (= 00:00 UTC), no preset → normal / 1.5
    s.add(Job(id=1, product_id=1, cml_job_name="daily8", schedule_cron="0 8 * * *"))
    # Job 2: same cron but strict preset → factor 1.0
    s.add(Job(id=2, product_id=1, cml_job_name="strict8", schedule_cron="0 8 * * *",
              sla_preset="strict"))
    # Job 3: no cron at all
    s.add(Job(id=3, product_id=1, cml_job_name="nocron"))
    # Job 1 runs: one exactly on schedule, one 30 min late.
    s.add(JobExecution(job_id=1, status="success", cml_run_id="r1",
                       timestamp=datetime(2026, 6, 5, 0, 0, 0)))    # 08:00 SGT exactly
    s.add(JobExecution(job_id=1, status="success", cml_run_id="r2",
                       timestamp=datetime(2026, 6, 6, 0, 30, 0)))   # 30 min late
    s.commit()


# ── relayops_job_sla_config ────────────────────────────────────────────────


def test_sla_config_normal_is_default(session, admin):
    d = tools.job_sla_config(session, admin, job_id=1)["data"]
    assert d["sla_preset"] == "normal"
    assert d["safety_factor"] == 1.5
    assert d["expected_interval_minutes"] == 1440          # daily
    assert d["stale_threshold_minutes"] == 2160            # ceil(1440 × 1.5)


def test_sla_config_strict_preset(session, admin):
    d = tools.job_sla_config(session, admin, job_id=2)["data"]
    assert d["sla_preset"] == "strict"
    assert d["safety_factor"] == 1.0
    assert d["stale_threshold_minutes"] == 1440


def test_sla_config_no_cron_threshold_is_none(session, admin):
    d = tools.job_sla_config(session, admin, job_id=3)["data"]
    assert d["expected_interval_minutes"] is None
    assert d["stale_threshold_minutes"] is None


def test_sla_config_missing_job_errors(session, admin):
    assert "error" in tools.job_sla_config(session, admin, job_id=999)


# ── relayops_job_schedule_adherence ────────────────────────────────────────


def test_adherence_signed_deviation_in_sgt(session, admin):
    d = tools.job_schedule_adherence(session, admin, job_id=1, days=3650)["data"]
    assert "SGT" in d["timezone"]
    devs = {r["deviation_minutes"] for r in d["runs"]}
    assert 0 in devs and 30 in devs            # on-time run + 30-min-late run
    assert d["max_abs_deviation_minutes"] == 30
    for r in d["runs"]:                         # cron is 08:00 SGT wall-clock
        assert r["scheduled_sgt"] and "08:00" in r["scheduled_sgt"]


def test_adherence_no_cron_returns_note(session, admin):
    d = tools.job_schedule_adherence(session, admin, job_id=3)["data"]
    assert "note" in d


def test_adherence_missing_job_errors(session, admin):
    assert "error" in tools.job_schedule_adherence(session, admin, job_id=999)
