"""Helpers for project-version bootstrap, snapshot capture, submission, and approval.

Version control lives at the project level (a project usually maps to one
repo). A ProjectVersion freezes the *entire* project scope — every product
under the project plus their jobs, applications, and scenarios — so the whole
handover can be reviewed, approved, and rolled back as a unit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from core.services.audit_service import (
    serialize_app,
    serialize_application_recovery_scenario,
    serialize_job,
    serialize_job_failure_scenario,
    serialize_product,
)
from core.models.entities import (
    Application,
    ApplicationRecoveryScenario,
    Job,
    JobFailureScenario,
    Product,
    ProductStatus,
    Project,
    ProjectLifecycleStatus,
    ProjectStatus,
    ProjectVersion,
    ProjectVersionStatus,
)
from core.services.handover_service import evaluate_project_handover_completeness
from core.services.version_number_utils import (
    compare_version_numbers,
    max_version_number,
    next_version_number,
    normalize_version_number,
    version_key,
)


def _version_context(version: Optional[ProjectVersion]) -> Dict[str, Any]:
    """Return a compact version reference for embedding in audit payloads."""
    if version is None:
        return {
            "project_version_id": None,
            "project_version_number": None,
            "project_version_status": None,
        }
    return {
        "project_version_id": version.id,
        "project_version_number": version.version_number,
        "project_version_status": version.version_status,
    }


def build_project_snapshot(
    session,
    project_id: int,
    *,
    completeness: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Freeze the current project scope (all products + assets) into a stable payload."""
    project = session.query(Project).filter(Project.id == project_id).first()
    if project is None:
        raise ValueError(f"Project {project_id} not found")

    if completeness is None:
        completeness = evaluate_project_handover_completeness(session, project_id)

    products = (
        session.query(Product)
        .filter(Product.project_id == project_id)
        .order_by(Product.id.asc())
        .all()
    )

    product_entries: List[Dict[str, Any]] = []
    for product in products:
        jobs = (
            session.query(Job)
            .filter(Job.product_id == product.id)
            .order_by(Job.id.asc())
            .all()
        )
        applications = (
            session.query(Application)
            .filter(Application.product_id == product.id)
            .order_by(Application.id.asc())
            .all()
        )

        job_entries = []
        for job in jobs:
            scenarios = (
                session.query(JobFailureScenario)
                .filter(JobFailureScenario.job_id == job.id)
                .order_by(JobFailureScenario.created_at.asc(), JobFailureScenario.id.asc())
                .all()
            )
            job_entries.append({
                **serialize_job(job),
                "failure_scenarios": [serialize_job_failure_scenario(scenario) for scenario in scenarios],
            })

        application_entries = []
        for app in applications:
            scenarios = (
                session.query(ApplicationRecoveryScenario)
                .filter(ApplicationRecoveryScenario.application_id == app.id)
                .order_by(ApplicationRecoveryScenario.created_at.asc(), ApplicationRecoveryScenario.id.asc())
                .all()
            )
            application_entries.append({
                **serialize_app(app),
                "recovery_scenarios": [serialize_application_recovery_scenario(scenario) for scenario in scenarios],
            })

        product_entries.append({
            **serialize_product(product),
            "jobs": job_entries,
            "applications": application_entries,
        })

    return {
        "captured_at": datetime.utcnow().isoformat(),
        "project": {
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "prod_stat_url": project.prod_stat_url,
            "cml_project_name": project.cml_project_name,
        },
        "products": product_entries,
        "completeness": completeness,
    }


def refresh_project_version_artifacts(session, version: ProjectVersion) -> ProjectVersion:
    """Refresh snapshot/completeness for mutable or newly-captured versions."""
    completeness = evaluate_project_handover_completeness(session, version.project_id)
    version.snapshot_json = build_project_snapshot(
        session,
        version.project_id,
        completeness=completeness,
    )
    version.completeness_summary_json = completeness
    version.updated_at = datetime.utcnow()
    return version


def get_current_draft_version(session, project: Project) -> Optional[ProjectVersion]:
    """Load the project's current draft version, if any."""
    if not project.current_draft_version_id:
        return None
    return (
        session.query(ProjectVersion)
        .filter(ProjectVersion.id == project.current_draft_version_id)
        .first()
    )


