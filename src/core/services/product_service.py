"""Product business logic. Pure functions; sessions and actors are passed in.

Routers are HTTP-only; this module owns:
- access control checks
- mutation rules (system_locked, draft_under_review)
- audit logging
- composing baseline-version + version-status side effects

These functions never call session.commit() — the caller (typically
the get_session() Depends) owns the transaction boundary.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser
from core.exceptions import ConflictError, ForbiddenError, NotFoundError, SystemLockedError, ValidationError
from core.models.entities import Application, Job, Product, Project, ProjectMember
from core.models.user import UserRole, is_elevated_role
from core.services.audit_service import log_audit, serialize_product
from core.services.handover_service import evaluate_product_handover_completeness
from core.services.project_version_service import (
    can_mutate_project_scope,
    ensure_project_editable_draft,
)
from core.services.support_group_service import is_project_editor as _actor_is_project_editor, user_has_project_group_access


# ── Access control ─────────────────────────────────────────────────────


def _check_project_access(
    session: Session,
    *,
    project_id: int,
    actor: CurrentUser,
    require_owner: bool = False,
) -> Project:
    """Resolve project + assert actor can access it. Raises NotFoundError / ForbiddenError."""
    project = session.query(Project).filter(Project.id == project_id).first()
    if project is None:
        raise NotFoundError("Project not found")

    if is_elevated_role(actor.role):  # admin or relayops_member (platform elevation)
        return project
    if project.is_system == 1 and not require_owner:
        return project
    if project.owner_id == actor.user_id:
        return project
    if require_owner:
        # product_member is a project editor with owner-equivalent rights over
        # the project's assets (member management / ownership stay owner-only).
        if _actor_is_project_editor(session, project_id, actor.user_id):
            return project
        raise ForbiddenError("Not authorized")

    is_member = (
        session.query(ProjectMember)
        .filter(ProjectMember.project_id == project_id, ProjectMember.user_id == actor.user_id)
        .first()
        is not None
    )
    if is_member:
        return project
    if user_has_project_group_access(session, project, actor.groups):
        return project
    raise ForbiddenError("Not authorized")


def _load_product_or_404(session: Session, product_id: int) -> Product:
    product = session.query(Product).filter(Product.id == product_id).first()
    if product is None:
        raise NotFoundError("Product not found")
    return product


# ── Use cases ──────────────────────────────────────────────────────────


def _ensure_project_scope_mutable(session: Session, project: Project) -> None:
    """Block scope edits while the project's draft is under review; otherwise open a draft.

    Editing any product / job / app is a scope change against the owning
    project's editable version. Mirrors the old per-product lock, now at the
    project level (version control lives on the project).
    """
    is_mutable, lock_err = can_mutate_project_scope(session, project)
    if not is_mutable:
        if lock_err == "draft_under_review":
            raise ConflictError("Project handover is under review and cannot be edited")
        raise ValidationError(lock_err or "scope_locked")
    try:
        ensure_project_editable_draft(session, project)
    except ValueError as exc:
        if str(exc) == "draft_under_review":
            raise ConflictError("Project handover is under review and cannot be edited")
        raise ValidationError(str(exc))


def create(session: Session, *, project_id: int, name: str, actor: CurrentUser) -> Product:
    project = _check_project_access(session, project_id=project_id, actor=actor, require_owner=True)
    if project.is_system == 1:
        raise SystemLockedError("System-managed project is read-only")
    _ensure_project_scope_mutable(session, project)

    product = Product(
        project_id=project_id,
        name=name,
        latest_version_number="1",
        is_system=0,
    )
    session.add(product)
    session.flush()
    session.refresh(product)

    log_audit(
        user_id=actor.user_id,
        action="create",
        entity_type="product",
        entity_id=product.id,
        new_value=serialize_product(product),
    )
    return product


def list_for_project(session: Session, *, project_id: int, actor: CurrentUser) -> List[Product]:
    _check_project_access(session, project_id=project_id, actor=actor)
    return (
        session.query(Product)
        .filter(Product.project_id == project_id)
        .order_by(Product.created_at.desc())
        .all()
    )


def get(session: Session, *, product_id: int, actor: CurrentUser) -> Product:
    product = _load_product_or_404(session, product_id)
    _check_project_access(session, project_id=product.project_id, actor=actor)
    return product


def copy(session: Session, *, product_id: int, actor: CurrentUser) -> Product:
    """Duplicate a product within its owning project, including every
    job/app and their scenarios. Name suffix bumps from "(copy)" if it
    collides with an existing product name in the same project.
    """
    from core.services.project_service import _deep_clone_product_assets, next_copy_name

    source = _load_product_or_404(session, product_id)
    project = _check_project_access(session, project_id=source.project_id, actor=actor, require_owner=True)
    if project.is_system == 1:
        raise SystemLockedError("System-managed project is read-only")
    _ensure_project_scope_mutable(session, project)

    sibling_names = [
        row.name
        for row in session.query(Product.name).filter(Product.project_id == source.project_id).all()
    ]
    new_name = next_copy_name(sibling_names, source.name)

    new_product = Product(
        project_id=source.project_id,
        name=new_name,
        latest_version_number="1",
        is_system=0,
    )
    session.add(new_product)
    session.flush()
    _deep_clone_product_assets(session, source, new_product)
    session.refresh(new_product)

    log_audit(
        user_id=actor.user_id,
        action="create",
        entity_type="product",
        entity_id=new_product.id,
        new_value=serialize_product(new_product),
    )
    return new_product


def update(session: Session, *, product_id: int, name: Optional[str] = None, actor: CurrentUser) -> Product:
    product = _load_product_or_404(session, product_id)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    project = _check_project_access(session, project_id=product.project_id, actor=actor, require_owner=True)
    _ensure_project_scope_mutable(session, project)

    old_val = serialize_product(product)
    if name is not None:
        product.name = name
    product.updated_at = datetime.utcnow()
    session.flush()
    session.refresh(product)

    log_audit(
        user_id=actor.user_id,
        action="update",
        entity_type="product",
        entity_id=product.id,
        old_value=old_val,
        new_value=serialize_product(product),
    )
    return product


def delete(session: Session, *, product_id: int, actor: CurrentUser) -> None:
    product = _load_product_or_404(session, product_id)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    project = _check_project_access(session, project_id=product.project_id, actor=actor, require_owner=True)
    _ensure_project_scope_mutable(session, project)

    old_val = serialize_product(product)
    session.delete(product)
    log_audit(
        user_id=actor.user_id,
        action="delete",
        entity_type="product",
        entity_id=product_id,
        old_value=old_val,
    )


_EMAIL_TEMPLATE_FIELDS = ("to", "cc", "subject", "body")


def _normalize_email_template(template: Optional[dict]) -> dict:
    """Coerce arbitrary input to the canonical 4-field owner-email shape."""
    src = template if isinstance(template, dict) else {}
    return {key: str(src.get(key, "") or "") for key in _EMAIL_TEMPLATE_FIELDS}


def _email_template_is_empty(template: Optional[dict]) -> bool:
    """True when no field carries a non-blank value (== "no template set")."""
    if not isinstance(template, dict):
        return True
    return not any(str(template.get(key, "") or "").strip() for key in _EMAIL_TEMPLATE_FIELDS)


def apply_email_template(
    session: Session,
    *,
    product_id: int,
    template: Optional[dict],
    mode: str = "fill_empty",
    scope: str = "product",
    job_id: Optional[int] = None,
    application_id: Optional[int] = None,
    actor: CurrentUser,
) -> dict:
    """Copy one send-email step's owner-email template onto many scenarios.

    Backs the "set as default / replace all" affordance in the scenario
    editor: the template the user authored on a single action step is written
    onto every scenario in the chosen ``scope`` (the whole product, or one
    job/app). ``mode`` is ``fill_empty`` (only scenarios without a template —
    "set as default") or ``overwrite`` (every target — "replace all").

    Returns ``{"updated": int, "scope": str, "mode": str}``.
    """
    if mode not in ("fill_empty", "overwrite"):
        raise ValidationError("mode must be 'fill_empty' or 'overwrite'")
    if scope not in ("product", "job", "app"):
        raise ValidationError("scope must be 'product', 'job', or 'app'")

    product = _load_product_or_404(session, product_id)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    project = _check_project_access(session, project_id=product.project_id, actor=actor, require_owner=True)
    _ensure_project_scope_mutable(session, project)

    tpl = _normalize_email_template(template)
    overwrite = mode == "overwrite"

    targets: List = []
    if scope == "job":
        if job_id is None:
            raise ValidationError("job_id is required for scope='job'")
        job = session.query(Job).filter(Job.id == job_id, Job.product_id == product_id).first()
        if job is None:
            raise NotFoundError("Job not found")
        targets.extend(job.failure_scenarios)
    elif scope == "app":
        if application_id is None:
            raise ValidationError("application_id is required for scope='app'")
        app = (
            session.query(Application)
            .filter(Application.id == application_id, Application.product_id == product_id)
            .first()
        )
        if app is None:
            raise NotFoundError("Application not found")
        targets.extend(app.recovery_scenarios)
    else:  # product — every scenario under every job and app
        for job in session.query(Job).filter(Job.product_id == product_id).all():
            targets.extend(job.failure_scenarios)
        for app in session.query(Application).filter(Application.product_id == product_id).all():
            targets.extend(app.recovery_scenarios)

    now = datetime.utcnow()
    updated = 0
    for scenario in targets:
        if not overwrite and not _email_template_is_empty(scenario.email_template):
            continue
        scenario.email_template = dict(tpl)
        scenario.updated_at = now
        updated += 1

    session.flush()

    log_audit(
        user_id=actor.user_id,
        action="update",
        entity_type="product",
        entity_id=product_id,
        new_value={
            "email_template_apply": {
                "scope": scope,
                "mode": mode,
                "job_id": job_id,
                "application_id": application_id,
                "updated": updated,
            }
        },
    )
    return {"updated": updated, "scope": scope, "mode": mode}


def get_handover_completeness(session: Session, *, product_id: int, actor: CurrentUser):
    product = _load_product_or_404(session, product_id)
    _check_project_access(session, project_id=product.project_id, actor=actor)
    return evaluate_product_handover_completeness(session, product_id)


def check_now(session: Session, *, product_id: int) -> dict:
    """Force a one-shot job/app/MMP check for a single product.

    Used by the user-facing "Check Now" button. Does not gate by user role
    beyond the route's get_current_user; product access is enforced upstream.
    """
    from core.config import get_config
    from core.integrations import (
        AppInterface, AppTarget, CmlApiError, ControlInterface, MmpInterface,
        is_within_cron_schedule, validate_timestamp,
    )
    from core.issue_management.issue_engine import create_issue
    from core.models.entities import Application, IssueType, Job, Product, ProductStatus

    cfg = get_config()
    before = datetime.utcnow()

    product = session.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise NotFoundError(f"Product {product_id} not found")
    # Allow Check Now for products that haven't been approved yet (DRAFT /
    # PENDING_REVIEW) so owners can validate asset bindings before submitting
    # for review. Only block ARCHIVED — those products are decommissioned and
    # surfacing fresh anomalies on them would be noise.
    if product.status == ProductStatus.ARCHIVED:
        raise ValidationError(f"Product {product_id} is archived")

    results = {"job_checks": [], "app_checks": [], "mmp_checks": [], "issues_created": []}
    control = ControlInterface(
        base_url=cfg.cml_platform_base_url,
        api_key=cfg.cml_platform_api_key or None,
        timeout=float(cfg.cml_platform_timeout_seconds),
        verify_ssl=cfg.cml_platform_verify_ssl,
        ca_bundle=cfg.cml_platform_ca_bundle_path or None,
        default_project_name=cfg.cml_platform_default_project_name or None,
    )
    staleness_threshold = cfg.cml_platform_job_staleness_threshold_minutes

    from core.integrations.normalization import _normalize_controlm_status

    jobs = session.query(Job).filter(Job.product_id == product_id).all()
    for job in jobs:
        # Skip jobs whose CML binding hasn't been resolved yet — the user
        # likely created the row before CML was reachable; surfacing a
        # success here would be misleading.
        if not job.cml_project_id or not job.cml_job_id:
            results["job_checks"].append({
                "job_id": job.id,
                "name": job.cml_job_name or job.control_m_job_name,
                "status": None,
                "binding": "unresolved",
            })
            continue

        try:
            runs = control.list_job_runs(
                job.cml_project_id, job.cml_job_id, limit=1
            )
        except CmlApiError as exc:
            # The attempt still counts as a "check" — surface it on the card.
            job.last_checked_at = datetime.utcnow()
            results["job_checks"].append({
                "job_id": job.id,
                "name": job.cml_job_name or job.control_m_job_name,
                "error": f"CML API: {exc.message}",
            })
            continue

        # Successful poll (regardless of run history) updates the timestamp.
        job.last_checked_at = datetime.utcnow()

        if not runs:
            results["job_checks"].append({
                "job_id": job.id,
                "name": job.cml_job_name or job.control_m_job_name,
                "status": None,
                "note": "no run history",
            })
            continue

        latest = runs[0]
        cml_engine_status = str(latest.get("status") or "")
        try:
            normalized = _normalize_controlm_status(cml_engine_status)
        except Exception:
            normalized = "unknown"
        last_run_ts = (
            latest.get("finished_at")
            or latest.get("running_at")
            or latest.get("created_at")
        )
        results["job_checks"].append({
            "job_id": job.id,
            "name": job.cml_job_name or job.control_m_job_name,
            "status": normalized,
            "cml_status": cml_engine_status,
            "cml_run_id": latest.get("id"),
            "last_run": last_run_ts,
        })

        # Mirror CmlChecker._evaluate_run so "Check Now" and the periodic loop
        # agree: a job is healthy only when its latest run COMPLETED
        # SUCCESSFULLY within its SLA window, and each create_issue carries the
        # run id as a dedup key for per-event (not per-open-issue) dedup.
        from core.checker.cml_checker import _safety_factor_for
        run_id = latest.get("id")
        dedup_key = str(run_id) if run_id else None
        cron = job.schedule_cron or job.control_m_cron or None
        if normalized == "failed":
            # A run that failed outside the job's cron window (e.g. a Monday run
            # for a "0 5 * * 2-5" Tue–Fri job) is recorded in job_checks above
            # but does NOT raise an Issue — the schedule didn't ask for that
            # execution. Mirrors CmlChecker._evaluate_run; fails open when the
            # cron is empty/unparseable or the trigger time is missing.
            triggered_at = (
                latest.get("created_at")
                or latest.get("running_at")
                or latest.get("finished_at")
            )
            if not is_within_cron_schedule(cron, triggered_at):
                results["job_checks"][-1]["issue_suppressed"] = "out_of_schedule"
                continue
            issue = create_issue(
                issue_type=IssueType.JOB_FAILED,
                title=f"CML Job failed: {job.cml_job_name or job.control_m_job_name}",
                description=(
                    f"CML reported {cml_engine_status} for the latest run.\n"
                    f"Ops Job ID: {job.id}\nProduct ID: {product_id}\n"
                    f"CML Project: {job.cml_project_id}\n"
                    f"CML Job: {job.cml_job_id}\n"
                    f"Run ID: {run_id}\n"
                    f"Finished At: {last_run_ts or 'unknown'}\n"
                    f"Failure Reason: {latest.get('failure_reason') or 'not provided'}"
                ),
                product_id=product_id,
                job_id=job.id,
                dedup_key=dedup_key,
            )
            if issue:
                results["issues_created"].append({"type": "JOB_FAILED", "id": issue.id})
        elif normalized == "completed":
            staleness = validate_timestamp(
                last_run_date=last_run_ts,
                sla_threshold_minutes=staleness_threshold,
                cron=(job.schedule_cron or job.control_m_cron or None),
                sla_safety_factor=_safety_factor_for(getattr(job, "sla_preset", None)),
                sla_override_minutes=getattr(job, "sla_custom_minutes", None),
            )
            if staleness.is_stale:
                issue = create_issue(
                    issue_type=IssueType.JOB_STALE,
                    title=f"CML Job stale (miss): {job.cml_job_name or job.control_m_job_name}",
                    description=(
                        f"CML reports the latest run is {cml_engine_status} but its "
                        f"finished_at is stale.\n"
                        f"Ops Job ID: {job.id}\nProduct ID: {product_id}\n"
                        f"CML Project: {job.cml_project_id}\n"
                        f"CML Job: {job.cml_job_id}\n"
                        f"Run ID: {run_id}\n"
                        f"Finished At: {last_run_ts}\n"
                        f"Age: {staleness.age_minutes:.0f} min\n"
                        f"SLA Threshold: {staleness.threshold_minutes} min "
                        f"(per-job, derived from cron when available; "
                        f"falls back to global {staleness_threshold} min)\n"
                        f"Reason: {staleness.reason}"
                    ),
                    product_id=product_id,
                    job_id=job.id,
                    dedup_key=dedup_key,
                )
                if issue:
                    results["issues_created"].append({"type": "JOB_STALE", "id": issue.id})
        elif normalized == "stopped":
            issue = create_issue(
                issue_type=IssueType.JOB_STALE,
                title=f"CML Job stopped: {job.cml_job_name or job.control_m_job_name}",
                description=(
                    f"CML reports the job's latest run was stopped.\n"
                    f"Ops Job ID: {job.id}\nProduct ID: {product_id}\n"
                    f"CML Project: {job.cml_project_id}\n"
                    f"CML Job: {job.cml_job_id}\n"
                    f"Run ID: {run_id}\n"
                    f"Finished At: {last_run_ts or 'unknown'}"
                ),
                product_id=product_id,
                job_id=job.id,
                dedup_key=dedup_key,
            )
            if issue:
                results["issues_created"].append({"type": "JOB_STALE", "id": issue.id})
        elif normalized == "running":
            # In-flight run that has overrun its expected runtime (anchored on
            # when it started) is a timeout/miss — see CmlChecker._evaluate_run.
            started_ts = latest.get("running_at") or latest.get("created_at")
            if started_ts:
                staleness = validate_timestamp(
                    last_run_date=started_ts,
                    sla_threshold_minutes=staleness_threshold,
                    cron=(job.schedule_cron or job.control_m_cron or None),
                    sla_safety_factor=_safety_factor_for(getattr(job, "sla_preset", None)),
                    sla_override_minutes=getattr(job, "sla_custom_minutes", None),
                )
                if staleness.is_stale:
                    issue = create_issue(
                        issue_type=IssueType.JOB_STALE,
                        title=f"CML Job timed out: {job.cml_job_name or job.control_m_job_name}",
                        description=(
                            f"Latest run is still {cml_engine_status} but has been "
                            f"in-flight past its expected runtime — it should already "
                            f"have completed.\n"
                            f"Ops Job ID: {job.id}\nProduct ID: {product_id}\n"
                            f"CML Project: {job.cml_project_id}\n"
                            f"CML Job: {job.cml_job_id}\n"
                            f"Run ID: {run_id}\n"
                            f"Started At: {started_ts}\n"
                            f"Running For: {staleness.age_minutes:.0f} min\n"
                            f"SLA Threshold: {staleness.threshold_minutes} min "
                            f"(per-job, derived from cron when available; "
                            f"falls back to global {staleness_threshold} min)\n"
                            f"Reason: {staleness.reason}"
                        ),
                        product_id=product_id,
                        job_id=job.id,
                        dedup_key=dedup_key,
                    )
                    if issue:
                        results["issues_created"].append({"type": "JOB_STALE", "id": issue.id})

    # Reuse the live Controller's AppChecker when available so manual checks
    # share the consecutive-failure counter state with periodic ticks.
    from core import controller as monitoring_controller
    instance = monitoring_controller.get_instance()
    if instance is not None:
        app_checker = instance.app_checker
    else:
        from core.checker import AppChecker as _AppChecker
        app_checker = _AppChecker(
            app_interface=AppInterface(
                control_interface=control,
                timeout=float(cfg.cml_platform_timeout_seconds),
            ),
            failure_threshold=cfg.cml_platform_health_failure_threshold,
        )

    apps = session.query(Application).filter(Application.product_id == product_id).all()
    for app in apps:
        if not app.cml_project_id or not app.cml_application_id:
            results["app_checks"].append({
                "app_id": app.id,
                "skipped": "no resolved CML binding (cml_project_id / cml_application_id not set)",
            })
            continue
        target = AppTarget(
            app_id=app.id,
            product_id=product_id,
            cml_project_id=app.cml_project_id,
            cml_application_id=app.cml_application_id,
            app_type=(app.cml_app_type or "generic").lower(),
            serving_url=app.cml_serving_url or "",
        )
        check_result = app_checker.check_app(target, session=session)
        is_healthy = check_result.status.name == "HEALTHY"
        results["app_checks"].append({
            "app_id": app.id,
            "serving_url": target.serving_url,
            "app_type": target.app_type,
            "healthy": is_healthy,
            "status": check_result.status.name,
        })
        if check_result.status.name == "ANOMALY" and check_result.anomaly_event:
            ev = check_result.anomaly_event
            issue = create_issue(
                issue_type=IssueType.APP_OFFLINE,
                title=ev.title,
                description=ev.description,
                product_id=product_id,
                app_id=app.id,
            )
            if issue:
                results["issues_created"].append({"type": "APP_OFFLINE", "id": issue.id})

    # Use the same drift→Issue dispatch helpers the periodic MmpChecker
    # uses, so "Check Now" and the background cadence stay in lockstep
    # for both the drift description format (with sub-flag breakdown) and
    # the three additional concern Issues (fairness / approval /
    # unapproved-exp-run).
    from core.checker.mmp_checker import (
        build_drift_subflag_lines,
        build_extra_concern_events,
        build_mmp_status_lines,
        build_recovery_events,
    )
    from core.issue_management.issue_engine import auto_close_open_issues

    mmp = MmpInterface(
        base_url=cfg.mmp_base_url,
        bearer_token=cfg.mmp_bearer_token,
        refresh_token=cfg.mmp_refresh_token,
        verify_ssl=cfg.mmp_verify_ssl,
        ca_bundle=cfg.mmp_ca_bundle_path or None,
        timeout=float(cfg.mmp_timeout_seconds),
    )
    # Map the extra-concern AnomalyType → IssueType + label. Only pending
    # approval + pending review are emitted by build_extra_concern_events now;
    # the periodic checker uses assign.py's _ANOMALY_TO_ISSUE_TYPE, "Check Now"
    # creates Issues directly so it duplicates that mapping here.
    from core.checker.base import AnomalyType as _AnomalyType
    _EXTRA_ISSUE_TYPE = {
        _AnomalyType.MMP_RUN_PENDING_APPROVAL: (IssueType.MMP_RUN_PENDING_APPROVAL, "MMP_RUN_PENDING_APPROVAL"),
        _AnomalyType.MMP_PENDING_REVIEW: (IssueType.MMP_PENDING_REVIEW, "MMP_PENDING_REVIEW"),
    }
    for job in jobs:
        if not job.has_mmp_dependency or not job.mmp_project_id:
            continue
        mmp_result = mmp.check_drift(
            repo_name=job.mmp_project_id,
            model_name=job.mmp_model_id or "",
            job=job,
        )
        results["mmp_checks"].append({"job_id": job.id, "project_id": job.mmp_project_id, "action": mmp_result.action})
        # Fold the standalone drift Issue into the pending-review/-approval
        # Issue when the same run is both drifted and pending (mirrors the
        # periodic MmpChecker._map_drift_result): the pending Issue below already
        # embeds the drift status block, so a separate drift ticket for the same
        # run is a confusing duplicate. The drift fact still rides along in that
        # Issue's status block.
        if mmp_result.action == "create_issue" and not mmp_result.drift_superseded_by_pending:
            cml_model_id = mmp_result.cml_model_id
            drift_details = mmp_result.drift_details or "N/A"
            sub_flag_lines = build_drift_subflag_lines(mmp_result)
            sub_flag_block = f"\n{sub_flag_lines}\n" if sub_flag_lines else ""
            status_block = f"\n{build_mmp_status_lines(mmp_result)}\n"
            drift_url = cfg.mmp_model_web_url(mmp_result.mmp_project_numeric_id)
            issue = create_issue(
                issue_type=IssueType.MMP_DRIFT,
                title=f"MMP Drift detected: {job.mmp_project_id}/{job.mmp_model_id}",
                description=(
                    f"MMP drift detected for model.\n\n"
                    f"MMP Project: {job.mmp_project_id}\n"
                    f"MMP Model: {job.mmp_model_id}\n"
                    f"CML Model ID: {cml_model_id if cml_model_id is not None else 'N/A'}\n"
                    f"Ops Job ID: {job.id}\nProduct ID: {product_id}\n"
                    f"Details: {drift_details}\n"
                    f"Reason: {mmp_result.reason}"
                    + (f"\nAction on MMP: {drift_url}\n" if drift_url else "")
                    + f"{sub_flag_block}"
                    f"{status_block}"
                ),
                product_id=product_id,
                job_id=job.id,
                external_url=drift_url or None,
            )
            if issue:
                results["issues_created"].append({"type": "MMP_DRIFT", "id": issue.id})

        # Pending approval / pending review ride alongside drift on the same
        # HTTP call. Each that came back true gets its own Issue, deep-linked
        # to the MMP web UI for action.
        if mmp_result.action in ("create_issue", "close_issue"):
            for extra in build_extra_concern_events(mmp_result, job):
                mapping = _EXTRA_ISSUE_TYPE.get(extra.anomaly_type)
                if mapping is None:
                    continue
                issue_type, label = mapping
                issue = create_issue(
                    issue_type=issue_type,
                    title=extra.title,
                    description=extra.description,
                    product_id=product_id,
                    job_id=job.id,
                    # Same per-run dedup as the periodic checker (see
                    # build_extra_concern_events): one ticket per pending run
                    # across all statuses, so "Check Now" can't re-create a
                    # ticket that was already raised + resolved for this run.
                    dedup_key=extra.dedup_key,
                    external_url=(extra.metadata or {}).get("external_url") or None,
                )
                if issue:
                    results["issues_created"].append({"type": label, "id": issue.id})

            # Auto-close any open Issue whose signal recovered (drift /
            # approval / review back to normal). Mutations land on the request
            # session; the get_session dependency owns the commit.
            for recovery in build_recovery_events(mmp_result, job):
                auto_close_open_issues(
                    session,
                    issue_type=recovery.issue_type,
                    job_id=job.id,
                    product_id=product_id,
                    resolution_description=recovery.reason,
                )

    return {
        "product_id": product_id,
        "checked_at": before.isoformat(),
        "results": results,
        "summary": f"{len(results['issues_created'])} issue(s) created",
    }
