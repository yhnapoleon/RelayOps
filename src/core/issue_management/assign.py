"""
AssignModule — thin facade over Issue Engine.

Responsibilities:
- Convert AnomalyEvent into create_issue() calls with correct IssueType mapping
- Periodically check for SLA overdue issues and send notifications
- Send duty-start notifications to on-duty assignees
"""

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from core.services.audit_service import log_audit, resolve_audit_user_id, serialize_notification
from core.checker.base import AnomalyEvent, AnomalyType
from core.config import get_config
from core.models.database import Database, get_db
from core.models.user import User
from core.models.entities import (
    Issue,
    IssueStatus,
    IssueType,
    Notification,
    Schedule,
)
from core.issue_management.issue_engine import create_issue, get_fallback_admin
from core.logging import get_logger

logger = get_logger(__name__)


# Mapping from AnomalyType enum to IssueType string constants
_ANOMALY_TO_ISSUE_TYPE: dict[AnomalyType, str] = {
    AnomalyType.JOB_FAILED: IssueType.JOB_FAILED,
    AnomalyType.JOB_STALE: IssueType.JOB_STALE,
    AnomalyType.JOB_NOT_TRIGGERED: IssueType.JOB_NOT_TRIGGERED,
    AnomalyType.APP_OFFLINE: IssueType.APP_OFFLINE,
    AnomalyType.MMP_DRIFT: IssueType.MMP_DRIFT,
    AnomalyType.MMP_FAIRNESS_RISK: IssueType.MMP_FAIRNESS_RISK,
    AnomalyType.MMP_RUN_PENDING_APPROVAL: IssueType.MMP_RUN_PENDING_APPROVAL,
    AnomalyType.MMP_PENDING_REVIEW: IssueType.MMP_PENDING_REVIEW,
    AnomalyType.MMP_UNAPPROVED_EXP_RUN: IssueType.MMP_UNAPPROVED_EXP_RUN,
}


def _get_db() -> Database:
    return get_db()


