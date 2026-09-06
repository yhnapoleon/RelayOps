"""Job + failure-scenario business logic. Pure functions, session injected."""
from __future__ import annotations

from datetime import datetime
from typing import List

from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser
from api.schema import JobCreate, JobDuplicateRequest, JobFailureScenarioCreate, JobFailureScenarioUpdate, JobUpdate
from core.exceptions import ConflictError, ForbiddenError, NotFoundError, SystemLockedError, ValidationError
from core.models.entities import (
    FallbackOwnerType,
    Job,
    JobFailureScenario,
    JobFailureScenarioType,
    Product,
    Project,
    ProjectMember,
)
from core.models.user import UserRole, is_elevated_role
from core.services.audit_service import log_audit, serialize_job, serialize_job_failure_scenario
from core.services.cml_binding_resolver import (
    build_control_interface,
    resolve_job_id as _resolve_cml_job_id,
    resolve_project_id as _resolve_cml_project_id,
)
from core.services.project_version_service import can_mutate_project_scope, ensure_project_editable_draft
from core.services.support_group_service import is_project_editor as _actor_is_project_editor, resolve_support_group_snapshot, user_has_project_group_access


# Whitelist of accepted SLA strictness presets — see Job.sla_preset. Anything
# else (empty string, "Normal " with trailing space, unknown value) collapses
# to ``None`` so the staleness checker falls back to the global default.
_SLA_PRESET_VALUES = {"strict", "normal", "loose"}


def _normalize_sla_preset(raw):
    """Lower-case + trim + whitelist. Returns ``None`` for any unknown value."""
    if raw is None:
        return None
    key = str(raw).strip().lower()
    return key if key in _SLA_PRESET_VALUES else None


def _reconcile_sla(preset, custom_minutes):
    """Enforce mutually-exclusive SLA config and return ``(preset, custom)``.

    ``sla_preset`` and ``sla_custom_minutes`` are stored in two independent
    columns, but the staleness checker lets a non-null ``sla_custom_minutes``
    silently override the preset (see ``staleness.validate_timestamp``). If both
    are persisted, a job can read e.g. "Normal" while actually being governed by
    a leftover flat threshold — producing daily false "miss" alerts on jobs that
    only run once a day. We collapse to a single source of truth: a non-null
    custom flat threshold is "custom mode" and clears the preset; otherwise the
    preset governs and any custom value is dropped.
    """
    if custom_minutes is not None:
        return None, custom_minutes
    return preset, None


# ── Access control & validation ────────────────────────────────────────


def _check_product_access(
    session: Session,
    *,
    product_id: int,
    actor: CurrentUser,
    require_owner: bool = False,
) -> Product:
    product = session.query(Product).filter(Product.id == product_id).first()
    if product is None:
        raise NotFoundError("Product not found")
    project = session.query(Project).filter(Project.id == product.project_id).first()
    if project is None:
        raise NotFoundError("Product not found")

    if is_elevated_role(actor.role):  # admin or relayops_member (platform elevation)
        return product
    if product.is_system == 1 and not require_owner:
        return product
    if project.owner_id == actor.user_id:
        return product
    if require_owner:
        # product_member is a project editor with owner-equivalent rights over
        # the project's assets (member management / ownership stay owner-only).
        if _actor_is_project_editor(session, project.id, actor.user_id):
            return product
        raise ForbiddenError("Not authorized")
    is_member = (
        session.query(ProjectMember)
        .filter(ProjectMember.project_id == project.id, ProjectMember.user_id == actor.user_id)
        .first()
        is not None
    )
    if is_member:
        return product
    if user_has_project_group_access(session, project, actor.groups):
        return product
    raise ForbiddenError("Not authorized")


def _ensure_mutable(session: Session, product: Product) -> None:
    project = session.query(Project).filter(Project.id == product.project_id).first()
    if project is None:
        raise NotFoundError("Owning project not found")
    is_mutable, lock_err = can_mutate_project_scope(session, project)
    if not is_mutable:
        if lock_err == "draft_under_review":
            raise ConflictError("Project handover is under review and cannot be edited")
        raise ValidationError(lock_err)
    ensure_project_editable_draft(session, project)