def get_current_approved_version(session, project: Project) -> Optional[ProjectVersion]:
    """Load the project's current approved version, if any."""
    if not project.current_approved_version_id:
        return None
    return (
        session.query(ProjectVersion)
        .filter(ProjectVersion.id == project.current_approved_version_id)
        .first()
    )


def _sorted_project_versions(session, project_id: int) -> List[ProjectVersion]:
    """Return project versions sorted by semantic version number descending."""
    versions = (
        session.query(ProjectVersion)
        .filter(ProjectVersion.project_id == project_id)
        .all()
    )
    return sorted(
        versions,
        key=lambda version: (version_key(version.version_number), version.created_at or datetime.min),
        reverse=True,
    )


def _next_version_number(session, project: Project) -> str:
    versions = _sorted_project_versions(session, project.id)
    highest_existing = versions[0].version_number if versions else None
    current_latest = normalize_version_number(project.latest_version_number)
    highest_known = max_version_number([current_latest, highest_existing])
    return next_version_number(highest_known)


def create_draft_version(
    session,
    project: Project,
    *,
    derived_from: Optional[ProjectVersion] = None,
    change_summary: str = "",
) -> ProjectVersion:
    """Create a new editable draft version for the current mutable workspace."""
    version = ProjectVersion(
        project_id=project.id,
        version_number=_next_version_number(session, project),
        version_status=ProjectVersionStatus.DRAFT,
        derived_from_version_id=derived_from.id if derived_from else None,
        change_summary=change_summary or "",
    )
    session.add(version)
    session.flush()
    project.current_draft_version_id = version.id
    project.latest_version_number = version.version_number
    project.updated_at = datetime.utcnow()
    if project.current_approved_version_id:
        project.lifecycle_status = ProjectLifecycleStatus.ACTIVE
        project.status = ProjectStatus.ACTIVE
    else:
        project.lifecycle_status = ProjectLifecycleStatus.DRAFTING
        project.status = ProjectStatus.DRAFT
    refresh_project_version_artifacts(session, version)
    return version


def ensure_project_baseline_version(session, project: Project) -> ProjectVersion:
    """Ensure a project has at least one version record; bootstrap a draft v1 lazily."""
    if project.current_draft_version_id:
        existing_draft = (
            session.query(ProjectVersion)
            .filter(ProjectVersion.id == project.current_draft_version_id)
            .first()
        )
        if existing_draft:
            return existing_draft

    if project.current_approved_version_id:
        existing_approved = (
            session.query(ProjectVersion)
            .filter(ProjectVersion.id == project.current_approved_version_id)
            .first()
        )
        if existing_approved:
            return existing_approved

    version_number = normalize_version_number(project.latest_version_number) or "1"
    if project.status == ProjectStatus.PENDING_REVIEW:
        version_status = ProjectVersionStatus.PENDING_REVIEW
    elif project.status in (ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED):
        version_status = ProjectVersionStatus.APPROVED
    else:
        version_status = ProjectVersionStatus.DRAFT
    version = ProjectVersion(
        project_id=project.id,
        version_number=version_number,
        version_status=version_status,
    )
    session.add(version)
    session.flush()

    project.latest_version_number = max_version_number([project.latest_version_number, version_number])
    if version_status == ProjectVersionStatus.APPROVED:
        project.current_approved_version_id = version.id
        project.lifecycle_status = (
            ProjectLifecycleStatus.ARCHIVED
            if project.status == ProjectStatus.ARCHIVED
            else ProjectLifecycleStatus.ACTIVE
        )
    else:
        project.current_draft_version_id = version.id
        if project.lifecycle_status not in (ProjectLifecycleStatus.ACTIVE, ProjectLifecycleStatus.ARCHIVED):
            project.lifecycle_status = ProjectLifecycleStatus.DRAFTING
    project.updated_at = datetime.utcnow()
    refresh_project_version_artifacts(session, version)
    return version


