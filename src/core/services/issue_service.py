"""Issue lifecycle business logic.

Orchestrates Issue creation (with auto-dispatch), state transitions
(start_working / record_step / verification / escalate / resolve /
false_positive), handover approval/rejection, and generic issue updates
(status / assignee / resolution).

Pure module-level functions. Routers handle HTTP concerns; this module
handles business orchestration and raises domain exceptions on failure.
"""

from copy import deepcopy
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy.orm.attributes import flag_modified

from core.config import get_config
from core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from core.logging import get_logger
from core.models.database import Database, get_db
from core.models.entities import (
    Application,
    Issue,
    IssueActionType,
    IssueStatus,
    IssueType,
    Job,
    Notification,
    Product,
    Project,
    ProjectVersion,
)
from core.models.user import User, UserRole
from core.services.audit_service import (
    log_audit,
    serialize_issue,
    serialize_notification,
    serialize_project_version,
)
from core.services.project_version_service import (
    approve_project_version,
    reject_project_version,
)
from core.services.support_group_service import resolve_support_group_snapshot
from core.services.user_service import get_user_by_id

logger = get_logger(__name__)


def _get_db() -> Database:
    return get_db()


def false_positive_execution_keys(session, job_ids) -> set[tuple[int, str]]:
    """Return ``{(job_id, cml_run_id)}`` for executions Ops dismissed as noise.

    A ``job_failed`` / ``job_stale`` alert that an on-duty member marks as a
    false positive should make the underlying CML run count as a *success* in
    every health chart, not a failure. Those automated issues carry the run's
    ``cml_run_id`` as their ``dedup_key`` (see cml_checker), which matches
    ``JobExecution.cml_run_id`` for the same run — so the analytics + timeline
    layers look the execution up by ``(job_id, cml_run_id)``.

    Returns an empty set when ``job_ids`` is empty.
    """
    job_ids = [jid for jid in (job_ids or []) if jid is not None]
    if not job_ids:
        return set()
    rows = (
        session.query(Issue.job_id, Issue.dedup_key)
        .filter(
            Issue.job_id.in_(job_ids),
            Issue.status == IssueStatus.FALSE_POSITIVE,
            Issue.dedup_key.isnot(None),
        )
        .all()
    )
    return {(row.job_id, row.dedup_key) for row in rows}


def _coerce_string_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _build_resolution_text(payload: Optional[dict], fallback_text: Optional[str] = None) -> Optional[str]:
    if payload:
        if payload.get("kind") == IssueActionType.FALSE_POSITIVE:
            reason = (payload.get("reason") or "").strip()
            return f"False positive confirmed: {reason}" if reason else "False positive confirmed."
        scenario = payload.get("scenario_name")
        actions_taken = _coerce_string_list(payload.get("actions_taken"))
        verification_notes = (payload.get("verification_notes") or "").strip()
        final_conclusion = (payload.get("final_conclusion") or "").strip()

        parts = []
        if scenario:
            parts.append(f"Scenario: {scenario}")
        if actions_taken:
            parts.append(f"Actions: {'; '.join(actions_taken)}")
        if verification_notes:
            parts.append(f"Verification: {verification_notes}")
        if final_conclusion:
            parts.append(f"Conclusion: {final_conclusion}")
        if parts:
            return " | ".join(parts)
    return fallback_text


def _issue_owner_context(session, issue: Issue) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Return (owner_id, owner_username, owner_display_name) for the issue's product owner."""
    if issue.product_id is None:
        return None, None, None
    product = session.query(Product).filter(Product.id == issue.product_id).first()
    if product is None:
        return None, None, None
    project = session.query(Project).filter(Project.id == product.project_id).first()
    if project is None:
        return None, None, None
    owner = get_user_by_id(_get_db(), project.owner_id)
    if owner is None:
        return project.owner_id, None, None
    return project.owner_id, owner.username, owner.display_name


def _issue_notification_recipients(session, issue: Issue, include_assignee: bool = False) -> set[int]:
    recipients: set[int] = set()
    if include_assignee and issue.assignee_id:
        recipients.add(issue.assignee_id)
    owner_id, _, _ = _issue_owner_context(session, issue)
    if owner_id:
        recipients.add(owner_id)
    creator_id = issue.created_by
    if creator_id:
        recipients.add(creator_id)
    return recipients


