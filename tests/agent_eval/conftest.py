"""Realistic seed + fixtures for agent behavioral eval.

Job shapes mirror the failure transcripts so the eval exercises exactly the
cases that broke:
  * job 1 — weekday-daily forecast, runs land a bit off schedule (all aligned);
  * job 5 — weekly Friday job with ONE extra mid-week run (the run that the old
    code reported as a 71h "early" deviation instead of an extra/unscheduled run).
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.models.entities  # noqa: F401 — register tables on Base
from core.auth.jwt import CurrentUser
from core.models.database import Base
from core.models.entities import Job, JobExecution, Product, Project
from core.models.user import User, UserRole


def seed_eval_db(s):
    s.add(User(id=1, username="boss", role=UserRole.ADMIN))
    s.add(Project(id=1, name="FORECAST", owner_id=1))
    s.add(Product(id=1, project_id=1, name="Forecast"))

    # Job 1: weekday-daily (Tue-Fri 04:30 SGT = 20:30 UTC prev day). Runs land
    # within an hour of schedule → all "aligned".
    s.add(Job(id=1, product_id=1, cml_job_name="Trigger Forecast for SG",
              control_m_job_name="FCAST_SG", schedule_cron="30 4 * * 2-5"))
    for ts in ["2026-06-15 20:23", "2026-06-16 21:25",
               "2026-06-17 20:09", "2026-06-18 20:54"]:
        s.add(JobExecution(job_id=1, status="completed", cml_run_id="sg-" + ts,
                           timestamp=datetime.fromisoformat(ts)))

    # Job 5: weekly Friday 14:00 SGT (= 06:00 UTC) + ONE extra mid-week run.
    s.add(Job(id=5, product_id=1, cml_job_name="Trigger Recommend",
              control_m_job_name="RECO", schedule_cron="0 14 * * 5"))
    for ts in ["2026-05-22 05:13", "2026-05-29 05:11",
               "2026-06-05 05:39", "2026-06-12 05:08"]:  # ~50 min early, aligned
        s.add(JobExecution(job_id=5, status="completed", cml_run_id="reco-" + ts,
                           timestamp=datetime.fromisoformat(ts)))
    # Extra run on a NON-Friday — must be classified "extra", never a 71h deviation.
    s.add(JobExecution(job_id=5, status="completed", cml_run_id="reco-extra",
                       timestamp=datetime.fromisoformat("2026-05-26 06:48")))
    s.commit()


@pytest.fixture
def eval_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    seed_eval_db(s)
    yield s
    s.close()


@pytest.fixture
def admin() -> CurrentUser:
    return CurrentUser(username="boss", user_id=1, role=UserRole.ADMIN)