def ensure_project_editable_draft(session, project: Project) -> ProjectVersion:
    """Return the editable draft for a project, creating one from the approved/rejected version if needed."""
    baseline = ensure_project_baseline_version(session, project)
    draft = get_current_draft_version(session, project)
    if draft:
        if draft.version_status == ProjectVersionStatus.DRAFT:
            return draft
        if draft.version_status == ProjectVersionStatus.PENDING_REVIEW:
            raise ValueError("draft_under_review")
        if draft.version_status in (
            ProjectVersionStatus.REJECTED,
            ProjectVersionStatus.SUPERSEDED,
            ProjectVersionStatus.APPROVED,
        ):
            return create_draft_version(session, project, derived_from=draft)

    approved = get_current_approved_version(session, project)
    if approved:
        return create_draft_version(session, project, derived_from=approved)

    if baseline.version_status == ProjectVersionStatus.DRAFT:
        project.current_draft_version_id = baseline.id
        project.status = ProjectStatus.DRAFT
        project.lifecycle_status = ProjectLifecycleStatus.DRAFTING
        return baseline

    return create_draft_version(session, project, derived_from=baseline)


def can_mutate_project_scope(session, project: Project) -> tuple[bool, Optional[str]]:
    """Tell routers whether the mutable workspace can be edited right now."""
    if project.is_system == 1:
        return False, "system_locked"
    draft = get_current_draft_version(session, project)
    if draft and draft.version_status == ProjectVersionStatus.PENDING_REVIEW:
        return False, "draft_under_review"
    return True, None


def assign_draft_version_number(
    session,
    project: Project,
    draft: ProjectVersion,
    *,
    requested_version_number: Optional[str],
) -> ProjectVersion:
    """Optionally rename the editable draft's version number before submission."""
    if requested_version_number is None:
        return draft
    requested_version_number = normalize_version_number(requested_version_number)
    if compare_version_numbers(requested_version_number, draft.version_number) < 0:
        raise ValueError("version_number_too_low")

    duplicate = (
        session.query(ProjectVersion)
        .filter(
            ProjectVersion.project_id == project.id,
            ProjectVersion.version_number == requested_version_number,
            ProjectVersion.id != draft.id,
        )
        .first()
    )
    if duplicate is not None:
        raise ValueError("version_number_duplicate")

    draft.version_number = requested_version_number
    project.latest_version_number = max_version_number([project.latest_version_number, requested_version_number])
    draft.updated_at = datetime.utcnow()
    project.updated_at = datetime.utcnow()
    return draft


