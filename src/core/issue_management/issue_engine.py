"""
Issue Engine — automatic Issue creation, SLA calculation, and auto-dispatch.

This module provides the core logic for:
  1. Creating Issues with automatic assignee resolution
  2. Calculating SLA deadlines based on issue type
  3. Dispatching Issues to the on-duty Ops Member (or fallback to Admin)
  4. Duplicate detection to avoid repeated alerts for the same problem
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from core.config import get_config
from core.models.database import Database, get_db
from core.models.user import User
from core.services.user_service import get_user_by_id
from core.models.entities import Issue, IssueType, IssueStatus, Schedule, UserIssuePreference, Product, Project, Job, Application
from core.logging import get_logger
from core.services.audit_service import (
    log_audit,
    resolve_audit_user_id,
    serialize_issue,
    serialize_notification,
)
from core.services.support_group_service import resolve_support_group_snapshot

logger = get_logger(__name__)


# ── SLA Configuration (minutes) ──────────────────────────────────────

ADMIN_REVIEW_ISSUE_TYPES = {
    IssueType.HANDOVER_REVIEW,
    IssueType.SCOPE_CHANGE_REVIEW,
}

OPS_ISSUE_TYPES = {
    IssueType.JOB_NOT_TRIGGERED,
    IssueType.JOB_FAILED,
    IssueType.JOB_STALE,
    IssueType.APP_OFFLINE,
    IssueType.MMP_DRIFT,
    IssueType.MMP_FAIRNESS_RISK,
    IssueType.MMP_RUN_PENDING_APPROVAL,
    IssueType.MMP_PENDING_REVIEW,
    IssueType.MMP_UNAPPROVED_EXP_RUN,
}

SLA_DEADLINES = {
    IssueType.JOB_NOT_TRIGGERED: 60,                # 1 hour to resolve
    IssueType.JOB_FAILED: 60,                       # 1 hour to resolve
    IssueType.JOB_STALE: 60,                        # 1 hour to resolve
    IssueType.APP_OFFLINE: 30,                      # 30 minutes to resolve
    IssueType.MMP_DRIFT: 240,                       # 4 hours to resolve
    # The three additional MMP concerns are model-governance signals rather
    # than production outages, so we give Ops a working day to action them.
    # If a fairness/approval finding actually IS time-critical the
    # responder can escalate manually — defaulting too tight just produces
    # SLA-overdue noise on signals that are usually scheduled work.
    IssueType.MMP_FAIRNESS_RISK: 1440,              # 24 hours to resolve
    IssueType.MMP_RUN_PENDING_APPROVAL: 1440,       # 24 hours to resolve
    IssueType.MMP_PENDING_REVIEW: 1440,             # 24 hours to resolve
    IssueType.MMP_UNAPPROVED_EXP_RUN: 1440,         # 24 hours to resolve
    IssueType.HANDOVER_REVIEW: 1440,                # 24 hours to review
    IssueType.SCOPE_CHANGE_REVIEW: 1440,            # 24 hours to review
}


def _get_db() -> Database:
    return get_db()


def _get_any_user_id(db: Database) -> Optional[int]:
    """Pick a deterministic fallback user ID when no platform admin is configured."""
    session = db.get_session()
    try:
        user = session.query(User).order_by(User.id.asc()).first()
        return user.id if user else None
    finally:
        session.close()


def get_current_on_duty_schedules(db: Database) -> list[Schedule]:
    """Get all currently active on-duty schedule rows."""
    now = datetime.utcnow()
    session = db.get_session()
    try:
        return session.query(Schedule).filter(
            Schedule.start_time <= now,
            Schedule.end_time >= now
        ).all()
    finally:
        session.close()


def get_current_on_duty_members(db: Database) -> list[User]:
    """Get all currently on-duty users from the schedule."""
    users: list[User] = []
    for schedule in get_current_on_duty_schedules(db):
        user = get_user_by_id(db, schedule.assignee_id)
        if user:
            users.append(user)
    return users


def get_open_issue_count(db: Database, user_id: int) -> int:
    """Get the number of open/in-progress issues for a user."""
    session = db.get_session()
    try:
        return session.query(Issue).filter(
            Issue.assignee_id == user_id,
            Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS])
        ).count()
    finally:
        session.close()


def get_fallback_admin(db: Database) -> Optional[User]:
    """
    Get the first Platform Admin as fallback assignee.

    Used when no Ops Member is on duty (no schedule configured).
    """
    config = get_config()
    if not config.platform_owners:
        return None

    session = db.get_session()
    try:
        for admin_username in config.platform_owners:
            user = session.query(User).filter(
                User.username == admin_username
            ).first()
            if user:
                return user
        return None
    finally:
        session.close()


# When no on-duty Ops member can take an Issue we assign it to a fixed real
# person rather than the generic top platform-owner account. The platform owner
# (config.platform_owners[0], typically the catch-all "admin") was never the
# right human to chase ops work — pin the fallback to the Ops lead so unassigned
# Issues land on someone who actually triages them.
FALLBACK_ASSIGNEE_USERNAME = "demo001"


def get_fallback_assignee(db: Database) -> Optional[User]:
    """Resolve the fixed fallback *assignee* for auto-created Issues.

    Returns the Ops lead (``FALLBACK_ASSIGNEE_USERNAME``) when that account
    exists and is a valid assignee (admin / relayops_member — Issues can only be
    assigned to those roles). Otherwise degrades to :func:`get_fallback_admin`
    so an Issue is never left unassigned (or rejected by the assignee-role
    guard) just because the lead's row hasn't synced or isn't a Ops member yet.
    """
    session = db.get_session()
    try:
        user = session.query(User).filter(
            User.username == FALLBACK_ASSIGNEE_USERNAME
        ).first()
        # Read role inside the session so it's available after close().
        role = user.role if user is not None else None
    finally:
        session.close()
    if user is not None and role in ("admin", "relayops_member"):
        return user
    logger.warning(
        "Fallback assignee '{}' unavailable (found={}, role={}); using "
        "platform-admin fallback",
        FALLBACK_ASSIGNEE_USERNAME,
        user is not None,
        role,
    )
    return get_fallback_admin(db)


def get_user_issue_preferences(db: Database, user_ids: list[int]) -> dict[int, set[str]]:
    """Get preferred issue types for a list of users."""
    if not user_ids:
        return {}

    session = db.get_session()
    try:
        prefs = (
            session.query(UserIssuePreference)
            .filter(UserIssuePreference.user_id.in_(user_ids))
            .all()
        )
        result: dict[int, set[str]] = {user_id: set() for user_id in user_ids}
        for pref in prefs:
            result.setdefault(pref.user_id, set()).add(pref.issue_type)
        return result
    finally:
        session.close()


def resolve_assignee(db: Database, issue_type: Optional[str] = None) -> Optional[int]:
    """
    Determine who to assign a new Issue to.

    Priority:
      1. Review issues go directly to Platform Admin
      2. Operational issues go to current on-duty Ops Members with the fewest open issues
      3. If tied, prefer members whose saved preference matches the issue type
      4. If still tied, prefer Primary duty role
      5. If still tied, use a deterministic user-id fallback
      6. Fallback to the fixed Ops-lead assignee (get_fallback_assignee)
      7. None (unassigned)
    """
    if issue_type in ADMIN_REVIEW_ISSUE_TYPES:
        fallback = get_fallback_assignee(db)
        if fallback:
            logger.debug(
                "Assigning review issue to fallback assignee: issue_type={} username={} id={}",
                issue_type,
                fallback.username,
                fallback.id,
            )
            return fallback.id
        logger.warning("Review issue has no fallback assignee configured: issue_type={}", issue_type)
        return None

    on_duty_schedules = get_current_on_duty_schedules(db)
    if issue_type in OPS_ISSUE_TYPES and on_duty_schedules:
        candidate_rows: list[dict] = []
        user_ids: list[int] = []
        for schedule in on_duty_schedules:
            member = get_user_by_id(db, schedule.assignee_id)
            if not member:
                continue
            # Admins explicitly placed on the duty roster are valid
            # candidates — scheduling oneself is an unambiguous "I'll
            # take it." Filtering them used to push their issues to the
            # fallback admin (config.platform_owners[0]) regardless of
            # the actual on-duty roster, which silently nullified the
            # schedule for anyone who happened to be a platform_owner.
            if member.role not in ("relayops_member", "admin"):
                continue
            user_ids.append(member.id)
            candidate_rows.append({
                "user": member,
                "duty_role": schedule.duty_role or "Primary",
                "open_issue_count": get_open_issue_count(db, member.id),
            })

        if candidate_rows:
            preferences = get_user_issue_preferences(db, user_ids)
            for row in candidate_rows:
                row["preference_match"] = bool(
                    issue_type and issue_type in preferences.get(row["user"].id, set())
                )
                row["is_primary"] = (row["duty_role"] or "").lower() == "primary"

            candidate_rows.sort(
                key=lambda row: (
                    row["open_issue_count"],
                    0 if row["preference_match"] else 1,
                    0 if row["is_primary"] else 1,
                    row["user"].id,
                )
            )
            best = candidate_rows[0]
            logger.debug(
                "Auto-assigning to on-duty member: {} (id={}, open_issues={}, preference_match={}, duty_role={})",
                best["user"].username,
                best["user"].id,
                best["open_issue_count"],
                best["preference_match"],
                best["duty_role"],
            )
            return best["user"].id
        logger.info(
            "No on-duty Ops member eligible for operational issue, using admin fallback: issue_type={}",
            issue_type,
        )

    fallback = get_fallback_assignee(db)
    if fallback:
        logger.info(
            "No on-duty member, falling back to fixed assignee: {} (id={})",
            fallback.username,
            fallback.id,
        )
        return fallback.id

    logger.warning("No assignee found: no on-duty member and no fallback assignee configured")
    return None


def _resolve_issue_group_context(session, product_id: Optional[int], job_id: Optional[int], app_id: Optional[int]) -> dict:
    support_group_id = None
    support_group_name = ""
    owner_group_id = None
    owner_group_name = ""

    if job_id is not None:
        job = session.query(Job).filter(Job.id == job_id).first()
        if job:
            support_group_id, support_group_name = resolve_support_group_snapshot(
                session,
                job.support_group_id,
                job.support_group_name_snapshot or job.support_group,
            )
    elif app_id is not None:
        app = session.query(Application).filter(Application.id == app_id).first()
        if app:
            support_group_id, support_group_name = resolve_support_group_snapshot(
                session,
                app.support_group_id,
                app.support_group_name_snapshot or app.support_group,
            )

    if product_id is not None:
        product = session.query(Product).filter(Product.id == product_id).first()
        project = session.query(Project).filter(Project.id == product.project_id).first() if product else None
        if project:
            owner_group_id = project.owner_group_id
            owner_group_name = project.owner_group_name_snapshot or ""

    return {
        "support_group_id": support_group_id,
        "support_group_name": support_group_name,
        "owner_group_id": owner_group_id,
        "owner_group_name": owner_group_name,
    }


def _assigned_via_value(db: Database, assignee_id: Optional[int], explicit_assignment: bool) -> str:
    if assignee_id is None:
        return ""
    if explicit_assignment:
        return "manual"
    on_duty_ids = {schedule.assignee_id for schedule in get_current_on_duty_schedules(db)}
    if assignee_id in on_duty_ids:
        return "schedule"
    return "group_fallback"


def calculate_sla_deadline(issue_type: str) -> Optional[datetime]:
    """Calculate the SLA deadline based on issue type."""
    minutes = SLA_DEADLINES.get(issue_type)
    if minutes:
        return datetime.utcnow() + timedelta(minutes=minutes)
    return None


def has_open_issue(
    db: Database,
    issue_type: str,
    job_id: Optional[int] = None,
    app_id: Optional[int] = None,
    product_id: Optional[int] = None,
) -> bool:
    """
    Check if there's already an open/in-progress Issue for the same entity.

    Used to avoid duplicate alerts (e.g., repeated job_not_triggered for the same job).
    """
    session = db.get_session()
    try:
        query = session.query(Issue).filter(
            Issue.type == issue_type,
            Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS]),
        )
        if job_id is not None:
            query = query.filter(Issue.job_id == job_id)
        if app_id is not None:
            query = query.filter(Issue.app_id == app_id)
        if product_id is not None:
            query = query.filter(Issue.product_id == product_id)
        return query.first() is not None
    finally:
        session.close()


def has_issue_for_dedup_key(
    db: Database,
    issue_type: str,
    dedup_key: str,
    job_id: Optional[int] = None,
    app_id: Optional[int] = None,
    product_id: Optional[int] = None,
) -> bool:
    """Check whether an Issue already exists for this exact occurrence.

    Unlike :func:`has_open_issue`, this matches on a stable per-event
    ``dedup_key`` (e.g. a CML ``cml_run_id``) regardless of issue status —
    so a recurring fault raises a fresh Issue on each *new* event even while
    an earlier Issue for a *different* event is still open, but the same event
    re-observed on a later monitoring tick never double-fires.
    """
    session = db.get_session()
    try:
        query = session.query(Issue).filter(
            Issue.type == issue_type,
            Issue.dedup_key == dedup_key,
        )
        if job_id is not None:
            query = query.filter(Issue.job_id == job_id)
        if app_id is not None:
            query = query.filter(Issue.app_id == app_id)
        if product_id is not None:
            query = query.filter(Issue.product_id == product_id)
        return query.first() is not None
    finally:
        session.close()


def create_issue(
    issue_type: str,
    title: str,
    description: str = "",
    product_id: Optional[int] = None,
    job_id: Optional[int] = None,
    app_id: Optional[int] = None,
    created_by: Optional[int] = None,
    assignee_id: Optional[int] = None,
    skip_duplicate_check: bool = False,
    dedup_key: Optional[str] = None,
    external_url: Optional[str] = None,
    mmp_run_id: Optional[int] = None,
    session: Optional[Session] = None,
) -> Optional[Issue]:
    """
    Create a new Issue with automatic SLA calculation and assignee resolution.

    Args:
        issue_type: One of IssueType constants.
        title: Brief description of the issue.
        description: Detailed description.
        product_id: Related product (optional).
        job_id: Related job (optional).
        app_id: Related application (optional).
        created_by: User who created/triggered the issue. Uses admin fallback if None.
        assignee_id: Explicit assignee. If None, auto-resolves from schedule.
        skip_duplicate_check: If True, skip duplicate detection.
        dedup_key: Stable per-occurrence key (e.g. a CML cml_run_id). When set,
            duplicate detection matches on (type, entity, dedup_key) across ALL
            statuses — one Issue per distinct event, so a recurring fault
            re-alerts on each new event instead of being suppressed by a
            still-open earlier Issue. When None, falls back to the legacy
            "any open issue of this type for this entity" check.
        session: Optional external session. When provided, the caller owns the
            transaction (commit/rollback/close); this function only flushes so
            the returned Issue has an assigned id. When None, an internal
            session is opened and committed immediately.

    Returns:
        The created Issue, or None if a duplicate already exists.
    """
    db = _get_db()

    # Duplicate check. With a dedup_key we dedup per distinct event (one Issue
    # per occurrence, any status); without one we keep the legacy "one open
    # issue of this type per entity" behaviour.
    if not skip_duplicate_check:
        if dedup_key is not None:
            if has_issue_for_dedup_key(
                db, issue_type, dedup_key, job_id=job_id, app_id=app_id, product_id=product_id
            ):
                logger.debug(
                    "Skipping duplicate issue (same event): type={} job_id={} "
                    "app_id={} product_id={} dedup_key={}",
                    issue_type, job_id, app_id, product_id, dedup_key,
                )
                return None
        elif has_open_issue(db, issue_type, job_id=job_id, app_id=app_id, product_id=product_id):
            logger.debug(
                "Skipping duplicate issue: type={} job_id={} app_id={} product_id={}",
                issue_type, job_id, app_id, product_id,
            )
            return None

    # MMP run advancing from "pending approval" to "pending review": update the
    # existing approval ticket in place instead of opening a second one. Both
    # dedup keys embed the same production run id (run_pending_approval:{id} vs
    # run_pending_user_review:{id}), so we can find the open approval Issue for
    # this exact run and transition it. Runs only ever move approval → review,
    # so this is the one direction we handle. Reached only when no review Issue
    # already exists for the run (the dedup check above returns first).
    if (
        issue_type == IssueType.MMP_PENDING_REVIEW
        and dedup_key
        and dedup_key.startswith("run_pending_user_review:")
    ):
        approval_dedup_key = "run_pending_approval:" + dedup_key.split(":", 1)[1]
        transitioned = transition_open_issue(
            from_type=IssueType.MMP_RUN_PENDING_APPROVAL,
            to_type=IssueType.MMP_PENDING_REVIEW,
            match_dedup_key=approval_dedup_key,
            new_dedup_key=dedup_key,
            new_title=title,
            new_description=description,
            job_id=job_id,
            app_id=app_id,
            product_id=product_id,
            external_url=external_url,
            mmp_run_id=mmp_run_id,
            reason=(
                "MMP run advanced from pending approval to pending review — "
                "existing ticket updated in place."
            ),
            session=session,
        )
        if transitioned is not None:
            return transitioned

    # Resolve assignee if not explicitly provided
    explicit_assignment = assignee_id is not None
    if assignee_id is None:
        assignee_id = resolve_assignee(db, issue_type=issue_type)

    # Resolve created_by: use system admin if not provided (for automated alerts)
    if created_by is None:
        admin = get_fallback_admin(db)
        if admin:
            created_by = admin.id
        elif assignee_id is not None:
            created_by = assignee_id
        else:
            created_by = _get_any_user_id(db)

    if created_by is None:
        logger.error(
            "Skipping issue creation because no user is available for created_by: type={}",
            issue_type,
        )
        return None

    # Calculate SLA deadline
    sla_deadline = calculate_sla_deadline(issue_type)

    use_external_session = session is not None
    if not use_external_session:
        session = db.get_session()
    try:
        created_notifications = []
        group_context = _resolve_issue_group_context(session, product_id, job_id, app_id)
        issue = Issue(
            type=issue_type,
            status=IssueStatus.OPEN,
            title=title,
            description=description,
            product_id=product_id,
            job_id=job_id,
            app_id=app_id,
            created_by=created_by,
            assignee_id=assignee_id,
            support_group_id=group_context["support_group_id"],
            support_group_name=group_context["support_group_name"],
            owner_group_id=group_context["owner_group_id"],
            owner_group_name=group_context["owner_group_name"],
            assigned_via=_assigned_via_value(db, assignee_id, explicit_assignment),
            dedup_key=dedup_key,
            external_url=external_url,
            mmp_run_id=mmp_run_id,
            sla_deadline=sla_deadline,
        )
        session.add(issue)
        session.flush()  # get issue.id before commit

        # Notify the assignee. The on-duty email is sent AFTER commit (see end
        # of function) so a slow relay never holds this transaction open; the
        # notification message is annotated with the outcome at that point.
        if assignee_id:
            from core.models.entities import Notification
            notif = Notification(
                user_id=assignee_id,
                title=f"New Issue Assigned: {title}",
                message=f"You have been assigned a new {issue_type.replace('_', ' ')} issue.",
                type="issue_assigned",
                related_entity_type="issue",
                related_entity_id=issue.id,
            )
            session.add(notif)
            created_notifications.append(notif)

        # Notify platform admins (excluding the assignee if they happen to be an admin)
        admin = get_fallback_admin(db)
        if admin and admin.id != assignee_id:
            from core.models.entities import Notification
            admin_notif = Notification(
                user_id=admin.id,
                title=f"New System Alert: {title}",
                message=f"A new {issue_type.replace('_', ' ')} issue has been created and assigned to user #{assignee_id}.",
                type="system_alert",
                related_entity_type="issue",
                related_entity_id=issue.id,
            )
            session.add(admin_notif)
            created_notifications.append(admin_notif)

        if product_id is not None:
            product = session.query(Product).filter(Product.id == product_id).first()
            project = session.query(Project).filter(Project.id == product.project_id).first() if product else None
            owner_id = project.owner_id if project else None
            if owner_id and owner_id not in {assignee_id, created_by, getattr(admin, "id", None)}:
                from core.models.entities import Notification
                owner_notif = Notification(
                    user_id=owner_id,
                    title=f"Issue Created: {title}",
                    message=f"A new {issue_type.replace('_', ' ')} issue was created for your product.",
                    type="issue_created",
                    related_entity_type="issue",
                    related_entity_id=issue.id,
                )
                session.add(owner_notif)
                created_notifications.append(owner_notif)

        if use_external_session:
            # Caller owns the transaction. Flush to make ids available to any
            # follow-up logic in the caller, but do not commit/refresh.
            session.flush()
        else:
            session.commit()
            session.refresh(issue)

        audit_user_id = resolve_audit_user_id(created_by)
        if audit_user_id is not None:
            log_audit(
                user_id=audit_user_id,
                action="create",
                entity_type="issue",
                entity_id=issue.id,
                new_value=serialize_issue(issue),
            )
            for notif in created_notifications:
                if notif.id is not None:
                    log_audit(
                        user_id=audit_user_id,
                        action="create",
                        entity_type="notification",
                        entity_id=notif.id,
                        new_value=serialize_notification(notif),
                    )

        logger.info(
            "Issue created: id={} type={} assignee_id={} sla={}",
            issue.id, issue_type, assignee_id,
            sla_deadline.isoformat() if sla_deadline else "none",
        )
    except Exception:
        if not use_external_session:
            session.rollback()
        logger.opt(exception=True).error("Failed to create issue: type={}", issue_type)
        raise
    finally:
        if not use_external_session:
            session.close()

    # Email the on-duty Ops about operational issues. Done here — after the
    # transaction committed and the connection was released — so the bounded
    # network send never holds a DB connection or worker thread.
    #   * internal session: we own the commit, so dispatch now.
    #   * external session: the caller (e.g. the Controller) owns the commit
    #     and dispatches after IT commits; dispatching here would email about
    #     a not-yet-committed (possibly rolled-back) issue.
    if not use_external_session:
        from core.issue_management.email_dispatch import dispatch_issue_email
        dispatch_issue_email(issue)

    return issue


def reassign_open_ops_issues_in_window(
    session: Session,
    *,
    assignee_id: int,
    start_time: datetime,
    end_time: datetime,
    actor_user_id: Optional[int] = None,
) -> list[Issue]:
    """Retroactively hand the on-duty member the operational Issues that arose
    during a (possibly back-dated) shift.

    Finds OPEN/IN_PROGRESS operational Issues (``OPS_ISSUE_TYPES``) whose
    ``created_at`` falls inside ``[start_time, min(now, end_time)]`` and that
    aren't already assigned to ``assignee_id``, then reassigns each to that
    member (``assigned_via='schedule'``). Every reassignment writes an
    ``update`` audit entry (old → new assignee) so it renders on the issue
    timeline, and notifies the new assignee. Review/handover Issues are left
    untouched — those never route through the duty roster.

    The caller owns the transaction (this only flushes). Returns the reassigned
    Issues so the caller can dispatch notification emails after commit.
    """
    now = datetime.utcnow()
    window_end = min(now, end_time)
    if start_time > window_end:
        # Purely-future shift: no already-created Issues to pick up.
        return []

    issues = (
        session.query(Issue)
        .filter(
            Issue.type.in_(list(OPS_ISSUE_TYPES)),
            Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS]),
            Issue.created_at >= start_time,
            Issue.created_at <= window_end,
            Issue.assignee_id != assignee_id,
        )
        .all()
    )
    if not issues:
        return []

    from core.models.entities import Notification

    pending: list[tuple[Issue, dict]] = []
    for issue in issues:
        old_val = serialize_issue(issue)
        issue.assignee_id = assignee_id
        issue.assigned_via = "schedule"
        issue.updated_at = now
        session.add(Notification(
            user_id=assignee_id,
            title=f"Issue Reassigned: {issue.title}",
            message=(
                f"Issue #{issue.id} was reassigned to you because you were set "
                "on duty for the window in which it was raised."
            ),
            type="issue_reassigned",
            related_entity_type="issue",
            related_entity_id=issue.id,
        ))
        pending.append((issue, old_val))

    session.flush()

    audit_user_id = resolve_audit_user_id(actor_user_id)
    result: list[Issue] = []
    for issue, old_val in pending:
        if audit_user_id is not None:
            log_audit(
                user_id=audit_user_id,
                action="update",
                entity_type="issue",
                entity_id=issue.id,
                old_value=old_val,
                new_value=serialize_issue(issue),
            )
        result.append(issue)

    logger.info(
        "Back-dated duty reassign: {} issue(s) -> assignee_id={} window=[{}, {}]",
        len(result), assignee_id, start_time.isoformat(), window_end.isoformat(),
    )
    return result


def transition_open_issue(
    *,
    from_type: str,
    to_type: str,
    match_dedup_key: str,
    new_dedup_key: Optional[str],
    new_title: str,
    new_description: str,
    job_id: Optional[int] = None,
    app_id: Optional[int] = None,
    product_id: Optional[int] = None,
    external_url: Optional[str] = None,
    mmp_run_id: Optional[int] = None,
    reason: str = "",
    session: Optional[Session] = None,
) -> Optional[Issue]:
    """Transition an existing OPEN/IN_PROGRESS Issue in place instead of closing
    it and opening a fresh one.

    Matches the single open Issue of ``from_type`` whose ``dedup_key`` equals
    ``match_dedup_key`` for the same entity, then rewrites it to ``to_type``
    (updating title/description/dedup_key/external_url/mmp_run_id and
    recomputing the SLA deadline). A ``transition`` audit entry (old type → new
    type) is written so the change shows as one continuous ticket on the issue
    timeline, and the current assignee is notified.

    Used by the MMP flow so a run moving from *pending approval* to *pending
    review* updates the same ticket rather than spawning a second one. Returns
    the updated Issue, or ``None`` when there is no matching open Issue to
    transition (caller then falls back to creating a new Issue).

    Session handling mirrors :func:`create_issue`: with an external ``session``
    the caller owns the transaction (we only flush); otherwise an internal
    session is opened and committed.
    """
    db = _get_db()
    use_external_session = session is not None
    if not use_external_session:
        session = db.get_session()
    try:
        query = session.query(Issue).filter(
            Issue.type == from_type,
            Issue.dedup_key == match_dedup_key,
            Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS]),
        )
        if job_id is not None:
            query = query.filter(Issue.job_id == job_id)
        if app_id is not None:
            query = query.filter(Issue.app_id == app_id)
        if product_id is not None:
            query = query.filter(Issue.product_id == product_id)
        issue = query.first()
        if issue is None:
            return None

        old_val = serialize_issue(issue)
        issue.type = to_type
        issue.title = new_title
        issue.description = new_description
        issue.dedup_key = new_dedup_key
        if external_url is not None:
            issue.external_url = external_url
        if mmp_run_id is not None:
            issue.mmp_run_id = mmp_run_id
        issue.sla_deadline = calculate_sla_deadline(to_type)
        issue.updated_at = datetime.utcnow()

        notif = None
        if issue.assignee_id:
            from core.models.entities import Notification
            notif = Notification(
                user_id=issue.assignee_id,
                title=f"Issue Updated: {new_title}",
                message=(
                    reason
                    or f"Issue #{issue.id} advanced to {to_type.replace('_', ' ')}."
                ),
                type="issue_transitioned",
                related_entity_type="issue",
                related_entity_id=issue.id,
            )
            session.add(notif)

        if use_external_session:
            session.flush()
        else:
            session.commit()
            session.refresh(issue)

        audit_user_id = resolve_audit_user_id(issue.created_by or issue.assignee_id)
        if audit_user_id is not None:
            log_audit(
                user_id=audit_user_id,
                action="transition",
                entity_type="issue",
                entity_id=issue.id,
                old_value=old_val,
                new_value=serialize_issue(issue),
            )
            if notif is not None and notif.id is not None:
                log_audit(
                    user_id=audit_user_id,
                    action="create",
                    entity_type="notification",
                    entity_id=notif.id,
                    new_value=serialize_notification(notif),
                )

        logger.info(
            "Transitioned issue #{}: {} -> {} (dedup {} -> {})",
            issue.id, from_type, to_type, match_dedup_key, new_dedup_key,
        )
        return issue
    except Exception:
        if not use_external_session:
            session.rollback()
        logger.opt(exception=True).error(
            "Failed to transition issue from {} to {}", from_type, to_type
        )
        raise
    finally:
        if not use_external_session:
            session.close()


def auto_close_open_issues(
    session: Session,
    *,
    issue_type: str,
    job_id: Optional[int] = None,
    app_id: Optional[int] = None,
    product_id: Optional[int] = None,
    resolution_description: str = "",
    created_before: Optional[datetime] = None,
) -> list[Issue]:
    """Auto-close OPEN/IN_PROGRESS issues of ``issue_type`` for an entity
    because the underlying monitored signal recovered.

    Sets each issue to CLOSED with ``resolved_at`` now, notifies the assignee
    (``issue_auto_closed``), and writes an ``auto_close`` audit entry. The
    caller owns the transaction — this mutates and flushes on the provided
    session but never commits. Returns the issues that were closed (possibly
    empty).

    ``created_before`` (optional) scopes the close to Issues created strictly
    before that timestamp — used by the MMP approval-supersedes path so a newly
    approved run only clears concerns raised against earlier runs, leaving any
    Issue raised after the approval intact. ``None`` closes all matching open
    Issues regardless of age.
    """
    query = session.query(Issue).filter(
        Issue.type == issue_type,
        Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS]),
    )
    if job_id is not None:
        query = query.filter(Issue.job_id == job_id)
    if app_id is not None:
        query = query.filter(Issue.app_id == app_id)
    if product_id is not None:
        query = query.filter(Issue.product_id == product_id)
    if created_before is not None:
        query = query.filter(Issue.created_at < created_before)

    issues = query.all()
    if not issues:
        return []

    from core.models.entities import Notification

    now = datetime.utcnow()
    closed: list[Issue] = []
    for issue in issues:
        issue.status = IssueStatus.CLOSED
        issue.resolved_at = now
        if resolution_description:
            issue.resolution_description = resolution_description
        if issue.assignee_id:
            session.add(Notification(
                user_id=issue.assignee_id,
                title=f"Issue Auto-Closed: {issue.title}",
                message=(
                    f"Issue #{issue.id} was auto-closed after the system "
                    "detected the underlying condition recovered."
                ),
                type="issue_auto_closed",
                related_entity_type="issue",
                related_entity_id=issue.id,
            ))
        closed.append(issue)

    session.flush()

    for issue in closed:
        audit_user_id = resolve_audit_user_id(issue.created_by or issue.assignee_id)
        if audit_user_id is not None:
            log_audit(
                user_id=audit_user_id,
                action="auto_close",
                entity_type="issue",
                entity_id=issue.id,
                new_value=serialize_issue(issue),
            )

    logger.info(
        "Auto-closed {} issue(s): type={} job_id={} app_id={} product_id={}",
        len(closed), issue_type, job_id, app_id, product_id,
    )
    return closed
