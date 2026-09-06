"""Audit log routes.

Endpoints:
    GET /api/audit-logs                    - Raw audit log list
    GET /api/audit-logs/issues            - Issue-centric audit summaries
    GET /api/audit-logs/issues/{issue_id} - Issue audit timeline
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.query_filters import parse_filter_values
from api.schema import (
    AuditLogResponse,
    ExportPayloadResponse,
    IssueAuditSummaryResponse,
    IssueAuditTimelineEventResponse,
)
from core.config import get_config
from core.exceptions import NotFoundError
from core.logging import get_logger
from core.models.database import Database, get_db
from core.models.entities import AuditLog, Issue, Product, ProductVersion, Project, ProjectMember
from core.services.support_group_service import user_has_project_group_access
from core.services.user_service import get_user_by_id

logger = get_logger(__name__)
router = APIRouter(tags=["audit-logs"])


def _is_platform_admin(username: str) -> bool:
    config = get_config()
    return username in config.platform_owners


def _enrich_audit_log(log: AuditLog, db: Database) -> dict:
    """Attach username to an audit log entry."""
    result = {
        "id": log.id,
        "user_id": log.user_id,
        "action": log.action,
        "entity_type": log.entity_type,
        "entity_id": log.entity_id,
        "old_value": log.old_value,
        "new_value": log.new_value,
        "timestamp": log.timestamp,
        "username": None,
        "display_name": None,
    }
    if log.user_id:
        user = get_user_by_id(db, log.user_id)
        if user:
            result["username"] = user.username
            result["display_name"] = user.display_name
    return result


def _build_accessible_issue_query(session, current_user: CurrentUser, is_admin: bool):
    """Build a query for issues visible to the current user."""
    query = session.query(Issue)
    if is_admin:
        return query

    owned_ids = [p.id for p in session.query(Project.id).filter(Project.owner_id == current_user.user_id).all()]
    member_ids = [m.project_id for m in session.query(ProjectMember.project_id).filter(ProjectMember.user_id == current_user.user_id).all()]
    group_ids = [
        project.id
        for project in session.query(Project).all()
        if user_has_project_group_access(session, project, current_user.groups)
    ]
    my_project_ids = list(set(owned_ids + member_ids + group_ids))
    my_product_ids = (
        [p.id for p in session.query(Product.id).filter(Product.project_id.in_(my_project_ids)).all()]
        if my_project_ids else []
    )

    conditions = [
        Issue.created_by == current_user.user_id,
        Issue.assignee_id == current_user.user_id,
    ]
    if my_product_ids:
        conditions.append(Issue.product_id.in_(my_product_ids))
    from sqlalchemy import or_
    query = query.filter(or_(*conditions))
    return query


def _format_user_name(user) -> Optional[str]:
    if not user:
        return None
    return user.display_name or user.username


def _derive_issue_event_label(log: AuditLog) -> str:
    old_value = log.old_value or {}
    new_value = log.new_value or {}

    if log.action == "create":
        return "Issue Created"
    if log.action == "email_dispatch":
        return "Email Sent" if new_value.get("email_status") == "sent" else "Email Failed"
    if log.action == "auto_close":
        return "Issue Auto-Closed"
    if log.action == "transition":
        return "Status Advanced"
    if log.action == "start_working":
        return "Work Started"
    if log.action == "record_step":
        return "Step Recorded"
    if log.action == "verification_passed":
        return "Verification Recorded"
    if log.action == "escalate":
        return "Issue Escalated"
    if log.action == "return_to_owner":
        return "Returned to Product Owner"
    if log.action == "resolve":
        return "Issue Resolved"
    if log.action == "false_positive":
        return "Marked False Positive"
    if log.action == "approve":
        return "Handover Approved"
    if log.action == "reject":
        return "Handover Rejected"
    if log.action == "handover":
        return "Handover Submitted"

    if log.action == "update":
        old_status = old_value.get("status")
        new_status = new_value.get("status")
        old_assignee = old_value.get("assignee_id")
        new_assignee = new_value.get("assignee_id")

        if old_status != new_status:
            if new_status == "in_progress":
                return "Work Started"
            if new_status == "resolved":
                return "Issue Resolved"
            if new_status == "closed":
                return "Issue Closed"
            if new_status == "false_positive":
                return "Marked False Positive"
            return f"Status Changed to {str(new_status).replace('_', ' ').title()}"

        if old_assignee != new_assignee:
            return "Issue Assigned" if not old_assignee else "Issue Reassigned"

        return "Issue Updated"

    return log.action.replace("_", " ").title()


def _format_user_ref(db: Database, user_id) -> str:
    """Render a user as ``Display Name (username)`` for human-readable timelines.

    The username (``username``, e.g. ``demo003``) is what people recognise, so we
    surface it alongside the display name instead of the opaque internal row id.
    Falls back to ``user #N`` only when the row can't be resolved.
    """
    if user_id is None:
        return "unassigned"
    user = get_user_by_id(db, int(user_id))
    if user is None:
        return f"user #{user_id}"
    name = user.display_name or user.username
    if name and user.username and user.username != name:
        return f"{name} ({user.username})"
    return name or f"user #{user_id}"


def _derive_issue_event_summary(log: AuditLog, db: Database) -> str:
    old_value = log.old_value or {}
    new_value = log.new_value or {}
    version_number = new_value.get("product_version_number") or old_value.get("product_version_number")
    version_suffix = f" for version v{version_number}" if version_number is not None else ""
    support_group_name = new_value.get("support_group_name") or old_value.get("support_group_name")

    if log.action == "create":
        assignee_id = new_value.get("assignee_id")
        if support_group_name and assignee_id:
            return f"Issue opened{version_suffix}, routed to group '{support_group_name}', and assigned to {_format_user_ref(db, assignee_id)}."
        if support_group_name:
            return f"Issue opened{version_suffix} and routed to group '{support_group_name}'."
        return f"Issue opened{version_suffix} and assigned to {_format_user_ref(db, assignee_id)}." if assignee_id else f"Issue opened{version_suffix}."
    if log.action == "email_dispatch":
        assignee_ref = _format_user_ref(db, new_value.get("assignee_id"))
        to_address = new_value.get("to_address")
        if new_value.get("email_status") == "sent":
            at_addr = f" at {to_address}" if to_address else ""
            return f"Notification email delivered to the assigned member {assignee_ref}{at_addr}."
        return f"Notification email to the assigned member {assignee_ref} could not be delivered."
    if log.action == "auto_close":
        return new_value.get("resolution_description") or "Issue was auto-closed after the system detected recovery."
    if log.action == "transition":
        old_type = (old_value.get("type") or "").replace("_", " ").strip()
        new_type = (new_value.get("type") or "").replace("_", " ").strip()
        if old_type and new_type:
            return f"Advanced from {old_type} to {new_type} — same ticket updated in place."
        return "Issue advanced to a new stage."
    if log.action == "start_working":
        scenario = new_value.get("selected_scenario_name")
        return (
            f"Assignee acknowledged the issue and selected scenario '{scenario}'."
            if scenario else
            "Assignee acknowledged and started working on the issue."
        )
    if log.action == "record_step":
        action_summary = new_value.get("action_summary_json") or {}
        steps = action_summary.get("steps") or []
        latest_step = steps[-1] if steps else {}
        title = latest_step.get("title") or "Execution step"
        notes = latest_step.get("notes") or ""
        return f"{title}: {notes}".strip(": ")
    if log.action == "verification_passed":
        action_summary = new_value.get("action_summary_json") or {}
        verifications = action_summary.get("verifications") or []
        latest_verification = verifications[-1] if verifications else {}
        return latest_verification.get("notes") or "Verification evidence was recorded."
    if log.action == "escalate":
        action_summary = new_value.get("action_summary_json") or {}
        escalations = action_summary.get("escalations") or []
        latest_escalation = escalations[-1] if escalations else {}
        target = latest_escalation.get("target")
        notes = latest_escalation.get("notes") or ""
        summary = f"Escalated to {target}." if target else "Issue was escalated."
        if notes:
            summary += f" Notes: {notes}"
        return summary
    if log.action == "return_to_owner":
        action_summary = new_value.get("action_summary_json") or {}
        notes = action_summary.get("returned_to_owner_notes") or ""
        return (
            f"Issue was returned to the product owner. Notes: {notes}"
            if notes else
            "Issue was returned to the product owner for further action."
        )
    if log.action == "resolve":
        return new_value.get("resolution_description") or "Issue was resolved."
    if log.action == "false_positive":
        return new_value.get("resolution_description") or "Issue was marked as a false positive."
    if log.action == "approve":
        return f"Review completed and approved{version_suffix}."
    if log.action == "reject":
        return new_value.get("rejection_reason") or f"Review completed and rejected{version_suffix}."
    if log.action == "handover":
        return f"Handover review was submitted{version_suffix}."

    if log.action == "update":
        old_status = old_value.get("status")
        new_status = new_value.get("status")
        old_assignee = old_value.get("assignee_id")
        new_assignee = new_value.get("assignee_id")
        old_group = old_value.get("support_group_name")
        new_group = new_value.get("support_group_name")

        if old_status != new_status:
            if new_status == "in_progress":
                return "Assignee acknowledged and started working on the issue."
            if new_status == "resolved":
                return new_value.get("resolution_description") or "Issue was resolved."
            if new_status == "closed":
                return new_value.get("resolution_description") or "Issue was closed."
            if new_status == "false_positive":
                return new_value.get("resolution_description") or "Issue was marked as a false positive."
            return f"Status changed from {old_status} to {new_status}."

        if old_assignee != new_assignee:
            if old_assignee is None:
                return f"Issue assigned to {_format_user_ref(db, new_assignee)}."
            return f"Issue reassigned from {_format_user_ref(db, old_assignee)} to {_format_user_ref(db, new_assignee)}."

        if old_group != new_group and new_group:
            if old_group:
                return f"Issue routing moved from group '{old_group}' to '{new_group}'."
            return f"Issue routed to group '{new_group}'."

        return "Issue metadata was updated."

    return "Issue audit event recorded."


# Notification types that fan out to people who aren't actioning the issue —
# specifically the product owner / "manager", who isn't on the duty roster.
# These are noise on the issue timeline, so they're hidden from it; the admin
# alert (system_alert) and the assignee's own events are kept.
_TIMELINE_HIDDEN_NOTIFICATION_TYPES = {"issue_created"}


def _derive_notification_label(log: AuditLog) -> str:
    notif_type = (log.new_value or {}).get("type")
    mapping = {
        "issue_assigned": "Notification Sent",
        "system_alert": "Notification Sent",
        "issue_resolved": "Notification Sent",
        "issue_false_positive": "Notification Sent",
        "issue_auto_closed": "Notification Sent",
        "issue_reassigned": "Notification Sent",
        "issue_transitioned": "Notification Sent",
        "issue_created": "Notification Sent",
        "issue_started": "Notification Sent",
        "issue_escalated": "Notification Sent",
        "issue_returned_to_owner": "Notification Sent",
        "sla_overdue": "Notification Sent",
        "duty_started": "Notification Sent",
    }
    return mapping.get(str(notif_type), "Notification Sent")


def _derive_notification_summary(log: AuditLog, db: Database) -> str:
    new_value = log.new_value or {}
    recipient_id = new_value.get("user_id")
    title = new_value.get("title")
    notif_type = new_value.get("type")
    label = title or str(notif_type or "notification").replace("_", " ")
    return f"Delivered '{label}' to {_format_user_ref(db, recipient_id)}."


def _build_issue_timeline_event(log: AuditLog, db: Database) -> Dict[str, Any]:
    actor = get_user_by_id(db, log.user_id) if log.user_id else None
    payload = log.new_value or log.old_value or {}
    recipient = None
    recipient_id = None
    is_notification = log.entity_type == "notification"
    if is_notification:
        recipient_id = payload.get("user_id")
        if recipient_id:
            recipient = get_user_by_id(db, int(recipient_id))

    return {
        "audit_log_id": log.id,
        "timestamp": log.timestamp,
        "action": log.action,
        "entity_type": log.entity_type,
        "label": _derive_notification_label(log) if is_notification else _derive_issue_event_label(log),
        "summary": _derive_notification_summary(log, db) if is_notification else _derive_issue_event_summary(log, db),
        "actor_user_id": log.user_id,
        "actor_username": actor.username if actor else None,
        "actor_display_name": actor.display_name if actor else None,
        "recipient_user_id": int(recipient_id) if recipient_id is not None else None,
        "recipient_username": recipient.username if recipient else None,
        "recipient_display_name": recipient.display_name if recipient else None,
        "is_notification": is_notification,
        "details": payload,
    }


def _load_issue_context(session, issue: Issue) -> Dict[str, Any]:
    product = session.query(Product).filter(Product.id == issue.product_id).first() if issue.product_id else None
    version = session.query(ProductVersion).filter(ProductVersion.id == issue.product_version_id).first() if issue.product_version_id else None
    project = session.query(Project).filter(Project.id == product.project_id).first() if product else None
    assignee = get_user_by_id(get_db(), issue.assignee_id) if issue.assignee_id else None
    return {
        "project_id": project.id if project else None,
        "project_name": project.name if project else None,
        "project_is_system": bool(project.is_system) if project else False,
        "product_id": product.id if product else None,
        "product_version_id": version.id if version else None,
        "product_version_number": version.version_number if version else None,
        "product_version_status": version.version_status if version else None,
        "product_name": product.name if product else None,
        "product_is_system": bool(product.is_system) if product else False,
        "support_group_id": issue.support_group_id,
        "support_group_name": issue.support_group_name or None,
        "owner_group_id": issue.owner_group_id,
        "owner_group_name": issue.owner_group_name or None,
        "assignee_id": issue.assignee_id,
        "assignee_username": assignee.username if assignee else None,
        "assignee_display_name": assignee.display_name if assignee else None,
    }


@router.get("/api/audit-logs", response_model=List[AuditLogResponse])
def list_audit_logs(
    entity_type: Optional[str] = Query(None, description="Filter by entity type"),
    entity_id: Optional[int] = Query(None, description="Filter by entity ID"),
    user_id: Optional[int] = Query(None, description="Filter by user ID"),
    action: Optional[str] = Query(None, description="Filter by action"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    is_admin = _is_platform_admin(current_user.username)
    query = session.query(AuditLog)

    if not is_admin:
        from sqlalchemy import or_

        owned_ids = [p.id for p in session.query(Project.id).filter(Project.owner_id == current_user.user_id).all()]
        member_ids = [m.project_id for m in session.query(ProjectMember.project_id).filter(ProjectMember.user_id == current_user.user_id).all()]
        group_ids = [
            project.id
            for project in session.query(Project).all()
            if user_has_project_group_access(session, project, current_user.groups)
        ]
        my_project_ids = list(set(owned_ids + member_ids + group_ids))
        my_product_ids = (
            [p.id for p in session.query(Product.id).filter(Product.project_id.in_(my_project_ids)).all()]
            if my_project_ids else []
        )
        conditions = [AuditLog.user_id == current_user.user_id]
        if my_project_ids:
            conditions.append((AuditLog.entity_type == "project") & (AuditLog.entity_id.in_(my_project_ids)))
        if my_product_ids:
            conditions.append((AuditLog.entity_type == "product") & (AuditLog.entity_id.in_(my_product_ids)))
        query = query.filter(or_(*conditions))

    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        query = query.filter(AuditLog.entity_id == entity_id)
    if user_id is not None and is_admin:
        query = query.filter(AuditLog.user_id == user_id)
    if action:
        query = query.filter(AuditLog.action == action)

    logs = query.order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit).all()
    return [_enrich_audit_log(log, db) for log in logs]


@router.get("/api/audit-logs/issues", response_model=List[IssueAuditSummaryResponse])
def list_issue_audit_summaries(
    status: Optional[str] = Query(None, description="Filter by issue status; comma-separated for multiple"),
    issue_type: Optional[str] = Query(None, description="Filter by issue type; comma-separated for multiple"),
    search: Optional[str] = Query(None, description="Search issue, project, product, owner, or latest event"),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    is_admin = _is_platform_admin(current_user.username)
    query = _build_accessible_issue_query(session, current_user, is_admin)
    status_values = parse_filter_values(status)
    type_values = parse_filter_values(issue_type)
    if status_values:
        query = query.filter(Issue.status.in_(status_values))
    if type_values:
        query = query.filter(Issue.type.in_(type_values))

    issues = query.order_by(Issue.updated_at.desc(), Issue.created_at.desc()).limit(limit).all()
    issue_ids = [issue.id for issue in issues]
    logs_by_issue: Dict[int, List[AuditLog]] = {issue_id: [] for issue_id in issue_ids}

    if issue_ids:
        raw_logs = (
            session.query(AuditLog)
            .filter(
                ((AuditLog.entity_type == "issue") & (AuditLog.entity_id.in_(issue_ids)))
                | (AuditLog.entity_type == "notification")
            )
            .order_by(AuditLog.timestamp.asc())
            .all()
        )
        for log in raw_logs:
            related_issue_id = log.entity_id
            if log.entity_type == "notification":
                related_issue_id = (log.new_value or {}).get("related_entity_id")
                if (log.new_value or {}).get("related_entity_type") != "issue":
                    continue
            if related_issue_id in logs_by_issue:
                logs_by_issue[int(related_issue_id)].append(log)

    summaries = []
    for issue in issues:
        timeline = [_build_issue_timeline_event(log, db) for log in logs_by_issue.get(issue.id, [])]
        lifecycle_events = [event for event in timeline if not event["is_notification"]]
        latest_event = lifecycle_events[-1] if lifecycle_events else {
            "timestamp": issue.updated_at or issue.created_at,
            "label": "Issue Updated",
        }
        context = _load_issue_context(session, issue)
        summaries.append({
            "issue_id": issue.id,
            "issue_type": issue.type,
            "issue_status": issue.status,
            "issue_title": issue.title,
            **context,
            "created_at": issue.created_at,
            "updated_at": issue.updated_at,
            "latest_event_at": latest_event["timestamp"],
            "latest_event_label": latest_event["label"],
            "event_count": len(lifecycle_events),
            "notification_count": sum(1 for event in timeline if event["is_notification"]),
        })

    search_query = (search or "").strip().lower()
    if search_query:
        summaries = [
            s for s in summaries
            if search_query in " ".join(filter(None, [
                str(s["issue_id"]), s["issue_title"], s.get("project_name"), s.get("product_name"),
                s.get("assignee_display_name"), s.get("assignee_username"),
                s.get("latest_event_label"), s.get("issue_type"), s.get("issue_status"),
            ])).lower()
        ]
    return summaries


@router.get("/api/audit-logs/export", response_model=ExportPayloadResponse)
def export_audit_logs(
    status: Optional[str] = Query(None, description="Filter by issue status; comma-separated for multiple"),
    issue_type: Optional[str] = Query(None, description="Filter by issue type; comma-separated for multiple"),
    search: Optional[str] = Query(None, description="Search issue, project, product, owner, or latest event"),
    limit: int = Query(300, ge=1, le=1000),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    is_admin = _is_platform_admin(current_user.username)
    query = _build_accessible_issue_query(session, current_user, is_admin)
    status_values = parse_filter_values(status)
    type_values = parse_filter_values(issue_type)
    if status_values:
        query = query.filter(Issue.status.in_(status_values))
    if type_values:
        query = query.filter(Issue.type.in_(type_values))

    issues = query.order_by(Issue.updated_at.desc(), Issue.created_at.desc()).limit(limit).all()
    issue_ids = [issue.id for issue in issues]
    logs_by_issue: Dict[int, List[AuditLog]] = {issue_id: [] for issue_id in issue_ids}
    raw_logs = []
    if issue_ids:
        raw_logs = (
            session.query(AuditLog)
            .filter(
                ((AuditLog.entity_type == "issue") & (AuditLog.entity_id.in_(issue_ids)))
                | (AuditLog.entity_type == "notification")
            )
            .order_by(AuditLog.timestamp.asc())
            .all()
        )

    for log in raw_logs:
        related_issue_id = log.entity_id
        if log.entity_type == "notification":
            related_issue_id = (log.new_value or {}).get("related_entity_id")
            if (log.new_value or {}).get("related_entity_type") != "issue":
                continue
        if related_issue_id in logs_by_issue:
            logs_by_issue[int(related_issue_id)].append(log)

    summaries = []
    for issue in issues:
        timeline = [_build_issue_timeline_event(log, db) for log in logs_by_issue.get(issue.id, [])]
        lifecycle_events = [event for event in timeline if not event["is_notification"]]
        latest_event = lifecycle_events[-1] if lifecycle_events else {
            "timestamp": issue.updated_at or issue.created_at,
            "label": "Issue Updated",
        }
        context = _load_issue_context(session, issue)
        summaries.append({
            "issue_id": issue.id,
            "issue_type": issue.type,
            "issue_status": issue.status,
            "issue_title": issue.title,
            **context,
            "created_at": issue.created_at,
            "updated_at": issue.updated_at,
            "latest_event_at": latest_event["timestamp"],
            "latest_event_label": latest_event["label"],
            "event_count": len(lifecycle_events),
            "notification_count": sum(1 for event in timeline if event["is_notification"]),
        })

    search_query = (search or "").strip().lower()
    if search_query:
        summaries = [
            s for s in summaries
            if search_query in " ".join(filter(None, [
                str(s["issue_id"]), s["issue_title"], s.get("project_name"), s.get("product_name"),
                s.get("assignee_display_name"), s.get("assignee_username"),
                s.get("latest_event_label"), s.get("issue_type"), s.get("issue_status"),
            ])).lower()
        ]
    visible_issue_ids = {s["issue_id"] for s in summaries}
    linked_logs = [
        _enrich_audit_log(log, db)
        for log in raw_logs
        if (
            (log.entity_type == "issue" and log.entity_id in visible_issue_ids)
            or (
                log.entity_type == "notification"
                and (log.new_value or {}).get("related_entity_type") == "issue"
                and (log.new_value or {}).get("related_entity_id") in visible_issue_ids
            )
        )
    ]
    return ExportPayloadResponse(
        export_type="audit",
        generated_at=datetime.utcnow(),
        filters={"status": status, "issue_type": issue_type, "search": search, "limit": limit},
        data={"issue_summaries": summaries, "audit_logs": linked_logs},
    )


def _merge_admin_notifications(events: List[Dict[str, Any]], db: Database) -> List[Dict[str, Any]]:
    """Collapse a fan-out of notifications to multiple platform admins into one
    timeline line, leaving every other event (assignee, etc.) untouched.

    Resolution / SLA-overdue flows notify *each* platform owner, so the timeline
    used to show one "Notification Sent to <admin>" line per owner. We group
    those by (title, second) and emit a single line — keeping the assignee's own
    notification separate even if that assignee happens to be an admin.
    """
    from core.config import get_config

    owners = {str(o).lower() for o in get_config().platform_owners}

    def is_admin_notif(e: Dict[str, Any]) -> bool:
        if not e.get("is_notification"):
            return False
        if (e.get("details") or {}).get("type") in ("issue_assigned", "issue_reassigned"):
            return False  # the assignee's own line always stays
        return (e.get("recipient_username") or "").lower() in owners

    groups: Dict[tuple, List[int]] = {}
    for idx, e in enumerate(events):
        if is_admin_notif(e):
            key = ((e.get("details") or {}).get("title"), str(e.get("timestamp"))[:19])
            groups.setdefault(key, []).append(idx)

    drop: set = set()
    relabel: Dict[int, int] = {}  # first-index -> distinct admin count
    for idxs in groups.values():
        first = idxs[0]
        drop.update(idxs[1:])  # keep only the first occurrence of each batch
        distinct = {events[i].get("recipient_user_id") for i in idxs}
        if len(distinct) > 1:
            relabel[first] = len(distinct)

    out: List[Dict[str, Any]] = []
    for idx, e in enumerate(events):
        if idx in drop:
            continue
        if idx in relabel:
            e = dict(e)
            title = (e.get("details") or {}).get("title") or "notification"
            e["summary"] = f"Delivered '{title}' to {relabel[idx]} platform admins."
            e["recipient_user_id"] = None
            e["recipient_username"] = None
            e["recipient_display_name"] = None
        out.append(e)
    return out


@router.get("/api/audit-logs/issues/{issue_id}", response_model=List[IssueAuditTimelineEventResponse])
def get_issue_audit_timeline(
    issue_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    is_admin = _is_platform_admin(current_user.username)
    issue = _build_accessible_issue_query(session, current_user, is_admin).filter(Issue.id == issue_id).first()
    if not issue:
        raise NotFoundError("Issue not found")

    raw_logs = (
        session.query(AuditLog)
        .filter(
            ((AuditLog.entity_type == "issue") & (AuditLog.entity_id == issue_id))
            | (AuditLog.entity_type == "notification")
        )
        .order_by(AuditLog.timestamp.asc())
        .all()
    )
    logs = []
    for log in raw_logs:
        if log.entity_type == "notification":
            payload = log.new_value or {}
            if payload.get("related_entity_type") != "issue":
                continue
            if payload.get("related_entity_id") != issue_id:
                continue
            # Drop FYI notifications to the not-on-duty product owner/manager;
            # keep the admin alert and the assignee's notification.
            if payload.get("type") in _TIMELINE_HIDDEN_NOTIFICATION_TYPES:
                continue
        logs.append(log)
    events = [_build_issue_timeline_event(log, db) for log in logs]
    return _merge_admin_notifications(events, db)
