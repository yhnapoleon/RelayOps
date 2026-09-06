"""
One-time (idempotent) cleanup for the disabled MMP concern signals.

MMP exposes several ``attention_required`` flags. Ops monitors three of them
(drift, run-pending-approval, run-pending-user-review); the remaining two —
fairness risk and unapproved-experiment-run — are NOT auto-detected and never
create Issues. This backfill removes any data those two disabled signals left
behind so the platform reflects the policy:

- Hard-delete every Issue of type ``mmp_fairness_risk`` /
  ``mmp_unapproved_exp_run`` (and the in-app Notifications that pointed at
  those Issues — they carry no FK so they must be cleaned up explicitly).
- Delete every ``JobFailureScenario`` row of those two scenario types so they
  disappear from existing Job runbooks. The scenario *types* remain selectable
  in the UI; we only clear the auto-created rows.

NOTE: ``mmp_run_pending_approval`` is intentionally NOT in the disabled set —
it (and ``mmp_pending_review``) are active issue types again, so their rows
must be preserved across restarts.

Safe to run on every startup: it only ever deletes rows matching the disabled
types, so a second run finds nothing and is a no-op.
"""
from __future__ import annotations

from typing import Dict

from core.models.database import get_db
from core.models.entities import (
    Issue,
    IssueType,
    JobFailureScenario,
    JobFailureScenarioType,
    Notification,
)
from core.logging import get_logger

logger = get_logger(__name__)

# MMP concern signals that stay disabled (never create Issues). Pending
# approval / pending review are deliberately excluded — they are active again.
_DISABLED_ISSUE_TYPES = [
    IssueType.MMP_FAIRNESS_RISK,
    IssueType.MMP_UNAPPROVED_EXP_RUN,
]
_DISABLED_SCENARIO_TYPES = [
    JobFailureScenarioType.MMP_FAIRNESS_RISK,
    JobFailureScenarioType.MMP_UNAPPROVED_EXP_RUN,
]


def cleanup_disabled_mmp_concerns() -> Dict[str, int]:
    """Delete Issues, their Notifications, and Job scenarios for the three
    retired MMP concern signals. Idempotent — safe to run repeatedly.
    """
    db = get_db()
    session = db.get_session()
    stats = {
        "issues_deleted": 0,
        "notifications_deleted": 0,
        "scenarios_deleted": 0,
    }
    try:
        issue_ids = [
            row[0]
            for row in session.query(Issue.id)
            .filter(Issue.type.in_(_DISABLED_ISSUE_TYPES))
            .all()
        ]

        if issue_ids:
            # Notifications reference issues by (related_entity_type,
            # related_entity_id) with no FK/cascade, so remove them first.
            stats["notifications_deleted"] = (
                session.query(Notification)
                .filter(
                    Notification.related_entity_type == "issue",
                    Notification.related_entity_id.in_(issue_ids),
                )
                .delete(synchronize_session=False)
            )
            stats["issues_deleted"] = (
                session.query(Issue)
                .filter(Issue.id.in_(issue_ids))
                .delete(synchronize_session=False)
            )

        stats["scenarios_deleted"] = (
            session.query(JobFailureScenario)
            .filter(JobFailureScenario.scenario_type.in_(_DISABLED_SCENARIO_TYPES))
            .delete(synchronize_session=False)
        )

        session.commit()
        if any(stats.values()):
            logger.info(
                "MMP concern cleanup complete: issues_deleted={} "
                "notifications_deleted={} scenarios_deleted={}",
                stats["issues_deleted"],
                stats["notifications_deleted"],
                stats["scenarios_deleted"],
            )
        return stats
    except Exception:
        session.rollback()
        logger.opt(exception=True).warning(
            "MMP concern cleanup failed; continuing startup"
        )
        return stats
    finally:
        session.close()