def _create_issue_notifications(
    session,
    issue: Issue,
    actor_user_id: int,
    notif_type: str,
    title: str,
    message: str,
    include_assignee: bool = False,
    extra_user_ids: Optional[list[int]] = None,
) -> list[Notification]:
    """Create notifications for relevant stakeholders, excluding the actor."""
    recipients = _issue_notification_recipients(session, issue, include_assignee=include_assignee)
    for user_id in extra_user_ids or []:
        if user_id:
            recipients.add(user_id)
    recipients.discard(actor_user_id)

    created: list[Notification] = []
    for user_id in recipients:
        notif = Notification(
            user_id=user_id,
            title=title,
            message=message,
            type=notif_type,
            related_entity_type="issue",
            related_entity_id=issue.id,
        )
        session.add(notif)
        created.append(notif)
    return created


def _issue_group_context(
    session,
    *,
    resolved_product_id: Optional[int],
    job_id: Optional[int],
    app_id: Optional[int],
    explicit_support_group_id: Optional[int] = None,
) -> dict:
    support_group_id = None
    support_group_name = ""
    owner_group_id = None
    owner_group_name = ""

    if explicit_support_group_id is not None:
        support_group_id, support_group_name = resolve_support_group_snapshot(session, explicit_support_group_id)
    elif job_id is not None:
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

    if resolved_product_id is not None:
        product = session.query(Product).filter(Product.id == resolved_product_id).first()
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


def _assigned_via_value(db: Database, issue_type: str, assignee_id: Optional[int], explicit_assignment: bool) -> str:
    if assignee_id is None:
        return ""
    if explicit_assignment:
        return "manual"
    from core.issue_management.issue_engine import get_current_on_duty_schedules
    on_duty_ids = {schedule.assignee_id for schedule in get_current_on_duty_schedules(db)}
    if assignee_id in on_duty_ids:
        return "schedule"
    return "group_fallback"


def create_issue_with_dispatch(
    *,
    issue_type: str,
    title: str,
    description: str = "",
    product_id: Optional[int] = None,
    job_id: Optional[int] = None,
    app_id: Optional[int] = None,
    assignee_id: Optional[int] = None,
    support_group_id: Optional[int] = None,
    actor_user_id: int,
    actor_username: str,
) -> Issue:
    """Create a new Issue with auto-SLA + assignee resolution.

    Raises ConflictError on duplicate, ValidationError on invalid assignee.
    """
    from core.issue_management.issue_engine import (
        calculate_sla_deadline,
        has_open_issue,
        resolve_assignee,
    )

    db = _get_db()

    if has_open_issue(db, issue_type, job_id=job_id, app_id=app_id, product_id=product_id):
        raise ConflictError("An open issue of this type already exists for the same entity")

    resolved_assignee_id = assignee_id
    if resolved_assignee_id is None:
        resolved_assignee_id = resolve_assignee(db, issue_type=issue_type)

    if resolved_assignee_id is not None:
        assignee_user = get_user_by_id(db, resolved_assignee_id)
        if assignee_user and assignee_user.role not in ("admin", "relayops_member"):
            raise ValidationError("Issues can only be assigned to admin or relayops_member users")

    sla_deadline = calculate_sla_deadline(issue_type)

    session = db.get_session()
    try:
        created_notifications: list[Notification] = []
        resolved_product_id = product_id
        if resolved_product_id is None and job_id is not None:
            job = session.query(Job).filter(Job.id == job_id).first()
            if job:
                resolved_product_id = job.product_id
        if resolved_product_id is None and app_id is not None:
            app = session.query(Application).filter(Application.id == app_id).first()
            if app:
                resolved_product_id = app.product_id

        group_context = _issue_group_context(
            session,
            resolved_product_id=resolved_product_id,
            job_id=job_id,
            app_id=app_id,
            explicit_support_group_id=support_group_id,
        )

        issue = Issue(
            type=issue_type,
            status=IssueStatus.OPEN,
            title=title,
            description=description or "",
            product_id=resolved_product_id,
            job_id=job_id,
            app_id=app_id,
            created_by=actor_user_id,
            assignee_id=resolved_assignee_id,
            support_group_id=group_context["support_group_id"],
            support_group_name=group_context["support_group_name"],
            owner_group_id=group_context["owner_group_id"],
            owner_group_name=group_context["owner_group_name"],
            assigned_via=_assigned_via_value(db, issue_type, resolved_assignee_id, assignee_id is not None),
            sla_deadline=sla_deadline,
        )
        session.add(issue)
        session.flush()

        if resolved_assignee_id:
            notif = Notification(
                user_id=resolved_assignee_id,
                title=f"New Issue Assigned: {title}",
                message=f"You have been assigned a new {issue_type.replace('_', ' ')} issue.",
                type="issue_assigned",
                related_entity_type="issue",
                related_entity_id=issue.id,
            )
            session.add(notif)
            created_notifications.append(notif)

        created_notifications.extend(
            _create_issue_notifications(
                session,
                issue,
                actor_user_id,
                notif_type="issue_created",
                title=f"Issue Created: {title}",
                message=f"Issue #{issue.id} was created for {issue_type.replace('_', ' ')} and is ready for follow-up.",
            )
        )

        session.commit()
        session.refresh(issue)
        notification_audits = [
            (notif.id, serialize_notification(notif))
            for notif in created_notifications
            if notif.id is not None
        ]

        logger.info("Issue created: id={} type={} by user={}", issue.id, issue_type, actor_username)
        log_audit(
            user_id=actor_user_id,
            action="create",
            entity_type="issue",
            entity_id=issue.id,
            new_value=serialize_issue(issue),
        )
        for notif_id, notif_payload in notification_audits:
            log_audit(
                user_id=actor_user_id,
                action="create",
                entity_type="notification",
                entity_id=notif_id,
                new_value=notif_payload,
            )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    # Email the on-duty Ops AFTER commit + connection release, so the bounded
    # send never holds this request's DB connection or worker thread.
    from core.issue_management.email_dispatch import dispatch_issue_email
    dispatch_issue_email(issue)

    return issue


