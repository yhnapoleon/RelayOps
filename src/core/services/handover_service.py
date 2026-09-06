"""Handover business logic.

Two responsibilities live here:
1. Completeness evaluators — `evaluate_product_handover_completeness` (per
   product, consumed by the product handover guide) and
   `evaluate_project_handover_completeness` (the project-wide aggregate that
   gates submission).
2. Workflow functions (`initiate_handover`, `submit_version`,
   `approve_version`, `reject_version`, `rollback_version`) — orchestrate the
   project-level ProjectVersion lifecycle and Issue creation/closure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from core.config import get_config
from core.exceptions import ConflictError, ForbiddenError, NotFoundError, SystemLockedError, ValidationError
from core.logging import get_logger
from core.models.entities import (
    Application,
    ApplicationRecoveryScenario,
    Issue,
    IssueStatus,
    IssueType,
    Job,
    JobFailureScenario,
    JobFailureScenarioType,
    Notification,
    Product,
    Project,
    ProjectVersion,
)
from core.models.database import get_db
from core.models.user import User
from core.services.audit_service import (
    log_audit,
    serialize_issue,
    serialize_project,
    serialize_project_version,
)

# project_version_service is imported lazily inside the workflow functions
# to break a circular import (project_version_service imports the
# completeness evaluator from this module).

logger = get_logger(__name__)


def _item(
    *,
    code: str,
    severity: str,
    entity_type: str,
    message: str,
    entity_id: int | None = None,
    entity_label: str | None = None,
    parent_entity_type: str | None = None,
    parent_entity_id: int | None = None,
    parent_entity_label: str | None = None,
    field_code: str | None = None,
) -> Dict[str, Any]:
    # ``parent_*`` and ``field_code`` let the UI render "in Job X" prefixes
    # and jump straight from a blocking item into the owning asset's edit form
    # with the offending field highlighted in red. They're optional so older
    # clients keep working with the legacy payload shape.
    return {
        "code": code,
        "severity": severity,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_label": entity_label,
        "parent_entity_type": parent_entity_type,
        "parent_entity_id": parent_entity_id,
        "parent_entity_label": parent_entity_label,
        "field_code": field_code,
        "message": message,
    }


def _has_steps(value: Any) -> bool:
    return isinstance(value, list) and any(str(step).strip() for step in value)


def evaluate_product_handover_completeness(session, product_id: int) -> Dict[str, Any]:
    """
    Evaluate whether a product is ready for handover.

    Returns a stable dict contract:
      {
        "is_complete": bool,
        "blocking_items": [...],
        "summary": {...}
      }
    """
    jobs: List[Job] = (
        session.query(Job)
        .filter(Job.product_id == product_id)
        .order_by(Job.id.asc())
        .all()
    )
    applications: List[Application] = (
        session.query(Application)
        .filter(Application.product_id == product_id)
        .order_by(Application.id.asc())
        .all()
    )

    blocking_items: List[Dict[str, Any]] = []

    if not jobs and not applications:
        blocking_items.append(_item(
            code="no_scope",
            severity="blocking",
            entity_type="product",
            entity_id=product_id,
            message="Product must have at least one job or application before handover.",
        ))

    total_job_scenarios = 0
    total_app_scenarios = 0

    for job in jobs:
        scenarios: List[JobFailureScenario] = (
            session.query(JobFailureScenario)
            .filter(JobFailureScenario.job_id == job.id, JobFailureScenario.is_active.is_(True))
            .all()
        )
        total_job_scenarios += len(scenarios)
        label = job.control_m_job_name or f"Job #{job.id}"

        if not scenarios:
            blocking_items.append(_item(
                code="job_missing_scenarios",
                severity="blocking",
                entity_type="job",
                entity_id=job.id,
                entity_label=label,
                message="Job has no active failure scenarios. Fill the scenario form or mark each preset as Don't apply.",
            ))
            continue

        scenario_types = {scenario.scenario_type for scenario in scenarios}
        for required_type, code in (
            (JobFailureScenarioType.NOT_TRIGGERED, "job_missing_not_triggered"),
            (JobFailureScenarioType.TRIGGERED_BUT_FAILED, "job_missing_triggered_failed"),
        ):
            if required_type not in scenario_types:
                blocking_items.append(_item(
                    code=code,
                    severity="blocking",
                    entity_type="job",
                    entity_id=job.id,
                    entity_label=label,
                    message=f"Missing suggested scenario type '{required_type}'.",
                ))

        if job.dependencies and not str(job.dependency_notes or "").strip():
            blocking_items.append(_item(
                code="job_missing_dependency_notes",
                severity="blocking",
                entity_type="job",
                entity_id=job.id,
                entity_label=label,
                field_code="dependency_notes",
                message="Jobs with dependencies should document dependency handling notes.",
            ))

        # Owner contact, scenario condition_description and scenario
        # escalation_target/fallback_owner_type were previously blocking but
        # downgraded per business request — they're now advisory only.

        for scenario in scenarios:
            scenario_label = scenario.scenario_name or f"Scenario #{scenario.id}"
            if bool(getattr(scenario, "is_not_applicable", False)):
                continue
            if not _has_steps(scenario.action_steps):
                blocking_items.append(_item(
                    code="job_scenario_missing_actions",
                    severity="blocking",
                    entity_type="job_failure_scenario",
                    entity_id=scenario.id,
                    entity_label=scenario_label,
                    parent_entity_type="job",
                    parent_entity_id=job.id,
                    parent_entity_label=label,
                    field_code="action_steps",
                    message="Action steps are empty. Fill them or mark Don't apply.",
                ))
            if not _has_steps(scenario.verification_steps):
                blocking_items.append(_item(
                    code="job_scenario_missing_verification",
                    severity="blocking",
                    entity_type="job_failure_scenario",
                    entity_id=scenario.id,
                    entity_label=scenario_label,
                    parent_entity_type="job",
                    parent_entity_id=job.id,
                    parent_entity_label=label,
                    field_code="verification_steps",
                    message="Verification steps are empty. Fill them or mark Don't apply.",
                ))

    for app in applications:
        scenarios: List[ApplicationRecoveryScenario] = (
            session.query(ApplicationRecoveryScenario)
            .filter(ApplicationRecoveryScenario.application_id == app.id, ApplicationRecoveryScenario.is_active.is_(True))
            .all()
        )
        total_app_scenarios += len(scenarios)
        label = app.application_url or f"Application #{app.id}"

        if not scenarios:
            blocking_items.append(_item(
                code="app_missing_scenarios",
                severity="blocking",
                entity_type="application",
                entity_id=app.id,
                entity_label=label,
                message="Application has no active recovery scenarios. Fill the scenario form or mark each preset as Don't apply.",
            ))
            continue

        if app.restart_supported and not str(app.restart_summary or "").strip():
            blocking_items.append(_item(
                code="app_missing_restart_summary",
                severity="blocking",
                entity_type="application",
                entity_id=app.id,
                entity_label=label,
                field_code="restart_summary",
                message="Applications with restart support should document the restart summary.",
            ))

        # Owner contact, scenario condition_description and scenario
        # escalation_target/fallback_owner_type were previously blocking but
        # downgraded per business request — they're now advisory only.

        for scenario in scenarios:
            scenario_label = scenario.scenario_name or f"Scenario #{scenario.id}"
            if bool(getattr(scenario, "is_not_applicable", False)):
                continue
            if not _has_steps(scenario.action_steps):
                blocking_items.append(_item(
                    code="app_scenario_missing_actions",
                    severity="blocking",
                    entity_type="application_recovery_scenario",
                    entity_id=scenario.id,
                    entity_label=scenario_label,
                    parent_entity_type="application",
                    parent_entity_id=app.id,
                    parent_entity_label=label,
                    field_code="action_steps",
                    message="Action steps are empty. Fill them or mark Don't apply.",
                ))
            if not _has_steps(scenario.verification_steps):
                blocking_items.append(_item(
                    code="app_scenario_missing_verification",
                    severity="blocking",
                    entity_type="application_recovery_scenario",
                    entity_id=scenario.id,
                    entity_label=scenario_label,
                    parent_entity_type="application",
                    parent_entity_id=app.id,
                    parent_entity_label=label,
                    field_code="verification_steps",
                    message="Verification steps are empty. Fill them or mark Don't apply.",
                ))

    return {
        "is_complete": len(blocking_items) == 0,
        "blocking_items": blocking_items,
        "summary": {
            "job_count": len(jobs),
            "application_count": len(applications),
            "job_scenario_count": total_job_scenarios,
            "application_scenario_count": total_app_scenarios,
            "blocking_count": len(blocking_items),
        },
    }


def evaluate_project_handover_completeness(session, project_id: int) -> Dict[str, Any]:
    """
    Evaluate whether a whole project is ready for handover.

    Aggregates the per-product completeness checks across every product under
    the project. Each blocking item is annotated with the owning product so
    the UI can render "in Product X" context. Returns the same stable
    contract as the per-product evaluator.
    """
    products: List[Product] = (
        session.query(Product)
        .filter(Product.project_id == project_id)
        .order_by(Product.id.asc())
        .all()
    )

    blocking_items: List[Dict[str, Any]] = []
    job_count = application_count = job_scenario_count = application_scenario_count = 0

    if not products:
        blocking_items.append(_item(
            code="no_products",
            severity="blocking",
            entity_type="project",
            entity_id=project_id,
            message="Project must have at least one product before handover.",
        ))

    for product in products:
        result = evaluate_product_handover_completeness(session, product.id)
        summary = result.get("summary") or {}
        job_count += int(summary.get("job_count") or 0)
        application_count += int(summary.get("application_count") or 0)
        job_scenario_count += int(summary.get("job_scenario_count") or 0)
        application_scenario_count += int(summary.get("application_scenario_count") or 0)
        product_label = product.name or f"Product #{product.id}"
        for item in result.get("blocking_items") or []:
            enriched = dict(item)
            enriched["product_id"] = product.id
            enriched["product_label"] = product_label
            blocking_items.append(enriched)

    return {
        "is_complete": len(blocking_items) == 0,
        "blocking_items": blocking_items,
        "summary": {
            "product_count": len(products),
            "job_count": job_count,
            "application_count": application_count,
            "job_scenario_count": job_scenario_count,
            "application_scenario_count": application_scenario_count,
            "blocking_count": len(blocking_items),
        },
    }


# ── workflow: handover lifecycle ──────────────────────────────────────


def _get_db():
    return get_db()


def _find_platform_admin_assignee(session) -> Optional[int]:
    config = get_config()
    for owner_username in config.platform_owners:
        admin_user = session.query(User).filter(User.username == owner_username).first()
        if admin_user:
            return admin_user.id
    return None


def _require_project_access(
    session,
    *,
    project_id: int,
    actor_user_id: int,
    actor_role: str,
    require_owner: bool = True,
    actor_groups: Optional[list[str]] = None,
) -> Project:
    """Resolve project + assert actor permission. Raises domain exceptions on failure."""
    from core.models.entities import ProjectMember
    from core.models.user import UserRole
    from core.services.support_group_service import is_project_editor, user_has_project_group_access

    project = session.query(Project).filter(Project.id == project_id).first()
    if project is None:
        raise NotFoundError("Project not found")

    if actor_role == UserRole.ADMIN:
        return project
    if project.is_system == 1 and not require_owner:
        return project
    if project.owner_id == actor_user_id:
        return project
    if require_owner:
        # product_member is a project editor with owner-equivalent rights over
        # the project (incl. submitting handovers); member management and
        # ownership transfer remain owner-only.
        if is_project_editor(session, project_id, actor_user_id):
            return project
        raise ForbiddenError("Not authorized")

    is_member = (
        session.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == actor_user_id,
        )
        .first()
        is not None
    )
    if is_member or user_has_project_group_access(session, project, actor_groups):
        return project
    raise ForbiddenError("Not authorized")


def _open_handover_review_issue(session, project_id: int) -> Optional[Issue]:
    return (
        session.query(Issue)
        .filter(
            Issue.type == IssueType.HANDOVER_REVIEW,
            Issue.project_id == project_id,
            Issue.status == IssueStatus.OPEN,
        )
        .first()
    )


def submit_version(
    *,
    project_id: int,
    actor_user_id: int,
    actor_username: str,
    actor_role: str,
    actor_groups: Optional[list[str]] = None,
    change_summary: Optional[str] = None,
    requested_version_number: Optional[str] = None,
) -> ProjectVersion:
    """Submit the project's current editable draft for handover review."""
    from core.services.project_version_service import (
        get_current_draft_version,
        submit_project_version,
    )
    db = _get_db()
    session = db.get_session()
    try:
        project = _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            require_owner=True,
            actor_groups=actor_groups,
        )
        if project.is_system == 1:
            raise SystemLockedError("System-managed project is read-only")

        if _open_handover_review_issue(session, project_id) is not None:
            raise ConflictError("A handover review is already open for this project")

        draft_before = get_current_draft_version(session, project)
        old_val = {
            "status": project.status,
            "project_version_id": draft_before.id if draft_before else None,
            "project_version_number": draft_before.version_number if draft_before else None,
            "project_version_status": draft_before.version_status if draft_before else None,
        }
        try:
            version = submit_project_version(
                session,
                project,
                submitted_by=actor_user_id,
                change_summary=change_summary,
                requested_version_number=requested_version_number,
            )
        except ValueError as exc:
            msg = str(exc)
            if msg == "incomplete_handover":
                raise ValidationError(getattr(exc, "completeness", None) or "incomplete_handover")
            if msg == "draft_under_review":
                raise ConflictError("Current draft is already under review")
            if msg == "invalid_version_number":
                raise ValidationError("Version number must use x, x.y, or x.y.z format")
            if msg == "version_number_too_low":
                raise ValidationError("Requested version number cannot be lower than the current draft version")
            if msg == "version_number_duplicate":
                raise ConflictError("Requested version number already exists for this project")
            raise

        assignee_id = _find_platform_admin_assignee(session)
        issue = Issue(
            type=IssueType.HANDOVER_REVIEW,
            status=IssueStatus.OPEN,
            title=f"Handover Review: {project.name} v{version.version_number}",
            description=(
                f"Business Owner '{actor_username}' has submitted project "
                f"'{project.name}' version v{version.version_number} for handover review."
            ),
            project_id=project.id,
            project_version_id=version.id,
            created_by=actor_user_id,
            assignee_id=assignee_id,
        )
        session.add(issue)
        session.flush()
        if assignee_id:
            session.add(Notification(
                user_id=assignee_id,
                title=f"Handover Review Requested: {project.name} v{version.version_number}",
                message=(
                    f"{actor_username} has submitted \"{project.name}\" version "
                    f"v{version.version_number} for handover review."
                ),
                type="issue_assigned",
                related_entity_type="issue",
                related_entity_id=issue.id,
            ))
        session.commit()
        session.refresh(version)
        session.refresh(issue)

        log_audit(
            user_id=actor_user_id,
            action="submit",
            entity_type="project_version",
            entity_id=version.id,
            old_value=old_val,
            new_value=serialize_project_version(version),
        )
        log_audit(
            user_id=actor_user_id,
            action="create",
            entity_type="issue",
            entity_id=issue.id,
            new_value=serialize_issue(issue),
        )
        return version
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Alias retained for the legacy "/handover" route — same project-level submit.
def initiate_handover(
    *,
    project_id: int,
    actor_user_id: int,
    actor_username: str,
    actor_role: str,
    actor_groups: Optional[list[str]] = None,
) -> Issue:
    submit_version(
        project_id=project_id,
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        actor_role=actor_role,
        actor_groups=actor_groups,
    )
    db = _get_db()
    session = db.get_session()
    try:
        issue = _open_handover_review_issue(session, project_id)
        if issue is None:
            raise NotFoundError("Handover review issue not found")
        session.refresh(issue)
        return issue
    finally:
        session.close()


