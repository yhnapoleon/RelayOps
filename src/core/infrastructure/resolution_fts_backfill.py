"""Idempotent backfill for the agent's FTS5 retrieval sidecar.

Rebuilds two indexes in ``core/agent/retrieval.py``'s SQLite file:

- ``issue_fts``: every resolved/closed/false-positive Issue that carries a
  resolution_description (the knowledge that "躺在数据库里没人复用");
- ``scenario_fts``: all job-failure and app-recovery runbook scenarios.

Safe to run on every startup — indexing is delete+insert per issue and a full
replace for scenarios. Incremental upkeep happens in ``issue_service.run_action``
(resolve / false-positive hook); this backfill catches auto-closed issues and
anything written before the hook existed.
"""

from __future__ import annotations

from core.logging import get_logger
from core.models.database import get_db
from core.models.entities import (
    ApplicationRecoveryScenario,
    Issue,
    IssueStatus,
    JobFailureScenario,
)

logger = get_logger(__name__)

_TERMINAL = [IssueStatus.RESOLVED, IssueStatus.CLOSED, IssueStatus.FALSE_POSITIVE]


def _steps_text(*lists) -> str:
    parts = []
    for v in lists:
        if isinstance(v, list):
            parts.extend(str(x) for x in v)
    return "\n".join(parts)


def run_backfill(db=None) -> dict:
    from core.agent import retrieval

    db = db or get_db()
    session = db.get_session()
    indexed_issues = 0
    try:
        conn = retrieval._connect()
        if conn is None:
            logger.warning("resolution FTS backfill skipped — FTS5 unavailable")
            return {"issues": 0, "scenarios": 0, "skipped": True}
        try:
            issues = (
                session.query(Issue)
                .filter(
                    Issue.status.in_(_TERMINAL),
                    Issue.resolution_description.isnot(None),
                    Issue.resolution_description != "",
                )
                .all()
            )
            for issue in issues:
                if retrieval.index_issue(issue, conn=conn):
                    indexed_issues += 1

            scenario_rows = []
            for s in session.query(JobFailureScenario).all():
                scenario_rows.append((
                    s.id, "job", s.job_id, s.scenario_type, s.scenario_name or "",
                    s.condition_description or "",
                    _steps_text(s.diagnostic_steps, s.action_steps, s.verification_steps),
                    s.escalation_target or "",
                ))
            for s in session.query(ApplicationRecoveryScenario).all():
                scenario_rows.append((
                    s.id, "app", s.application_id, s.scenario_type, s.scenario_name or "",
                    s.condition_description or "",
                    _steps_text(s.action_steps, s.verification_steps),
                    s.escalation_target or "",
                ))
            indexed_scenarios = retrieval.index_scenarios(scenario_rows, conn=conn)
        finally:
            conn.close()
    finally:
        session.close()

    logger.info("resolution FTS backfill: {} issues, {} scenarios", indexed_issues, indexed_scenarios)
    return {"issues": indexed_issues, "scenarios": indexed_scenarios, "skipped": False}


if __name__ == "__main__":
    run_backfill()