def approve_handover(
    *,
    issue_id: int,
    actor_user_id: int,
    actor_username: str,
) -> Issue:
    """Approve a handover review issue. Raises domain exceptions on failure."""
    db = _get_db()
    session = db.get_session()
    try:
        issue = session.query(Issue).filter(Issue.id == issue_id).first()
        if issue is None:
            raise NotFoundError("Issue not found")

        if issue.type != IssueType.HANDOVER_REVIEW:
            raise ValidationError(f"Issue is of type '{issue.type}', not 'handover_review'")

        if issue.status != IssueStatus.OPEN:
            raise ValidationError(f"Issue is in '{issue.status}' status, must be 'open' to approve")

        if issue.project_id is None:
            raise ValidationError("Issue has no associated project")

        project = session.query(Project).filter(Project.id == issue.project_id).first()
        if project is None:
            raise ValidationError("Associated project not found")

        version = None
        if issue.project_version_id is not None:
            version = session.query(ProjectVersion).filter(ProjectVersion.id == issue.project_version_id).first()
        if version is None:
            raise ValidationError("Associated project version not found")

        old_version_val = serialize_project_version(version)
        try:
            approve_project_version(session, project, version, reviewed_by=actor_user_id)
        except ValueError:
            raise ValidationError("Only pending review versions can be approved")

        issue.status = IssueStatus.CLOSED
        issue.resolved_at = datetime.utcnow()
        issue.resolution_description = f"Approved by {actor_username} (version v{version.version_number})"
        issue.updated_at = datetime.utcnow()

        notif = Notification(
            user_id=project.owner_id,
            title=f"Handover Approved: {project.name} v{version.version_number}",
            message=f"Your project \"{project.name}\" version v{version.version_number} has been approved by {actor_username} and is now active.",
            type="handover_approved",
            related_entity_type="project",
            related_entity_id=project.id,
        )
        session.add(notif)

        session.commit()
        session.refresh(issue)

        version_audit = {"old_version": old_version_val, "new_version": serialize_project_version(version)}
        logger.info("Handover approved: issue {} by admin {}", issue_id, actor_username)
        log_audit(
            user_id=actor_user_id,
            action="approve",
            entity_type="issue",
            entity_id=issue_id,
            new_value=serialize_issue(issue),
        )
        log_audit(
            user_id=actor_user_id,
            action="approve",
            entity_type="project_version",
            entity_id=issue.project_version_id or 0,
            old_value=version_audit.get("old_version"),
            new_value=version_audit.get("new_version"),
        )

        return issue
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reject_handover(
    *,
    issue_id: int,
    rejection_reason: str,
    actor_user_id: int,
    actor_username: str,
) -> Issue:
    """Reject a handover review issue. Raises domain exceptions on failure."""
    db = _get_db()
    session = db.get_session()
    try:
        issue = session.query(Issue).filter(Issue.id == issue_id).first()
        if issue is None:
            raise NotFoundError("Issue not found")

        if issue.type != IssueType.HANDOVER_REVIEW:
            raise ValidationError(f"Issue is of type '{issue.type}', not 'handover_review'")

        if issue.status != IssueStatus.OPEN:
            raise ValidationError(f"Issue is in '{issue.status}' status, must be 'open' to reject")

        if issue.project_id is None:
            raise ValidationError("Issue has no associated project")

        project = session.query(Project).filter(Project.id == issue.project_id).first()
        if project is None:
            raise ValidationError("Associated project not found")

        version = None
        if issue.project_version_id is not None:
            version = session.query(ProjectVersion).filter(ProjectVersion.id == issue.project_version_id).first()
        if version is None:
            raise ValidationError("Associated project version not found")

        old_version_val = serialize_project_version(version)
        try:
            new_draft = reject_project_version(
                session,
                project,
                version,
                reviewed_by=actor_user_id,
                rejection_reason=rejection_reason,
            )
        except ValueError:
            raise ValidationError("Only pending review versions can be rejected")

        issue.status = IssueStatus.CLOSED
        issue.resolved_at = datetime.utcnow()
        issue.rejection_reason = rejection_reason
        issue.resolution_description = (
            f"Rejected by {actor_username}: {rejection_reason} "
            f"(new draft v{new_draft.version_number} opened)"
        )
        issue.updated_at = datetime.utcnow()

        notif = Notification(
            user_id=project.owner_id,
            title=f"Handover Rejected: {project.name} v{version.version_number}",
            message=(
                f"Your project \"{project.name}\" version v{version.version_number} was rejected by "
                f"{actor_username}.\n\nReason: {rejection_reason}\n\n"
                f"A new draft version v{new_draft.version_number} has been opened for revision."
            ),
            type="handover_rejected",
            related_entity_type="project",
            related_entity_id=project.id,
        )
        session.add(notif)

        session.commit()
        session.refresh(issue)

        version_audit = {
            "old_version": old_version_val,
            "rejected_version": serialize_project_version(version),
            "next_draft_version_id": new_draft.id,
        }
        logger.info(
            "Handover rejected: issue {} by admin {}, reason: {}",
            issue_id,
            actor_username,
            rejection_reason,
        )
        log_audit(
            user_id=actor_user_id,
            action="reject",
            entity_type="issue",
            entity_id=issue_id,
            new_value=serialize_issue(issue),
        )
        log_audit(
            user_id=actor_user_id,
            action="reject",
            entity_type="project_version",
            entity_id=issue.project_version_id or 0,
            old_value=version_audit.get("old_version"),
            new_value={
                **(version_audit.get("rejected_version") or {}),
                "next_draft_version_id": version_audit.get("next_draft_version_id"),
            },
        )

        return issue
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_action(
    *,
    issue_id: int,
    action: str,
    actor_user_id: int,
    actor_username: str,
    is_admin: bool,
    scenario_id: Optional[int] = None,
    step_title: Optional[str] = None,
    notes: Optional[str] = None,
    verification_notes: Optional[str] = None,
    escalation_target: Optional[str] = None,
    actions_taken: Optional[list] = None,
    final_conclusion: Optional[str] = None,
    build_workspace_payload_fn=None,
) -> Issue:
    """Apply a structured workspace action. Raises domain exceptions on failure.

    build_workspace_payload_fn: Callable(issue, db) -> dict producing the
    workspace payload (scenarios list). Passed in by the router since it
    depends on HTTP-layer response models.
    """
    db = _get_db()
    session = db.get_session()
    try:
        issue = session.query(Issue).filter(Issue.id == issue_id).first()
        if issue is None:
            raise NotFoundError("Issue not found")
        if issue.type == IssueType.HANDOVER_REVIEW:
            raise ValidationError("handover_review issues must be handled via /approve or /reject")
        if not is_admin and issue.assignee_id != actor_user_id:
            raise ForbiddenError("Only the current assignee or admin can perform this action")
        if issue.status in (IssueStatus.RESOLVED, IssueStatus.CLOSED, IssueStatus.FALSE_POSITIVE):
            raise ValidationError("Issue is already in a terminal state")

        old_val = serialize_issue(issue)
        action_summary = deepcopy(issue.action_summary_json or {})
        action_summary.setdefault("steps", [])
        action_summary.setdefault("verifications", [])
        action_summary.setdefault("escalations", [])
        created_notifications: list[Notification] = []
        selected_scenario_name = issue.selected_scenario_name
        selected_scenario_type = issue.selected_scenario_type

        available_scenarios = []
        if build_workspace_payload_fn is not None:
            workspace = build_workspace_payload_fn(issue, db)
            available_scenarios = workspace.get("scenarios", [])

        selected_scenario = None
        if scenario_id is not None:
            for scenario in available_scenarios:
                if scenario.get("scenario_id") == scenario_id:
                    selected_scenario = scenario
                    break
        if selected_scenario is None and available_scenarios:
            selected_scenario = available_scenarios[0]
        if selected_scenario is not None:
            selected_scenario_name = selected_scenario.get("scenario_name")
            selected_scenario_type = selected_scenario.get("scenario_type")
            action_summary["selected_scenario_id"] = selected_scenario.get("scenario_id")
            action_summary["selected_scenario_name"] = selected_scenario_name
            action_summary["selected_scenario_type"] = selected_scenario_type

        issue.selected_scenario_name = selected_scenario_name
        issue.selected_scenario_type = selected_scenario_type

        if action == IssueActionType.START_WORKING:
            issue.status = IssueStatus.IN_PROGRESS
            action_summary["started_at"] = datetime.utcnow().isoformat()
            action_summary["started_by"] = actor_user_id
            created_notifications.extend(
                _create_issue_notifications(
                    session,
                    issue,
                    actor_user_id,
                    notif_type="issue_started",
                    title=f"Issue Started: {issue.title}",
                    message=f"{actor_username} started working on issue #{issue.id}.",
                )
            )
        elif action == IssueActionType.RECORD_STEP:
            if not (step_title or notes):
                raise ValidationError("step_title or notes is required when recording a step")
            issue.status = IssueStatus.IN_PROGRESS
            action_summary["steps"].append({
                "title": step_title or "Execution step",
                "notes": notes or "",
                "recorded_at": datetime.utcnow().isoformat(),
                "recorded_by": actor_user_id,
            })
        elif action == IssueActionType.VERIFICATION_PASSED:
            if not (verification_notes or "").strip():
                raise ValidationError("verification_notes is required when recording verification")
            issue.status = IssueStatus.IN_PROGRESS
            action_summary["verifications"].append({
                "notes": verification_notes or "",
                "recorded_at": datetime.utcnow().isoformat(),
                "recorded_by": actor_user_id,
            })
        elif action == IssueActionType.ESCALATE:
            target = (escalation_target or "").strip()
            if not target:
                raise ValidationError("escalation_target is required when escalating")
            issue.status = IssueStatus.IN_PROGRESS
            action_summary["escalations"].append({
                "target": target,
                "notes": notes or "",
                "recorded_at": datetime.utcnow().isoformat(),
                "recorded_by": actor_user_id,
            })
            created_notifications.extend(
                _create_issue_notifications(
                    session,
                    issue,
                    actor_user_id,
                    notif_type="issue_escalated",
                    title=f"Issue Escalated: {issue.title}",
                    message=f"{actor_username} escalated issue #{issue.id} to {target}.",
                )
            )
        elif action == IssueActionType.RETURN_TO_OWNER:
            owner_id, owner_username, owner_display_name = _issue_owner_context(session, issue)
            if owner_id is None:
                raise ValidationError("This issue is not linked to a product owner")
            issue.status = IssueStatus.OPEN
            issue.assignee_id = owner_id
            action_summary["returned_to_owner_at"] = datetime.utcnow().isoformat()
            action_summary["returned_to_owner_by"] = actor_user_id
            action_summary["returned_to_owner_notes"] = notes or ""
            created_notifications.extend(
                _create_issue_notifications(
                    session,
                    issue,
                    actor_user_id,
                    notif_type="issue_returned_to_owner",
                    title=f"Issue Returned to Owner: {issue.title}",
                    message=(
                        f"{actor_username} returned issue #{issue.id} to "
                        f"{owner_display_name or owner_username or f'user #{owner_id}'}."
                        + (f"\n\nNotes: {notes}" if notes else "")
                    ),
                    extra_user_ids=[owner_id],
                )
            )
        elif action == IssueActionType.RESOLVE:
            if not (final_conclusion or "").strip():
                raise ValidationError("final_conclusion is required when resolving an issue")
            issue.status = IssueStatus.RESOLVED
            issue.resolved_at = datetime.utcnow()
            issue.resolution_summary_json = {
                "kind": IssueActionType.RESOLVE,
                "scenario_name": selected_scenario_name,
                "scenario_type": selected_scenario_type,
                "actions_taken": _coerce_string_list(actions_taken),
                "verification_notes": verification_notes or "",
                "final_conclusion": final_conclusion or "",
            }
            issue.resolution_description = _build_resolution_text(issue.resolution_summary_json)
            created_notifications.extend(
                _create_issue_notifications(
                    session,
                    issue,
                    actor_user_id,
                    notif_type="issue_resolved",
                    title=f"Issue Resolved: {issue.title}",
                    message=f"{actor_username} resolved issue #{issue.id}.",
                )
            )
        elif action == IssueActionType.FALSE_POSITIVE:
            reason = (notes or final_conclusion or "").strip()
            if not reason:
                raise ValidationError("notes is required when marking false positive")
            issue.status = IssueStatus.FALSE_POSITIVE
            issue.resolved_at = datetime.utcnow()
            issue.resolution_summary_json = {
                "kind": IssueActionType.FALSE_POSITIVE,
                "reason": reason,
            }
            issue.resolution_description = _build_resolution_text(issue.resolution_summary_json, reason)
            created_notifications.extend(
                _create_issue_notifications(
                    session,
                    issue,
                    actor_user_id,
                    notif_type="issue_false_positive",
                    title=f"Issue Marked False Positive: {issue.title}",
                    message=f"{actor_username} marked issue #{issue.id} as false positive.",
                )
            )

        issue.action_summary_json = action_summary
        flag_modified(issue, "action_summary_json")
        if issue.resolution_summary_json is not None:
            flag_modified(issue, "resolution_summary_json")
        issue.updated_at = datetime.utcnow()

        session.commit()
        session.refresh(issue)

        # Resolved knowledge feeds the agent's retrieval index (best-effort —
        # the startup backfill catches anything this misses).
        if action in (IssueActionType.RESOLVE, IssueActionType.FALSE_POSITIVE):
            try:
                from core.agent import retrieval

                retrieval.index_issue(issue)
            except Exception:
                logger.opt(exception=True).warning(
                    "resolution FTS index skipped for issue {}", issue.id)

        notification_audits = [
            (notif.id, serialize_notification(notif))
            for notif in created_notifications
            if notif.id is not None
        ]

        log_audit(
            user_id=actor_user_id,
            action=action,
            entity_type="issue",
            entity_id=issue.id,
            old_value=old_val,
            new_value=serialize_issue(issue),
        )
        for notif_id, notif_payload in notification_audits:
            log_audit(
                user_id=actor_user_id,
                action="create",
                entity_type="notification",
                entity_id=notif_id,
                new_value=notif_payload,
            )

        return issue
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _issue_project_id(session, issue: Issue) -> Optional[int]:
    """Resolve the project an issue belongs to — directly for project-scoped
    issues, else via its product. Returns None when unresolvable."""
    if issue.project_id:
        return issue.project_id
    if issue.product_id:
        product = session.query(Product).filter(Product.id == issue.product_id).first()
        return product.project_id if product else None
    return None