def approve_version(
    *,
    version_id: int,
    actor_user_id: int,
    actor_username: str,
) -> ProjectVersion:
    from core.services.project_version_service import approve_project_version
    db = _get_db()
    session = db.get_session()
    try:
        version = session.query(ProjectVersion).filter(ProjectVersion.id == version_id).first()
        if version is None:
            raise NotFoundError("Project version not found")
        project = session.query(Project).filter(Project.id == version.project_id).first()
        if project is None:
            raise NotFoundError("Project version not found")

        old_val = serialize_project_version(version)
        try:
            approve_project_version(session, project, version, reviewed_by=actor_user_id)
        except ValueError as exc:
            msg = str(exc)
            if msg == "invalid_version_status":
                raise ValidationError("Only pending review versions can be approved")
            raise ValidationError(msg)

        review_issue = (
            session.query(Issue)
            .filter(
                Issue.type == IssueType.HANDOVER_REVIEW,
                Issue.project_version_id == version.id,
                Issue.status == IssueStatus.OPEN,
            )
            .first()
        )
        if review_issue:
            review_issue.status = IssueStatus.CLOSED
            review_issue.resolved_at = datetime.utcnow()
            review_issue.resolution_description = f"Approved by {actor_username}"
            review_issue.updated_at = datetime.utcnow()

        session.add(Notification(
            user_id=project.owner_id,
            title=f"Handover Approved: {project.name} v{version.version_number}",
            message=(
                f"Your project \"{project.name}\" version v{version.version_number} "
                f"has been approved by {actor_username}."
            ),
            type="handover_approved",
            related_entity_type="project",
            related_entity_id=project.id,
        ))
        session.commit()
        session.refresh(version)

        log_audit(
            user_id=actor_user_id,
            action="approve",
            entity_type="project_version",
            entity_id=version.id,
            old_value=old_val,
            new_value=serialize_project_version(version),
        )
        return version
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reject_version(
    *,
    version_id: int,
    rejection_reason: str,
    actor_user_id: int,
    actor_username: str,
) -> ProjectVersion:
    from core.services.project_version_service import reject_project_version
    db = _get_db()
    session = db.get_session()
    try:
        version = session.query(ProjectVersion).filter(ProjectVersion.id == version_id).first()
        if version is None:
            raise NotFoundError("Project version not found")
        project = session.query(Project).filter(Project.id == version.project_id).first()
        if project is None:
            raise NotFoundError("Project version not found")

        old_val = serialize_project_version(version)
        try:
            new_draft = reject_project_version(
                session,
                project,
                version,
                reviewed_by=actor_user_id,
                rejection_reason=rejection_reason,
            )
        except ValueError as exc:
            msg = str(exc)
            if msg == "invalid_version_status":
                raise ValidationError("Only pending review versions can be rejected")
            raise ValidationError(msg)

        review_issue = (
            session.query(Issue)
            .filter(
                Issue.type == IssueType.HANDOVER_REVIEW,
                Issue.project_version_id == version.id,
                Issue.status == IssueStatus.OPEN,
            )
            .first()
        )
        if review_issue:
            review_issue.status = IssueStatus.CLOSED
            review_issue.resolved_at = datetime.utcnow()
            review_issue.rejection_reason = rejection_reason
            review_issue.resolution_description = f"Rejected by {actor_username}: {rejection_reason}"
            review_issue.updated_at = datetime.utcnow()

        session.add(Notification(
            user_id=project.owner_id,
            title=f"Handover Rejected: {project.name} v{version.version_number}",
            message=(
                f"Your project \"{project.name}\" version v{version.version_number} "
                f"was rejected by {actor_username}.\n\nReason: {rejection_reason}"
            ),
            type="handover_rejected",
            related_entity_type="project",
            related_entity_id=project.id,
        ))
        session.commit()
        session.refresh(version)

        log_audit(
            user_id=actor_user_id,
            action="reject",
            entity_type="project_version",
            entity_id=version.id,
            old_value=old_val,
            new_value={
                "rejection_reason": rejection_reason,
                "next_draft_version_id": new_draft.id,
            },
        )
        return version
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def rollback_version(
    *,
    version_id: int,
    actor_user_id: int,
    actor_username: str,
    actor_role: str,
    actor_groups: Optional[list[str]] = None,
) -> ProjectVersion:
    from core.services.project_version_service import (
        can_mutate_project_scope,
        rollback_project_to_version,
    )
    db = _get_db()
    session = db.get_session()
    try:
        source_version = session.query(ProjectVersion).filter(ProjectVersion.id == version_id).first()
        if source_version is None:
            raise NotFoundError("Project version not found")
        project = session.query(Project).filter(Project.id == source_version.project_id).first()
        if project is None:
            raise NotFoundError("Project version not found")
        if project.is_system == 1:
            raise SystemLockedError("System-managed project is read-only")
        _require_project_access(
            session,
            project_id=project.id,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            require_owner=True,
            actor_groups=actor_groups,
        )

        is_mutable, lock_err = can_mutate_project_scope(session, project)
        if not is_mutable:
            if lock_err == "draft_under_review":
                raise ConflictError("Current draft is already under review")
            raise ValidationError(lock_err)

        old_snapshot = serialize_project(project)
        try:
            draft = rollback_project_to_version(session, project, source_version)
        except ValueError as exc:
            msg = str(exc)
            if msg == "snapshot_unavailable":
                raise ValidationError("This version cannot be rolled back because no snapshot is available")
            if msg == "draft_not_editable":
                raise ValidationError("Current draft is not editable")
            raise
        session.commit()
        session.refresh(draft)

        log_audit(
            user_id=actor_user_id,
            action="rollback",
            entity_type="project_version",
            entity_id=draft.id,
            old_value={
                "source_version_id": source_version.id,
                "source_version_number": source_version.version_number,
                "project_before": old_snapshot,
            },
            new_value=serialize_project_version(draft),
        )
        return draft
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