def _restore_project_scope_from_snapshot(
    session,
    project: Project,
    snapshot: Dict[str, Any],
) -> None:
    """Replace the mutable workspace tables (products + assets) with a prior snapshot."""
    products_payload = list(snapshot.get("products") or [])

    products = session.query(Product).filter(Product.project_id == project.id).all()
    product_ids = [p.id for p in products]

    if product_ids:
        job_ids_subq = session.query(Job.id).filter(Job.product_id.in_(product_ids))
        app_ids_subq = session.query(Application.id).filter(Application.product_id.in_(product_ids))
        (
            session.query(JobFailureScenario)
            .filter(JobFailureScenario.job_id.in_(job_ids_subq))
            .delete(synchronize_session=False)
        )
        (
            session.query(ApplicationRecoveryScenario)
            .filter(ApplicationRecoveryScenario.application_id.in_(app_ids_subq))
            .delete(synchronize_session=False)
        )
        session.query(Job).filter(Job.product_id.in_(product_ids)).delete(synchronize_session=False)
        session.query(Application).filter(Application.product_id.in_(product_ids)).delete(synchronize_session=False)
        session.query(Product).filter(Product.project_id == project.id).delete(synchronize_session=False)
    session.flush()

    for product_payload in products_payload:
        product = Product(
            project_id=project.id,
            name=product_payload.get("name") or "Recovered Product",
            status=product_payload.get("status") or ProductStatus.DRAFT,
            is_system=0,
        )
        session.add(product)
        session.flush()

        for job_payload in product_payload.get("jobs") or []:
            job = Job(
                product_id=product.id,
                mmp_project_id=job_payload.get("mmp_project_id") or "",
                mmp_model_id=job_payload.get("mmp_model_id") or "",
                control_m_job_name=job_payload.get("control_m_job_name") or "",
                cml_project_name=job_payload.get("cml_project_name") or "",
                cml_job_name=job_payload.get("cml_job_name") or "",
                cml_project_id=job_payload.get("cml_project_id"),
                cml_job_id=job_payload.get("cml_job_id"),
                schedule_cron=job_payload.get("schedule_cron") or "",
                description=job_payload.get("description") or "",
                dependencies=job_payload.get("dependencies"),
                failure_strategy_summary=job_payload.get("failure_strategy_summary") or "",
                dependency_notes=job_payload.get("dependency_notes") or "",
                owner_contact=job_payload.get("owner_contact") or "",
                support_group_id=job_payload.get("support_group_id"),
                support_group_name_snapshot=job_payload.get("support_group_name_snapshot") or "",
                support_group=job_payload.get("support_group") or "",
                runbook_required=bool(job_payload.get("runbook_required", True)),
                has_mmp_dependency=job_payload.get("has_mmp_dependency"),
                is_system=0,
            )
            session.add(job)
            session.flush()
            for scenario_payload in job_payload.get("failure_scenarios") or []:
                session.add(JobFailureScenario(
                    job_id=job.id,
                    scenario_type=scenario_payload.get("scenario_type") or "other",
                    scenario_name=scenario_payload.get("scenario_name") or "Recovered Scenario",
                    condition_description=scenario_payload.get("condition_description") or "",
                    detection_source=scenario_payload.get("detection_source") or "",
                    diagnostic_steps=scenario_payload.get("diagnostic_steps"),
                    action_steps=scenario_payload.get("action_steps"),
                    verification_steps=scenario_payload.get("verification_steps"),
                    escalation_target=scenario_payload.get("escalation_target") or "",
                    fallback_owner_type=scenario_payload.get("fallback_owner_type"),
                    is_active=bool(scenario_payload.get("is_active", True)),
                ))

        for app_payload in product_payload.get("applications") or []:
            app = Application(
                product_id=product.id,
                application_url=app_payload.get("application_url") or "",
                health_check_url=app_payload.get("health_check_url") or "",
                cml_project_name=app_payload.get("cml_project_name") or "",
                cml_application_name=app_payload.get("cml_application_name") or "",
                cml_subdomain=app_payload.get("cml_subdomain") or "",
                cml_app_type=app_payload.get("cml_app_type") or "generic",
                cml_project_id=app_payload.get("cml_project_id"),
                cml_application_id=app_payload.get("cml_application_id"),
                cml_serving_url=app_payload.get("cml_serving_url") or "",
                description=app_payload.get("description") or "",
                restart_supported=bool(app_payload.get("restart_supported", False)),
                restart_summary=app_payload.get("restart_summary") or "",
                owner_contact=app_payload.get("owner_contact") or "",
                support_group_id=app_payload.get("support_group_id"),
                support_group_name_snapshot=app_payload.get("support_group_name_snapshot") or "",
                support_group=app_payload.get("support_group") or "",
                is_system=0,
            )
            session.add(app)
            session.flush()
            for scenario_payload in app_payload.get("recovery_scenarios") or []:
                session.add(ApplicationRecoveryScenario(
                    application_id=app.id,
                    scenario_type=scenario_payload.get("scenario_type") or "other",
                    scenario_name=scenario_payload.get("scenario_name") or "Recovered Scenario",
                    condition_description=scenario_payload.get("condition_description") or "",
                    action_steps=scenario_payload.get("action_steps"),
                    verification_steps=scenario_payload.get("verification_steps"),
                    escalation_target=scenario_payload.get("escalation_target") or "",
                    fallback_owner_type=scenario_payload.get("fallback_owner_type"),
                    email_template=scenario_payload.get("email_template"),
                    is_active=bool(scenario_payload.get("is_active", True)),
                ))
    project.updated_at = datetime.utcnow()


def rollback_project_to_version(
    session,
    project: Project,
    source_version: ProjectVersion,
) -> ProjectVersion:
    """Restore a historical version snapshot into the current editable draft workspace."""
    snapshot = source_version.snapshot_json or {}
    if not snapshot:
        raise ValueError("snapshot_unavailable")

    draft = ensure_project_editable_draft(session, project)
    if draft.version_status != ProjectVersionStatus.DRAFT:
        raise ValueError("draft_not_editable")

    _restore_project_scope_from_snapshot(session, project, snapshot)
    draft.derived_from_version_id = source_version.id
    draft.change_summary = f"Rollback prepared from v{source_version.version_number}"
    refresh_project_version_artifacts(session, draft)
    project.status = ProjectStatus.ACTIVE if project.current_approved_version_id else ProjectStatus.DRAFT
    project.lifecycle_status = (
        ProjectLifecycleStatus.ACTIVE if project.current_approved_version_id else ProjectLifecycleStatus.DRAFTING
    )
    project.updated_at = datetime.utcnow()
    return draft