def _validate_scenario_body(body) -> None:
    if body.scenario_type is not None and body.scenario_type not in JobFailureScenarioType.ALL:
        raise ValidationError(f"Invalid job scenario_type. Must be one of: {JobFailureScenarioType.ALL}")
    if body.fallback_owner_type is not None and body.fallback_owner_type not in FallbackOwnerType.ALL:
        raise ValidationError(f"Invalid fallback_owner_type. Must be one of: {FallbackOwnerType.ALL}")
    if body.threshold_operator is not None and body.threshold_operator not in {"", ">", ">=", "<", "<=", "="}:
        raise ValidationError("Invalid threshold_operator. Must be one of: '', >, >=, <, <=, =")


def _resolve_cml_job(
    owning_project: Project,
    cml_project_name_override: str | None,
    job_name: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve a CML job by name, optionally targeting a CML project other
    than the one the owning Ops Project is bound to.

    A non-empty ``cml_project_name_override`` binds this Job to a different
    CML project than its Ops parent — the override-vs-inherit semantics mirror
    ``app_service._resolve_cml_app``. Empty / None means inherit.

    Returns ``(cml_project_id, cml_job_id, error)``. Whether the returned id
    came from override or inheritance is encoded in the row by the caller via
    ``job.cml_project_name`` (non-empty = override, empty = inheriting).
    """
    from core.services.cml_binding_resolver import (
        build_control_interface,
        resolve_job_id as _resolve_cml_job_id,
    )

    override = (cml_project_name_override or "").strip()

    if override:
        # Asset-level override: resolve the override project independently.
        control = build_control_interface()
        pid, perr = _resolve_cml_project_id(control, override)
        if perr:
            return None, None, perr
        if not pid:
            return None, None, f"CML project '{override}' not found"
        if not job_name:
            return pid, None, None
        jid, jerr = _resolve_cml_job_id(control, pid, job_name)
        return pid, jid, jerr

    # Inheritance path — use the owning Ops Project's CML binding.
    if owning_project is None or not (owning_project.cml_project_id or "").strip():
        if owning_project is not None and (owning_project.cml_binding_error or "").strip():
            return None, None, f"Project unresolved: {owning_project.cml_binding_error}"
        return None, None, "Owning Ops Project has no CML Project Name set"
    if not job_name:
        return owning_project.cml_project_id, None, None
    control = build_control_interface()
    jid, jerr = _resolve_cml_job_id(control, owning_project.cml_project_id, job_name)
    return owning_project.cml_project_id, jid, jerr


# ── Job use cases ──────────────────────────────────────────────────────


def _owning_project(session: Session, product_id: int) -> Project | None:
    """Resolve the Ops Project that owns a Product, for CML binding inheritance."""
    product = session.query(Product).filter(Product.id == product_id).first()
    if product is None:
        return None
    return session.query(Project).filter(Project.id == product.project_id).first()


def create(session: Session, *, product_id: int, body: JobCreate, actor: CurrentUser) -> Job:
    product = _check_product_access(session, product_id=product_id, actor=actor, require_owner=True)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, product)

    # The Job UI exposes a single "Job Name" input and a single "Cron" input.
    # control_m_* and cml_* columns mirror each other — whichever side the
    # caller filled wins, the other is back-filled.
    raw_cml_job_name = (body.cml_job_name or "").strip()
    raw_control_m_job_name = (body.control_m_job_name or "").strip()
    raw_schedule_cron = (body.schedule_cron or "").strip()
    raw_control_m_cron = (body.control_m_cron or "").strip()

    effective_job_name = raw_cml_job_name or raw_control_m_job_name
    effective_cron = raw_schedule_cron or raw_control_m_cron

    # CML project binding defaults to inheriting from the owning Ops Project.
    # body.cml_project_name acts as an optional override — when set, this Job
    # targets a different CML project than its parent (e.g. training jobs in
    # ``material-classifier`` while the parent Ops Project is bound to the
    # serving project). Empty override = inherit.
    cml_project_name_override = (body.cml_project_name or "").strip()
    owning_project = session.query(Project).filter(Project.id == product.project_id).first()
    cml_project_id, cml_job_id, cml_binding_error = _resolve_cml_job(
        owning_project, cml_project_name_override, effective_job_name
    )

    sla_preset, sla_custom_minutes = _reconcile_sla(
        _normalize_sla_preset(body.sla_preset), body.sla_custom_minutes
    )
    job = Job(
        product_id=product_id,
        mmp_project_id=body.mmp_project_id or "",
        mmp_model_id=body.mmp_model_id or "",
        control_m_job_name=effective_job_name,
        control_m_cron=effective_cron,
        # Store the override verbatim (empty = inherit from parent Ops Project).
        cml_project_name=cml_project_name_override,
        cml_job_name=effective_job_name,
        cml_project_id=cml_project_id,
        cml_job_id=cml_job_id,
        cml_binding_error=cml_binding_error,
        schedule_cron=effective_cron,
        description=body.description or "",
        dependencies=body.dependencies,
        failure_strategy_summary=body.failure_strategy_summary or "",
        dependency_notes=body.dependency_notes or "",
        owner_contact=body.owner_contact or "",
        runbook_required=body.runbook_required,
        has_mmp_dependency=body.has_mmp_dependency,
        sla_preset=sla_preset,
        sla_custom_minutes=sla_custom_minutes,
        is_system=0,
    )
    sg_id, sg_name = resolve_support_group_snapshot(session, body.support_group_id, body.support_group)
    job.support_group_id = sg_id
    job.support_group_name_snapshot = sg_name
    job.support_group = sg_name
    session.add(job)
    session.flush()
    session.refresh(job)
    log_audit(
        user_id=actor.user_id,
        action="create",
        entity_type="job",
        entity_id=job.id,
        new_value=serialize_job(job),
    )
    return job


def list_for_product(session: Session, *, product_id: int, actor: CurrentUser) -> List[Job]:
    _check_product_access(session, product_id=product_id, actor=actor)
    return (
        session.query(Job)
        .filter(Job.product_id == product_id)
        .order_by(Job.created_at.desc())
        .all()
    )


def update(session: Session, *, job_id: int, body: JobUpdate, actor: CurrentUser) -> Job:
    job = session.query(Job).filter(Job.id == job_id).first()
    if job is None:
        raise NotFoundError("Job not found")
    product = _check_product_access(session, product_id=job.product_id, actor=actor, require_owner=True)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, product)

    old_val = serialize_job(job)
    # For these fields the schema's `Optional = None` doubles as both "user
    # didn't send this field" and "user explicitly cleared it to null", so a
    # blind `value is not None` check makes them unclearable. SLA preset /
    # custom minutes need to be nullable from the UI (switching back to the
    # default), so use model_fields_set to honour an explicit null.
    fields_sent = body.model_fields_set
    for field in (
        "mmp_project_id",
        "mmp_model_id",
        "control_m_job_name",
        "control_m_cron",
        "cml_project_name",
        "cml_job_name",
        "schedule_cron",
        "description",
        "dependencies",
        "failure_strategy_summary",
        "dependency_notes",
        "owner_contact",
        "runbook_required",
        "has_mmp_dependency",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(job, field, value)

    # SLA preset and custom-minutes are mutually exclusive (see _reconcile_sla).
    # Only touch them when the caller sent at least one, so an unrelated edit
    # never disturbs the existing SLA config. sla_preset goes through
    # normalisation so we never write garbage like "Strict " into the column.
    preset_sent = "sla_preset" in fields_sent
    custom_sent = "sla_custom_minutes" in fields_sent
    if custom_sent and body.sla_custom_minutes is not None:
        # Custom flat threshold wins and clears any lingering preset, so the
        # job can't read "Normal" while silently running on the override.
        job.sla_custom_minutes = body.sla_custom_minutes
        job.sla_preset = None
    elif preset_sent:
        # Explicitly choosing a preset clears any leftover custom value that
        # would otherwise keep overriding it.
        job.sla_preset = _normalize_sla_preset(body.sla_preset)
        job.sla_custom_minutes = None
    elif custom_sent:
        # Only an explicit null custom was sent → back to preset-driven mode.
        job.sla_custom_minutes = None

    # Mirror the single-input UI into both legacy and CML columns. We do this
    # after the bulk setattr so explicit per-side values still win, but a
    # caller that only sent control_m_job_name (the one input the new UI
    # exposes) gets cml_job_name updated for free — and vice versa.
    name_touched = (
        body.cml_job_name is not None or body.control_m_job_name is not None
    )
    if name_touched:
        unified_name = (job.cml_job_name or job.control_m_job_name or "").strip()
        job.cml_job_name = unified_name
        job.control_m_job_name = unified_name

    cron_touched = (
        body.schedule_cron is not None or body.control_m_cron is not None
    )
    if cron_touched:
        unified_cron = (job.schedule_cron or job.control_m_cron or "").strip()
        job.schedule_cron = unified_cron
        job.control_m_cron = unified_cron

    # Re-resolve when the job name OR the project override changed.
    # job.cml_project_name acts as the override sentinel — empty means inherit,
    # non-empty means this Job targets a different CML project than its parent.
    project_override_touched = body.cml_project_name is not None
    if name_touched or project_override_touched:
        owning_project = _owning_project(session, job.product_id)
        cml_project_id, cml_job_id, cml_binding_error = _resolve_cml_job(
            owning_project,
            (job.cml_project_name or "").strip(),
            (job.cml_job_name or "").strip(),
        )
        job.cml_project_id = cml_project_id
        job.cml_job_id = cml_job_id
        job.cml_binding_error = cml_binding_error
    sg_id, sg_name = resolve_support_group_snapshot(session, body.support_group_id, body.support_group)
    job.support_group_id = sg_id
    job.support_group_name_snapshot = sg_name
    job.support_group = sg_name
    job.updated_at = datetime.utcnow()
    session.flush()
    session.refresh(job)
    log_audit(
        user_id=actor.user_id,
        action="update",
        entity_type="job",
        entity_id=job.id,
        old_value=old_val,
        new_value=serialize_job(job),
    )
    return job


def duplicate(
    session: Session,
    *,
    source_job_id: int,
    body: JobDuplicateRequest,
    actor: CurrentUser,
) -> Job:
    """Duplicate a Job (and selected failure scenarios) into a target product.

    Cross-project is allowed — the target product can live in any Ops project
    the actor has write access to. CML ids are cleared so the resolver re-runs
    against the new owning project's binding; the override (cml_project_name
    on the source row) is carried over verbatim so an explicitly pinned CML
    project stays pinned.

    Selected scenarios are deep-copied; ``not_applicable_signoff_by/at`` are
    cleared because the new entity's sign-off must be re-done from scratch.
    """
    source = session.query(Job).filter(Job.id == source_job_id).first()
    if source is None:
        raise NotFoundError("Source job not found")
    # Source-side access: actor must be able to read the original product.
    _check_product_access(session, product_id=source.product_id, actor=actor)

    # Target-side access: actor must be able to mutate the target product.
    target_product = _check_product_access(
        session, product_id=body.target_product_id, actor=actor, require_owner=True
    )
    if target_product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, target_product)

    new_name = (body.control_m_job_name or "").strip()
    if not new_name:
        raise ValidationError("control_m_job_name is required")

    # Re-run CML binding against the new owning project. cml_project_name on
    # the source row acts as an override sentinel (empty = inherit), so just
    # forward whatever was on the source.
    owning_project = _owning_project(session, body.target_product_id)
    cml_project_id, cml_job_id, cml_binding_error = _resolve_cml_job(
        owning_project,
        (source.cml_project_name or "").strip(),
        new_name,
    )

    # Normalise the copied SLA config so a source row that has both a preset
    # and a leftover custom value doesn't propagate the ambiguity.
    dup_sla_preset, dup_sla_custom_minutes = _reconcile_sla(
        source.sla_preset, source.sla_custom_minutes
    )
    new_job = Job(
        product_id=body.target_product_id,
        mmp_project_id=source.mmp_project_id or "",
        mmp_model_id=source.mmp_model_id or "",
        control_m_job_name=new_name,
        control_m_cron=source.control_m_cron or "",
        cml_project_name=source.cml_project_name or "",
        cml_job_name=new_name,
        cml_project_id=cml_project_id,
        cml_job_id=cml_job_id,
        cml_binding_error=cml_binding_error,
        schedule_cron=source.schedule_cron or "",
        description=source.description or "",
        dependencies=source.dependencies,
        failure_strategy_summary=source.failure_strategy_summary or "",
        dependency_notes=source.dependency_notes or "",
        owner_contact=source.owner_contact or "",
        support_group_id=source.support_group_id,
        support_group_name_snapshot=source.support_group_name_snapshot or "",
        support_group=source.support_group or "",
        runbook_required=source.runbook_required,
        has_mmp_dependency=source.has_mmp_dependency,
        sla_preset=dup_sla_preset,
        sla_custom_minutes=dup_sla_custom_minutes,
        is_system=0,
    )
    session.add(new_job)
    session.flush()
    session.refresh(new_job)

    # Copy selected scenarios. body.scenario_ids=None means "all"; an explicit
    # list filters to that subset (empty list = copy nothing).
    source_scenarios = (
        session.query(JobFailureScenario)
        .filter(JobFailureScenario.job_id == source.id)
        .all()
    )
    if body.scenario_ids is not None:
        wanted = set(body.scenario_ids)
        source_scenarios = [s for s in source_scenarios if s.id in wanted]

    for s in source_scenarios:
        cloned = JobFailureScenario(
            job_id=new_job.id,
            scenario_type=s.scenario_type,
            scenario_name=s.scenario_name,
            condition_description=s.condition_description or "",
            detection_source=s.detection_source or "",
            diagnostic_steps=s.diagnostic_steps,
            action_steps=s.action_steps,
            verification_steps=s.verification_steps,
            escalation_target=s.escalation_target or "",
            fallback_owner_type=s.fallback_owner_type,
            threshold_operator=s.threshold_operator or "",
            threshold_value=s.threshold_value,
            threshold_feature_list=s.threshold_feature_list,
            email_template=s.email_template,
            is_not_applicable=bool(s.is_not_applicable),
            # Signoff fields intentionally cleared — the duplicate is a fresh
            # entity and any "do not apply" decision needs a new sign-off.
            not_applicable_signoff_by=None,
            not_applicable_signoff_at=None,
            is_active=bool(s.is_active),
        )
        session.add(cloned)
    session.flush()
    session.refresh(new_job)

    log_audit(
        user_id=actor.user_id,
        action="duplicate",
        entity_type="job",
        entity_id=new_job.id,
        new_value={
            **serialize_job(new_job),
            "duplicated_from_job_id": source.id,
            "scenarios_copied": len(source_scenarios),
        },
    )
    return new_job


def resolve_binding(session: Session, *, job_id: int, actor: CurrentUser) -> Job:
    """Re-resolve the cached CML ids for a Job using the owning Project's binding.

    Read-side only — does not touch the draft/version flow. Reuses whatever
    cml_project_id the owning Ops Project has cached, so this is a pure
    "look up my job inside my project" refresh.
    """
    job = session.query(Job).filter(Job.id == job_id).first()
    if job is None:
        raise NotFoundError("Job not found")
    _check_product_access(session, product_id=job.product_id, actor=actor)

    owning_project = _owning_project(session, job.product_id)
    # Honor an existing override on the row — re-resolve under whichever CML
    # project the Job is currently bound to (override if set, parent's if not).
    cml_project_id, cml_job_id, cml_binding_error = _resolve_cml_job(
        owning_project,
        (job.cml_project_name or "").strip(),
        (job.cml_job_name or job.control_m_job_name or "").strip(),
    )
    job.cml_project_id = cml_project_id
    job.cml_job_id = cml_job_id
    job.cml_binding_error = cml_binding_error
    job.updated_at = datetime.utcnow()
    session.flush()
    session.refresh(job)
    return job


def delete(session: Session, *, job_id: int, actor: CurrentUser) -> None:
    job = session.query(Job).filter(Job.id == job_id).first()
    if job is None:
        raise NotFoundError("Job not found")
    product = _check_product_access(session, product_id=job.product_id, actor=actor, require_owner=True)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, product)

    old_val = serialize_job(job)
    session.delete(job)
    log_audit(
        user_id=actor.user_id,
        action="delete",
        entity_type="job",
        entity_id=job_id,
        old_value=old_val,
    )


# ── Failure-scenario use cases ─────────────────────────────────────────


def list_scenarios(session: Session, *, job_id: int, actor: CurrentUser) -> List[JobFailureScenario]:
    job = session.query(Job).filter(Job.id == job_id).first()
    if job is None:
        raise NotFoundError("Job not found")
    _check_product_access(session, product_id=job.product_id, actor=actor)
    return (
        session.query(JobFailureScenario)
        .filter(JobFailureScenario.job_id == job_id)
        .order_by(JobFailureScenario.created_at.asc(), JobFailureScenario.id.asc())
        .all()
    )


def create_scenario(
    session: Session,
    *,
    job_id: int,
    body: JobFailureScenarioCreate,
    actor: CurrentUser,
) -> JobFailureScenario:
    _validate_scenario_body(body)
    job = session.query(Job).filter(Job.id == job_id).first()
    if job is None:
        raise NotFoundError("Job not found")
    product = _check_product_access(session, product_id=job.product_id, actor=actor, require_owner=True)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, product)

    scenario = JobFailureScenario(
        job_id=job_id,
        scenario_type=body.scenario_type,
        scenario_name=body.scenario_name,
        condition_description=body.condition_description or "",
        detection_source=body.detection_source or "",
        diagnostic_steps=body.diagnostic_steps,
        action_steps=body.action_steps,
        verification_steps=body.verification_steps,
        escalation_target=body.escalation_target or "",
        fallback_owner_type=body.fallback_owner_type,
        threshold_operator=body.threshold_operator or "",
        threshold_value=body.threshold_value,
        threshold_feature_list=body.threshold_feature_list or [],
        email_template=body.email_template,
        is_not_applicable=bool(body.is_not_applicable),
        not_applicable_signoff_by=actor.user_id if body.is_not_applicable else None,
        not_applicable_signoff_at=datetime.utcnow() if body.is_not_applicable else None,
        is_active=body.is_active,
    )
    session.add(scenario)
    session.flush()
    session.refresh(scenario)
    log_audit(
        user_id=actor.user_id,
        action="create",
        entity_type="job_failure_scenario",
        entity_id=scenario.id,
        new_value=serialize_job_failure_scenario(scenario),
    )
    return scenario


def update_scenario(
    session: Session,
    *,
    scenario_id: int,
    body: JobFailureScenarioUpdate,
    actor: CurrentUser,
) -> JobFailureScenario:
    _validate_scenario_body(body)
    scenario = (
        session.query(JobFailureScenario)
        .filter(JobFailureScenario.id == scenario_id)
        .first()
    )
    if scenario is None:
        raise NotFoundError("Job failure scenario not found")
    job = session.query(Job).filter(Job.id == scenario.job_id).first()
    if job is None:
        raise NotFoundError("Job failure scenario not found")
    product = _check_product_access(session, product_id=job.product_id, actor=actor, require_owner=True)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, product)

    old_val = serialize_job_failure_scenario(scenario)
    for field in (
        "scenario_type",
        "scenario_name",
        "condition_description",
        "detection_source",
        "diagnostic_steps",
        "action_steps",
        "verification_steps",
        "escalation_target",
        "fallback_owner_type",
        "threshold_operator",
        "threshold_value",
        "threshold_feature_list",
        "email_template",
        "is_active",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(scenario, field, value)
    if body.is_not_applicable is not None:
        scenario.is_not_applicable = bool(body.is_not_applicable)
        if scenario.is_not_applicable:
            scenario.not_applicable_signoff_by = actor.user_id
            scenario.not_applicable_signoff_at = datetime.utcnow()
        else:
            scenario.not_applicable_signoff_by = None
            scenario.not_applicable_signoff_at = None
    scenario.updated_at = datetime.utcnow()
    session.flush()
    session.refresh(scenario)
    log_audit(
        user_id=actor.user_id,
        action="update",
        entity_type="job_failure_scenario",
        entity_id=scenario.id,
        old_value=old_val,
        new_value=serialize_job_failure_scenario(scenario),
    )
    return scenario


def delete_scenario(session: Session, *, scenario_id: int, actor: CurrentUser) -> None:
    scenario = (
        session.query(JobFailureScenario)
        .filter(JobFailureScenario.id == scenario_id)
        .first()
    )
    if scenario is None:
        raise NotFoundError("Job failure scenario not found")
    job = session.query(Job).filter(Job.id == scenario.job_id).first()
    if job is None:
        raise NotFoundError("Job failure scenario not found")
    product = _check_product_access(session, product_id=job.product_id, actor=actor, require_owner=True)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, product)

    old_val = serialize_job_failure_scenario(scenario)
    session.delete(scenario)
    log_audit(
        user_id=actor.user_id,
        action="delete",
        entity_type="job_failure_scenario",
        entity_id=scenario_id,
        old_value=old_val,
    )


def get_product_cml_status_pairs(
    session: Session, *, product_id: int
) -> list[tuple[int, str]] | None:
    """Return [(job_id, control_m_job_name), ...] or None if product missing."""
    product = session.query(Product).filter(Product.id == product_id).first()
    if product is None:
        return None
    jobs = session.query(Job).filter(Job.product_id == product_id).all()
    return [(j.id, j.control_m_job_name) for j in jobs if j.control_m_job_name]