def claim_issues(
    *,
    issue_ids: list[int],
    actor_user_id: int,
    actor_username: str,
    is_elevated: bool,
) -> Tuple[list[Issue], list[dict]]:
    """Self-assign one or more OPEN issues to the acting user.

    Two tiers may claim:
    - **Elevated** (global admin / relayops_member): any open issue across the
      platform — the platform-wide Ops triage board.
    - **Project-level Ops members**: only open issues belonging to a project
      where they hold the ``relayops_member`` role. This gives a global
      regular_user real operational reach over the projects they've been
      appointed to, without widening their global role.

    Partial success is intentional: a bulk claim never aborts on a single
    bad id. Each issue that cannot be claimed is returned in ``skipped``
    with a human-readable reason. handover_review issues are never
    claimable here (they go through /approve or /reject), and only OPEN
    issues can be claimed — anything already resolved/closed is skipped.

    Returns ``(claimed_issues, skipped)`` where ``skipped`` is a list of
    ``{"issue_id": int, "reason": str}``.
    """
    from core.services.support_group_service import user_project_relayops_member_ids

    db = _get_db()
    session = db.get_session()
    claimed: list[Issue] = []
    skipped: list[dict] = []
    audit_payloads: list[tuple[int, dict, dict]] = []
    try:
        # Non-elevated actors may only claim within projects they Ops-own.
        relayops_project_ids: set[int] = set()
        if not is_elevated:
            relayops_project_ids = user_project_relayops_member_ids(session, actor_user_id)
            if not relayops_project_ids:
                raise ForbiddenError(
                    "Only Ops members (global or project-level) can claim issues"
                )

        for issue_id in issue_ids:
            issue = session.query(Issue).filter(Issue.id == issue_id).first()
            if issue is None:
                skipped.append({"issue_id": issue_id, "reason": "Issue not found"})
                continue
            if issue.type == IssueType.HANDOVER_REVIEW:
                skipped.append({"issue_id": issue_id, "reason": "Handover reviews cannot be claimed"})
                continue
            if issue.status != IssueStatus.OPEN:
                skipped.append({"issue_id": issue_id, "reason": f"Issue is {issue.status}, not open"})
                continue
            if issue.assignee_id == actor_user_id:
                skipped.append({"issue_id": issue_id, "reason": "Already assigned to you"})
                continue
            if not is_elevated and _issue_project_id(session, issue) not in relayops_project_ids:
                skipped.append({"issue_id": issue_id, "reason": "Not a Ops member of this issue's project"})
                continue

            old_val = serialize_issue(issue)
            issue.assignee_id = actor_user_id
            issue.assigned_via = "manual"
            issue.updated_at = datetime.utcnow()
            claimed.append(issue)
            audit_payloads.append((issue.id, old_val, {}))

        session.commit()
        for issue in claimed:
            session.refresh(issue)
        # Serialize new values after refresh so the audit reflects committed state.
        new_values = {issue.id: serialize_issue(issue) for issue in claimed}
        for issue_id, old_val, _ in audit_payloads:
            log_audit(
                user_id=actor_user_id,
                action="update",
                entity_type="issue",
                entity_id=issue_id,
                old_value=old_val,
                new_value=new_values.get(issue_id),
            )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    logger.info(
        "User {} claimed {} issue(s); {} skipped",
        actor_username, len(claimed), len(skipped),
    )
    return claimed, skipped