def submit_project_version(
    session,
    project: Project,
    *,
    submitted_by: Optional[int],
    change_summary: Optional[str] = None,
    requested_version_number: Optional[str] = None,
) -> ProjectVersion:
    """Submit the current editable draft for review."""
    draft = ensure_project_editable_draft(session, project)
    if draft.version_status != ProjectVersionStatus.DRAFT:
        raise ValueError("draft_not_editable")
    assign_draft_version_number(
        session,
        project,
        draft,
        requested_version_number=requested_version_number,
    )

    completeness = evaluate_project_handover_completeness(session, project.id)
    if not completeness["is_complete"]:
        exc = ValueError("incomplete_handover")
        setattr(exc, "completeness", completeness)
        raise exc

    refresh_project_version_artifacts(session, draft)
    draft.version_status = ProjectVersionStatus.PENDING_REVIEW
    draft.submitted_by = submitted_by
    draft.submitted_at = datetime.utcnow()
    draft.reviewed_by = None
    draft.reviewed_at = None
    draft.rejection_reason = None
    if change_summary is not None:
        draft.change_summary = change_summary
    draft.completeness_summary_json = completeness
    draft.updated_at = datetime.utcnow()
    project.updated_at = datetime.utcnow()
    if project.current_approved_version_id:
        project.status = ProjectStatus.ACTIVE
        project.lifecycle_status = ProjectLifecycleStatus.ACTIVE
    else:
        project.status = ProjectStatus.PENDING_REVIEW
        project.lifecycle_status = ProjectLifecycleStatus.DRAFTING
    return draft


def approve_project_version(
    session,
    project: Project,
    version: ProjectVersion,
    *,
    reviewed_by: Optional[int],
) -> ProjectVersion:
    """Approve a pending review version and promote it to the current active version."""
    if version.version_status != ProjectVersionStatus.PENDING_REVIEW:
        raise ValueError("invalid_version_status")

    previous_approved = get_current_approved_version(session, project)
    if previous_approved and previous_approved.id != version.id:
        previous_approved.version_status = ProjectVersionStatus.SUPERSEDED
        previous_approved.updated_at = datetime.utcnow()

    version.version_status = ProjectVersionStatus.APPROVED
    version.reviewed_by = reviewed_by
    version.reviewed_at = datetime.utcnow()
    version.updated_at = datetime.utcnow()
    project.current_approved_version_id = version.id
    if project.current_draft_version_id == version.id:
        project.current_draft_version_id = None
    project.status = ProjectStatus.ACTIVE
    project.lifecycle_status = ProjectLifecycleStatus.ACTIVE
    project.updated_at = datetime.utcnow()
    return version


def reject_project_version(
    session,
    project: Project,
    version: ProjectVersion,
    *,
    reviewed_by: Optional[int],
    rejection_reason: str,
) -> ProjectVersion:
    """Reject a pending version and immediately open a fresh editable draft."""
    if version.version_status != ProjectVersionStatus.PENDING_REVIEW:
        raise ValueError("invalid_version_status")

    version.version_status = ProjectVersionStatus.REJECTED
    version.reviewed_by = reviewed_by
    version.reviewed_at = datetime.utcnow()
    version.rejection_reason = rejection_reason
    version.updated_at = datetime.utcnow()

    new_draft = create_draft_version(session, project, derived_from=version)
    project.current_draft_version_id = new_draft.id
    project.updated_at = datetime.utcnow()
    if project.current_approved_version_id:
        project.status = ProjectStatus.ACTIVE
        project.lifecycle_status = ProjectLifecycleStatus.ACTIVE
    else:
        project.status = ProjectStatus.DRAFT
        project.lifecycle_status = ProjectLifecycleStatus.DRAFTING
    return new_draft


def get_version_audit_context(version: Optional[ProjectVersion]) -> Dict[str, Any]:
    """Public wrapper for version context embedding."""
    return _version_context(version)