class AssignModule:
    """
    Issue Engine facade. Converts AnomalyEvents into Issues and manages
    SLA overdue / duty-start notifications.
    """

    def __init__(self) -> None:
        # In-memory deduplication for SLA overdue notifications (per-process)
        self._notified_overdue: set[int] = set()

    def handle_anomaly(
        self,
        anomaly_event: AnomalyEvent,
        session: Optional[Session] = None,
    ) -> Optional[Issue]:
        """
        Convert an AnomalyEvent into an Issue via issue_engine.create_issue().

        When `session` is provided, the Issue is created on that session and
        the caller owns the transaction (commit/rollback) so that checker
        writes and Issue creation land atomically.

        The Issue Engine already handles:
        - Duplicate detection (has_open_issue)
        - Assignee resolution (resolve_assignee)
        - SLA deadline calculation (calculate_sla_deadline)
        - Notifications to assignee and admins
        - Audit logging

        Returns:
            The created Issue, or None if duplicate/skipped.
        """
        issue_type = _ANOMALY_TO_ISSUE_TYPE.get(anomaly_event.anomaly_type)
        if issue_type is None:
            logger.warning(
                "Unknown anomaly type, cannot map to IssueType: {}",
                anomaly_event.anomaly_type,
            )
            return None

        # Some monitoring issues (MMP pending-approval / pending-review) are
        # actioned on an external platform; the checker passes the deep link
        # via the event metadata so the My Actions card can offer a jump-out.
        external_url = (anomaly_event.metadata or {}).get("external_url") or None
        # MMP anomalies carry the triggering production run id so the issue can
        # later trace back to that exact run in the live MMP body.
        mmp_run_id = (anomaly_event.metadata or {}).get("latest_run_id")

        return create_issue(
            issue_type=issue_type,
            title=anomaly_event.title,
            description=anomaly_event.description,
            product_id=anomaly_event.product_id,
            job_id=anomaly_event.job_id,
            app_id=anomaly_event.app_id,
            dedup_key=anomaly_event.dedup_key,
            external_url=external_url,
            mmp_run_id=mmp_run_id if isinstance(mmp_run_id, int) else None,
            session=session,
        )

    def check_sla_overdue(self) -> None:
        """
        Check all open/in-progress Issues for SLA deadline violations.

        For each overdue Issue:
        - Notify the assignee and all platform admins (once per issue)
        - Uses _notified_overdue set for in-process deduplication
        - Uses DB Notification records for multi-worker deduplication
        """
        db = _get_db()
        session = db.get_session()
        try:
            now = datetime.utcnow()
            overdue_issues = (
                session.query(Issue)
                .filter(
                    Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS]),
                    Issue.sla_deadline != None,  # noqa: E711
                    Issue.sla_deadline < now,
                )
                .all()
            )

            config = get_config()
            created_notification_audits: list[tuple[object, int]] = []

            for issue in overdue_issues:
                if issue.id in self._notified_overdue:
                    continue

                recipients: set[int] = set()

                # Notify assignee
                if issue.assignee_id:
                    recipients.add(issue.assignee_id)

                # Notify all platform admins
                for admin_username in config.platform_owners:
                    admin_user = (
                        session.query(User)
                        .filter(User.username == admin_username)
                        .first()
                    )
                    if admin_user:
                        recipients.add(admin_user.id)

                notified_anyone = False
                audit_user_id = resolve_audit_user_id(
                    issue.created_by or issue.assignee_id
                )

                for uid in recipients:
                    # Check if already notified in DB (multi-worker/restart safety)
                    already_notified = (
                        session.query(Notification)
                        .filter(
                            Notification.user_id == uid,
                            Notification.type == "sla_overdue",
                            Notification.related_entity_id == issue.id,
                        )
                        .first()
                    )
                    if already_notified:
                        continue

                    notif = Notification(
                        user_id=uid,
                        title=f"SLA Overdue: {issue.title}",
                        message=f"Issue #{issue.id} has exceeded its SLA deadline.",
                        type="sla_overdue",
                        related_entity_type="issue",
                        related_entity_id=issue.id,
                    )
                    session.add(notif)
                    session.flush()

                    if audit_user_id is not None and notif.id is not None:
                        created_notification_audits.append((notif, audit_user_id))
                    notified_anyone = True

                self._notified_overdue.add(issue.id)

                if notified_anyone:
                    logger.info(
                        "SLA overdue notification sent: issue_id={} recipients={}",
                        issue.id,
                        recipients,
                    )

            session.commit()

            for notif, audit_user_id in created_notification_audits:
                log_audit(
                    user_id=audit_user_id,
                    action="create",
                    entity_type="notification",
                    entity_id=notif.id,
                    new_value=serialize_notification(notif),
                )
        except Exception:
            session.rollback()
            logger.opt(exception=True).error("Error checking SLA overdue")
        finally:
            session.close()

    def check_duty_start(self) -> None:
        """
        Notify each assignee once per schedule row while that shift is active.

        Dedupes on (user, schedule id) using persisted Notification rows
        so that long shifts don't produce repeated notifications and
        restarts / multiple workers are safe.
        """
        db = _get_db()
        session = db.get_session()
        try:
            now = datetime.utcnow()
            active_schedules = (
                session.query(Schedule)
                .filter(Schedule.start_time <= now, Schedule.end_time >= now)
                .all()
            )
            created_notification_audits: list[tuple[object, int]] = []

            for sched in active_schedules:
                already = (
                    session.query(Notification)
                    .filter(
                        Notification.user_id == sched.assignee_id,
                        Notification.type == "duty_started",
                        Notification.related_entity_type == "schedule",
                        Notification.related_entity_id == sched.id,
                    )
                    .first()
                )
                if already:
                    continue

                audit_user_id = resolve_audit_user_id(
                    sched.created_by or sched.assignee_id
                )
                notif = Notification(
                    user_id=sched.assignee_id,
                    title="Your On-Duty Period Has Started",
                    message=(
                        f"Your on-duty shift is active until "
                        f"{sched.end_time.strftime('%Y-%m-%d %H:%M')} UTC. "
                        "New issues may be assigned to you during this window."
                    ),
                    type="duty_started",
                    related_entity_type="schedule",
                    related_entity_id=sched.id,
                )
                session.add(notif)
                session.flush()

                if audit_user_id is not None and notif.id is not None:
                    created_notification_audits.append((notif, audit_user_id))
                logger.info(
                    "Duty start notification: schedule_id={} assignee_id={}",
                    sched.id,
                    sched.assignee_id,
                )

            session.commit()

            for notif, audit_user_id in created_notification_audits:
                log_audit(
                    user_id=audit_user_id,
                    action="create",
                    entity_type="notification",
                    entity_id=notif.id,
                    new_value=serialize_notification(notif),
                )
        except Exception:
            session.rollback()
            logger.opt(exception=True).error("Error sending duty start notification")
        finally:
            session.close()