def update_issue(
    *,
    issue_id: int,
    actor_user_id: int,
    actor_username: str,
    is_admin: bool,
    is_elevated: Optional[bool] = None,
    new_status: Optional[str] = None,
    new_assignee_id: Optional[int] = None,
    resolution_description: Optional[str] = None,
) -> Issue:
    """Update an issue (status / assignee / resolution). Raises domain exceptions on failure.

    ``is_admin`` is the *platform-admin* tier (gates reassignment and skipping
    the resolution note). ``is_elevated`` is the wider see-all / act-as-owner
    tier (admin OR relayops_member) and only gates acting on an issue the actor is
    not assigned to. When ``is_elevated`` is omitted it defaults to ``is_admin``
    so existing callers keep their previous behaviour.
    """
    if is_elevated is None:
        is_elevated = is_admin
    db = _get_db()
    session = db.get_session()
    # Flipped on when the assignee actually changes, so we email the new
    # on-duty member after commit (mirrors create_issue's post-commit dispatch).
    reassigned = False
    try:
        created_notifications: list[Notification] = []
        issue = session.query(Issue).filter(Issue.id == issue_id).first()
        if issue is None:
            raise NotFoundError("Issue not found")

        if not is_elevated and issue.assignee_id != actor_user_id:
            raise ForbiddenError("Not authorized")

        old_val = serialize_issue(issue)
        old_status = issue.status

        if issue.type == IssueType.HANDOVER_REVIEW:
            if new_status is not None or resolution_description is not None:
                raise ValidationError(
                    "handover_review issues must be handled via /approve or /reject endpoints"
                )

        if new_status is not None:
            if new_status not in IssueStatus.ALL:
                raise ValidationError(f"Invalid status. Must be one of: {IssueStatus.ALL}")

            if new_status in (IssueStatus.RESOLVED, IssueStatus.CLOSED, IssueStatus.FALSE_POSITIVE):
                if new_status == IssueStatus.FALSE_POSITIVE and not resolution_description:
                    raise ValidationError("resolution_description is required when resolving an issue")
                if new_status in (IssueStatus.RESOLVED, IssueStatus.CLOSED) and not resolution_description and not is_admin:
                    raise ValidationError("resolution_description is required when resolving an issue")
                issue.resolved_at = datetime.utcnow()

            issue.status = new_status

        if new_assignee_id is not None:
            if not is_admin:
                raise ForbiddenError("Only Platform Admins can reassign issues")

            target_user = session.query(User).filter(User.id == new_assignee_id).first()
            if target_user is None:
                raise NotFoundError("Assignee user not found")
            if target_user.role not in (UserRole.ADMIN, UserRole.RELAYOPS_MEMBER):
                raise ValidationError("Admin can only assign issues to Ops members or other admins")
            issue.assignee_id = new_assignee_id
            issue.assigned_via = "manual"

            if old_val.get("assignee_id") != new_assignee_id:
                reassigned = True
                notif = Notification(
                    user_id=new_assignee_id,
                    title=f"Issue Reassigned: {issue.title}",
                    message=f"Issue #{issue.id} was reassigned to you by {actor_username}.",
                    type="issue_reassigned",
                    related_entity_type="issue",
                    related_entity_id=issue.id,
                )
                session.add(notif)
                created_notifications.append(notif)

        if resolution_description is not None:
            issue.resolution_description = resolution_description

        issue.updated_at = datetime.utcnow()

        if (
            new_status in (IssueStatus.RESOLVED, IssueStatus.CLOSED, IssueStatus.FALSE_POSITIVE)
            and old_status not in (IssueStatus.RESOLVED, IssueStatus.CLOSED, IssueStatus.FALSE_POSITIVE)
        ):
            config = get_config()
            for admin_username in config.platform_owners:
                admin_user = session.query(User).filter(User.username == admin_username).first()
                if admin_user and admin_user.id != actor_user_id:
                    is_false_positive = new_status == IssueStatus.FALSE_POSITIVE
                    notif = Notification(
                        user_id=admin_user.id,
                        title=(
                            f"Issue Marked False Positive: {issue.title}"
                            if is_false_positive
                            else f"Issue Resolved: {issue.title}"
                        ),
                        message=(
                            f"Issue #{issue.id} was marked as false positive by {actor_username}."
                            if is_false_positive
                            else f"Issue #{issue.id} was resolved by {actor_username}."
                        ) + (f"\n\nResolution: {resolution_description}" if resolution_description else ""),
                        type="issue_false_positive" if is_false_positive else "issue_resolved",
                        related_entity_type="issue",
                        related_entity_id=issue.id,
                    )
                    session.add(notif)
                    created_notifications.append(notif)

            created_notifications.extend(
                _create_issue_notifications(
                    session,
                    issue,
                    actor_user_id,
                    notif_type="issue_resolved" if new_status != IssueStatus.FALSE_POSITIVE else "issue_false_positive",
                    title=(
                        f"Issue Marked False Positive: {issue.title}"
                        if new_status == IssueStatus.FALSE_POSITIVE else
                        f"Issue Resolved: {issue.title}"
                    ),
                    message=(
                        f"Issue #{issue.id} was marked as false positive by {actor_username}."
                        if new_status == IssueStatus.FALSE_POSITIVE else
                        f"Issue #{issue.id} was resolved by {actor_username}."
                    ) + (f"\n\nResolution: {resolution_description}" if resolution_description else ""),
                )
            )

        session.commit()
        session.refresh(issue)
        notification_audits = [
            (notif.id, serialize_notification(notif))
            for notif in created_notifications
            if notif.id is not None
        ]

        log_audit(
            user_id=actor_user_id,
            action="update",
            entity_type="issue",
            entity_id=issue.id,
            old_value=old_val,
            new_value=serialize_issue(issue),
        )
        for notif_id, notif_payload in notification_audits:
            log_audit(
                user_id=actor_user_id,
                action="create",
                entity_type="notification",
                entity_id=notif_id,
                new_value=notif_payload,
            )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    if reassigned:
        # Email the new on-duty assignee AFTER commit + connection release so
        # the bounded send never holds this request's DB connection (mirrors
        # create_issue). dispatch_issue_email no-ops for non-ops issue types.
        from core.issue_management.email_dispatch import dispatch_issue_email
        dispatch_issue_email(issue)

    return issue
