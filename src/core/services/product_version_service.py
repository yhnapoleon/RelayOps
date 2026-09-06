"""Helpers for product-version bootstrap, snapshot capture, submission, and approval."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

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
    IssueType,
    Job,
    JobFailureScenario,
    Product,
    ProductLifecycleStatus,
    ProductStatus,
    ProductVersion,
    ProductVersionStatus,
)
from core.services.handover_service import evaluate_product_handover_completeness
from core.services.version_number_utils import (
    compare_version_numbers,
    max_version_number,
    next_version_number,
    normalize_version_number,
    version_key,
)


def _version_context(version: Optional[ProductVersion]) -> Dict[str, Any]:
    """Return a compact version reference for embedding in audit payloads."""
    if version is None:
        return {
            "product_version_id": None,
            "product_version_number": None,
            "product_version_status": None,
        }
    return {
        "product_version_id": version.id,
        "product_version_number": version.version_number,
        "product_version_status": version.version_status,
    }


def build_product_snapshot(
    session,
    product_id: int,
    *,
    completeness: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Freeze the current product scope into a stable JSON-friendly payload."""
    product = session.query(Product).filter(Product.id == product_id).first()
    if product is None:
        raise ValueError(f"Product {product_id} not found")

    if completeness is None:
        completeness = evaluate_product_handover_completeness(session, product_id)

    jobs = (
        session.query(Job)
        .filter(Job.product_id == product_id)
        .order_by(Job.id.asc())
        .all()
    )
    applications = (
        session.query(Application)
        .filter(Application.product_id == product_id)
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

    return {
        "captured_at": datetime.utcnow().isoformat(),
        "product": serialize_product(product),
        "jobs": job_entries,
        "applications": application_entries,
        "completeness": completeness,
    }


def refresh_product_version_artifacts(session, version: ProductVersion) -> ProductVersion:
    """Refresh snapshot/completeness for mutable or newly-captured versions."""
    completeness = evaluate_product_handover_completeness(session, version.product_id)
    version.snapshot_json = build_product_snapshot(
        session,
        version.product_id,
        completeness=completeness,
    )
    version.completeness_summary_json = completeness
    version.updated_at = datetime.utcnow()
    return version


def get_current_draft_version(session, product: Product) -> Optional[ProductVersion]:
    """Load the product's current draft version, if any."""
    if not product.current_draft_version_id:
        return None
    return (
        session.query(ProductVersion)
        .filter(ProductVersion.id == product.current_draft_version_id)
        .first()
    )


def get_current_approved_version(session, product: Product) -> Optional[ProductVersion]:
    """Load the product's current approved version, if any."""
    if not product.current_approved_version_id:
        return None
    return (
        session.query(ProductVersion)
        .filter(ProductVersion.id == product.current_approved_version_id)
        .first()
    )


def _sorted_product_versions(session, product_id: int) -> list[ProductVersion]:
    """Return product versions sorted by semantic version number descending."""
    versions = (
        session.query(ProductVersion)
        .filter(ProductVersion.product_id == product_id)
        .all()
    )
    return sorted(versions, key=lambda version: (version_key(version.version_number), version.created_at or datetime.min), reverse=True)


def _next_version_number(session, product: Product) -> str:
    versions = _sorted_product_versions(session, product.id)
    highest_existing = versions[0].version_number if versions else None
    current_latest = normalize_version_number(product.latest_version_number)
    highest_known = max_version_number([current_latest, highest_existing])
    return next_version_number(highest_known)


def create_draft_version(
    session,
    product: Product,
    *,
    derived_from: Optional[ProductVersion] = None,
    change_summary: str = "",
) -> ProductVersion:
    """Create a new editable draft version for the current mutable workspace."""
    version = ProductVersion(
        product_id=product.id,
        version_number=_next_version_number(session, product),
        version_status=ProductVersionStatus.DRAFT,
        derived_from_version_id=derived_from.id if derived_from else None,
        change_summary=change_summary or "",
    )
    session.add(version)
    session.flush()
    product.current_draft_version_id = version.id
    product.latest_version_number = version.version_number
    product.updated_at = datetime.utcnow()
    if product.current_approved_version_id:
        product.lifecycle_status = ProductLifecycleStatus.ACTIVE
        product.status = ProductStatus.ACTIVE
    else:
        product.lifecycle_status = ProductLifecycleStatus.DRAFTING
        product.status = ProductStatus.DRAFT
    refresh_product_version_artifacts(session, version)
    return version


def ensure_product_baseline_version(session, product: Product) -> ProductVersion:
    """
    Ensure a product has at least one version record.

    Existing active products lazily bootstrap to an approved v1.
    Everything else lazily bootstraps to a draft v1.
    """
    if product.current_draft_version_id:
        existing_draft = (
            session.query(ProductVersion)
            .filter(ProductVersion.id == product.current_draft_version_id)
            .first()
        )
        if existing_draft:
            return existing_draft

    if product.current_approved_version_id:
        existing_approved = (
            session.query(ProductVersion)
            .filter(ProductVersion.id == product.current_approved_version_id)
            .first()
        )
        if existing_approved:
            return existing_approved

    version_number = normalize_version_number(product.latest_version_number) or "1"
    if product.status == ProductStatus.PENDING_REVIEW:
        version_status = ProductVersionStatus.PENDING_REVIEW
    elif product.status in (ProductStatus.ACTIVE, ProductStatus.ARCHIVED):
        version_status = ProductVersionStatus.APPROVED
    else:
        version_status = ProductVersionStatus.DRAFT
    version = ProductVersion(
        product_id=product.id,
        version_number=version_number,
        version_status=version_status,
    )
    session.add(version)
    session.flush()

    product.latest_version_number = max_version_number([product.latest_version_number, version_number])
    if version_status == ProductVersionStatus.APPROVED:
        product.current_approved_version_id = version.id
        product.lifecycle_status = (
            ProductLifecycleStatus.ARCHIVED
            if product.status == ProductStatus.ARCHIVED
            else ProductLifecycleStatus.ACTIVE
        )
    else:
        product.current_draft_version_id = version.id
        if product.lifecycle_status not in (ProductLifecycleStatus.ACTIVE, ProductLifecycleStatus.ARCHIVED):
            product.lifecycle_status = ProductLifecycleStatus.DRAFTING
    product.updated_at = datetime.utcnow()
    refresh_product_version_artifacts(session, version)
    return version


def ensure_product_editable_draft(session, product: Product) -> ProductVersion:
    """
    Return the editable draft for a product, creating one from the approved/rejected
    version when necessary.
    """
    baseline = ensure_product_baseline_version(session, product)
    draft = get_current_draft_version(session, product)
    if draft:
        if draft.version_status == ProductVersionStatus.DRAFT:
            return draft
        if draft.version_status == ProductVersionStatus.PENDING_REVIEW:
            raise ValueError("draft_under_review")
        if draft.version_status in (
            ProductVersionStatus.REJECTED,
            ProductVersionStatus.SUPERSEDED,
            ProductVersionStatus.APPROVED,
        ):
            return create_draft_version(session, product, derived_from=draft)

    approved = get_current_approved_version(session, product)
    if approved:
        return create_draft_version(session, product, derived_from=approved)

    if baseline.version_status == ProductVersionStatus.DRAFT:
        product.current_draft_version_id = baseline.id
        product.status = ProductStatus.DRAFT
        product.lifecycle_status = ProductLifecycleStatus.DRAFTING
        return baseline

    return create_draft_version(session, product, derived_from=baseline)


def can_mutate_product_scope(session, product: Product) -> tuple[bool, Optional[str]]:
    """Tell routers whether the mutable workspace can be edited right now."""
    if product.is_system == 1:
        return False, "system_locked"
    draft = get_current_draft_version(session, product)
    if draft and draft.version_status == ProductVersionStatus.PENDING_REVIEW:
        return False, "draft_under_review"
    return True, None


def assign_draft_version_number(
    session,
    product: Product,
    draft: ProductVersion,
    *,
    requested_version_number: Optional[str],
) -> ProductVersion:
    """Optionally rename the editable draft's version number before submission."""
    if requested_version_number is None:
        return draft
    requested_version_number = normalize_version_number(requested_version_number)
    if compare_version_numbers(requested_version_number, draft.version_number) < 0:
        raise ValueError("version_number_too_low")

    duplicate = (
        session.query(ProductVersion)
        .filter(
            ProductVersion.product_id == product.id,
            ProductVersion.version_number == requested_version_number,
            ProductVersion.id != draft.id,
        )
        .first()
    )
    if duplicate is not None:
        raise ValueError("version_number_duplicate")

    draft.version_number = requested_version_number
    product.latest_version_number = max_version_number([product.latest_version_number, requested_version_number])
    draft.updated_at = datetime.utcnow()
    product.updated_at = datetime.utcnow()
    return draft


def _restore_product_scope_from_snapshot(
    session,
    product: Product,
    snapshot: Dict[str, Any],
) -> None:
    """Replace the mutable workspace tables with a prior version snapshot."""
    product_payload = dict(snapshot.get("product") or {})
    jobs_payload = list(snapshot.get("jobs") or [])
    applications_payload = list(snapshot.get("applications") or [])

    if product_payload:
        if "name" in product_payload:
            product.name = product_payload.get("name") or product.name

    (
        session.query(JobFailureScenario)
        .filter(JobFailureScenario.job_id.in_(session.query(Job.id).filter(Job.product_id == product.id)))
        .delete(synchronize_session=False)
    )
    (
        session.query(ApplicationRecoveryScenario)
        .filter(
            ApplicationRecoveryScenario.application_id.in_(
                session.query(Application.id).filter(Application.product_id == product.id)
            )
        )
        .delete(synchronize_session=False)
    )
    session.query(Job).filter(Job.product_id == product.id).delete(synchronize_session=False)
    session.query(Application).filter(Application.product_id == product.id).delete(synchronize_session=False)
    session.flush()

    for job_payload in jobs_payload:
        job = Job(
            product_id=product.id,
            mmp_project_id=job_payload.get("mmp_project_id") or "",
            mmp_model_id=job_payload.get("mmp_model_id") or "",
            control_m_job_name=job_payload.get("control_m_job_name") or "",
            control_m_cron=job_payload.get("control_m_cron") or "",
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

    for app_payload in applications_payload:
        app = Application(
            product_id=product.id,
            application_url=app_payload.get("application_url") or "",
            health_check_url=app_payload.get("health_check_url") or "",
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
    product.updated_at = datetime.utcnow()


def rollback_product_to_version(
    session,
    product: Product,
    source_version: ProductVersion,
) -> ProductVersion:
    """Restore a historical version snapshot into the current editable draft workspace."""
    snapshot = source_version.snapshot_json or {}
    if not snapshot:
        raise ValueError("snapshot_unavailable")

    draft = ensure_product_editable_draft(session, product)
    if draft.version_status != ProductVersionStatus.DRAFT:
        raise ValueError("draft_not_editable")

    _restore_product_scope_from_snapshot(session, product, snapshot)
    draft.derived_from_version_id = source_version.id
    draft.change_summary = f"Rollback prepared from v{source_version.version_number}"
    refresh_product_version_artifacts(session, draft)
    product.status = ProductStatus.ACTIVE if product.current_approved_version_id else ProductStatus.DRAFT
    product.lifecycle_status = (
        ProductLifecycleStatus.ACTIVE if product.current_approved_version_id else ProductLifecycleStatus.DRAFTING
    )
    product.updated_at = datetime.utcnow()
    return draft


def submit_product_version(
    session,
    product: Product,
    *,
    submitted_by: Optional[int],
    change_summary: Optional[str] = None,
    requested_version_number: Optional[str] = None,
) -> ProductVersion:
    """Submit the current editable draft for review."""
    draft = ensure_product_editable_draft(session, product)
    if draft.version_status != ProductVersionStatus.DRAFT:
        raise ValueError("draft_not_editable")
    assign_draft_version_number(
        session,
        product,
        draft,
        requested_version_number=requested_version_number,
    )

    completeness = evaluate_product_handover_completeness(session, product.id)
    if not completeness["is_complete"]:
        exc = ValueError("incomplete_handover")
        setattr(exc, "completeness", completeness)
        raise exc

    refresh_product_version_artifacts(session, draft)
    draft.version_status = ProductVersionStatus.PENDING_REVIEW
    draft.submitted_by = submitted_by
    draft.submitted_at = datetime.utcnow()
    draft.reviewed_by = None
    draft.reviewed_at = None
    draft.rejection_reason = None
    if change_summary is not None:
        draft.change_summary = change_summary
    draft.completeness_summary_json = completeness
    draft.updated_at = datetime.utcnow()
    product.updated_at = datetime.utcnow()
    if product.current_approved_version_id:
        product.status = ProductStatus.ACTIVE
        product.lifecycle_status = ProductLifecycleStatus.ACTIVE
    else:
        product.status = ProductStatus.PENDING_REVIEW
        product.lifecycle_status = ProductLifecycleStatus.DRAFTING
    return draft


def approve_product_version(
    session,
    product: Product,
    version: ProductVersion,
    *,
    reviewed_by: Optional[int],
) -> ProductVersion:
    """Approve a pending review version and promote it to the current active version."""
    if version.version_status != ProductVersionStatus.PENDING_REVIEW:
        raise ValueError("invalid_version_status")

    previous_approved = get_current_approved_version(session, product)
    if previous_approved and previous_approved.id != version.id:
        previous_approved.version_status = ProductVersionStatus.SUPERSEDED
        previous_approved.updated_at = datetime.utcnow()

    version.version_status = ProductVersionStatus.APPROVED
    version.reviewed_by = reviewed_by
    version.reviewed_at = datetime.utcnow()
    version.updated_at = datetime.utcnow()
    product.current_approved_version_id = version.id
    if product.current_draft_version_id == version.id:
        product.current_draft_version_id = None
    product.status = ProductStatus.ACTIVE
    product.lifecycle_status = ProductLifecycleStatus.ACTIVE
    product.updated_at = datetime.utcnow()
    return version


def reject_product_version(
    session,
    product: Product,
    version: ProductVersion,
    *,
    reviewed_by: Optional[int],
    rejection_reason: str,
) -> ProductVersion:
    """Reject a pending version and immediately open a fresh editable draft."""
    if version.version_status != ProductVersionStatus.PENDING_REVIEW:
        raise ValueError("invalid_version_status")

    version.version_status = ProductVersionStatus.REJECTED
    version.reviewed_by = reviewed_by
    version.reviewed_at = datetime.utcnow()
    version.rejection_reason = rejection_reason
    version.updated_at = datetime.utcnow()

    new_draft = create_draft_version(session, product, derived_from=version)
    product.current_draft_version_id = new_draft.id
    product.updated_at = datetime.utcnow()
    if product.current_approved_version_id:
        product.status = ProductStatus.ACTIVE
        product.lifecycle_status = ProductLifecycleStatus.ACTIVE
    else:
        product.status = ProductStatus.DRAFT
        product.lifecycle_status = ProductLifecycleStatus.DRAFTING
    return new_draft


def get_version_for_issue(session, *, product_id: Optional[int], issue_type: Optional[str] = None) -> Optional[ProductVersion]:
    """Choose the most relevant version to bind to a newly created issue."""
    if product_id is None:
        return None

    product = session.query(Product).filter(Product.id == product_id).first()
    if product is None:
        return None

    if issue_type == IssueType.HANDOVER_REVIEW:
        try:
            return ensure_product_editable_draft(session, product)
        except ValueError:
            draft = get_current_draft_version(session, product)
            return draft or ensure_product_baseline_version(session, product)

    if product.current_approved_version_id:
        version = (
            session.query(ProductVersion)
            .filter(ProductVersion.id == product.current_approved_version_id)
            .first()
        )
        if version:
            return version

    if product.current_draft_version_id:
        version = (
            session.query(ProductVersion)
            .filter(ProductVersion.id == product.current_draft_version_id)
            .first()
        )
        if version:
            refresh_product_version_artifacts(session, version)
            return version

    version = ensure_product_baseline_version(session, product)
    if version.version_status == ProductVersionStatus.DRAFT:
        refresh_product_version_artifacts(session, version)
    return version


def get_version_audit_context(version: Optional[ProductVersion]) -> Dict[str, Any]:
    """Public wrapper for version context embedding."""
    return _version_context(version)
