"""Application + recovery-scenario business logic.

Pure functions; sessions and actors injected from the route boundary.
Never calls session.commit() — `get_session` Depends owns the transaction.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser
from api.schema import (
    AppCreate,
    AppDuplicateRequest,
    AppUpdate,
    ApplicationRecoveryScenarioCreate,
    ApplicationRecoveryScenarioUpdate,
)
from core.exceptions import ConflictError, ForbiddenError, NotFoundError, SystemLockedError, ValidationError
from core.models.entities import (
    Application,
    ApplicationRecoveryScenario,
    ApplicationRecoveryScenarioType,
    FallbackOwnerType,
    Product,
)
from core.models.user import UserRole, is_elevated_role
from core.services.audit_service import (
    log_audit,
    serialize_app,
    serialize_application_recovery_scenario,
)
from core.services.cml_binding_resolver import (
    build_control_interface,
    resolve_application_id as _resolve_cml_application_id,
    resolve_project_id as _resolve_cml_project_id,
)
from core.services.project_version_service import can_mutate_project_scope, ensure_project_editable_draft
from core.services.support_group_service import is_project_editor as _actor_is_project_editor, resolve_support_group_snapshot, user_has_project_group_access
from core.models.entities import Project, ProjectMember


# ── Access control ─────────────────────────────────────────────────────


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


_CML_APP_TYPES = ("fastapi", "runtime", "ray", "generic")


def _validate_cml_app_type(value: str | None) -> None:
    if value is not None and value != "" and value not in _CML_APP_TYPES:
        raise ValidationError(
            f"Invalid cml_app_type {value!r}. Must be one of: {_CML_APP_TYPES}"
        )


def _resolve_cml_app(
    owning_project: "Project | None",
    cml_project_name_override: str | None,
    application_name: str | None,
    subdomain: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve a CML application, optionally targeting a CML project other
    than the one the owning Ops Project is bound to.

    A non-empty ``cml_project_name_override`` binds this asset to a different
    CML project than its Ops parent — for the case where, e.g., Name
    Screening's training pipeline lives in CML project ``material-classifier``
    but the serving app lives in ``inventory-scoring-model``. Empty / None means
    inherit from the owning Ops Project's binding (default, back-compat).

    Returns ``(cml_project_id, cml_application_id, error)``. Whether the
    returned id came from the override or the parent's inheritance is encoded
    in the row by the caller via ``app.cml_project_name`` (override stored
    non-empty = override; empty = inheriting).
    """
    override = (cml_project_name_override or "").strip()

    if override:
        # Asset-level override: resolve the override project independently.
        control = build_control_interface()
        pid, perr = _resolve_cml_project_id(control, override)
        if perr:
            return None, None, perr
        if not pid:
            return None, None, f"CML project '{override}' not found"
        if not (application_name or subdomain):
            return pid, None, None
        aid, aerr = _resolve_cml_application_id(
            control, pid, name=application_name, subdomain=subdomain,
        )
        return pid, aid, aerr

    # Inheritance path — use the owning Ops Project's CML binding.
    if owning_project is None or not (owning_project.cml_project_id or "").strip():
        if owning_project is not None and (owning_project.cml_binding_error or "").strip():
            return None, None, f"Project unresolved: {owning_project.cml_binding_error}"
        return None, None, "Owning Ops Project has no CML Project Name set"

    if not (application_name or subdomain):
        return owning_project.cml_project_id, None, None
    control = build_control_interface()
    aid, aerr = _resolve_cml_application_id(
        control, owning_project.cml_project_id,
        name=application_name, subdomain=subdomain,
    )
    return owning_project.cml_project_id, aid, aerr


def _validate_scenario_body(body) -> None:
    if body.scenario_type is not None and body.scenario_type not in ApplicationRecoveryScenarioType.ALL:
        raise ValidationError(
            f"Invalid application scenario_type. Must be one of: {ApplicationRecoveryScenarioType.ALL}"
        )
    if body.fallback_owner_type is not None and body.fallback_owner_type not in FallbackOwnerType.ALL:
        raise ValidationError(
            f"Invalid fallback_owner_type. Must be one of: {FallbackOwnerType.ALL}"
        )


# ── App use cases ──────────────────────────────────────────────────────


def _owning_project(session: Session, product_id: int) -> Project | None:
    product = session.query(Product).filter(Product.id == product_id).first()
    if product is None:
        return None
    return session.query(Project).filter(Project.id == product.project_id).first()


def create(session: Session, *, product_id: int, body: AppCreate, actor: CurrentUser) -> Application:
    _validate_cml_app_type(body.cml_app_type)
    product = _check_product_access(session, product_id=product_id, actor=actor, require_owner=True)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, product)

    cml_application_name = (body.cml_application_name or "").strip()
    cml_subdomain = (body.cml_subdomain or "").strip()
    cml_project_name_override = (body.cml_project_name or "").strip()
    # CML project binding defaults to inheriting from the owning Ops Project.
    # If body.cml_project_name is set, it overrides — letting one Ops Product
    # cover assets that live across multiple CML projects (e.g. training
    # pipeline in one project + serving app in another). The override is stored
    # verbatim in app.cml_project_name; empty means "inherit from parent".
    owning_project = session.query(Project).filter(Project.id == product.project_id).first()
    cml_project_id, cml_application_id, cml_binding_error = _resolve_cml_app(
        owning_project, cml_project_name_override, cml_application_name, cml_subdomain
    )

    # The App UI exposes a single "Serving URL" input. application_url and
    # cml_serving_url legacy columns mirror each other — whichever side the
    # caller filled wins, the other is back-filled. cml_serving_url is what
    # the AppInterface probes against (with /health, /runtime/config, etc.
    # appended based on cml_app_type), so keeping the two in sync makes the
    # single-input UI actually drive monitoring. health_check_url is no
    # longer needed under the v2 contract (probe path is composed) but the
    # column stays for back-compat — if the caller sent it, mirror it too.
    raw_application_url = (body.application_url or "").strip()
    raw_cml_serving_url = (body.cml_serving_url or "").strip()
    raw_health_check_url = (body.health_check_url or "").strip()
    effective_url = raw_cml_serving_url or raw_application_url or raw_health_check_url

    app = Application(
        product_id=product_id,
        application_url=effective_url,
        health_check_url=effective_url,
        # Store the override verbatim (empty = inherit from parent Ops Project).
        cml_project_name=cml_project_name_override,
        cml_application_name=cml_application_name,
        cml_subdomain=cml_subdomain,
        cml_app_type=(body.cml_app_type or "generic"),
        cml_project_id=cml_project_id,
        cml_application_id=cml_application_id,
        cml_binding_error=cml_binding_error,
        cml_serving_url=effective_url,
        description=body.description or "",
        restart_supported=body.restart_supported,
        restart_summary=body.restart_summary or "",
        owner_contact=body.owner_contact or "",
        is_system=0,
    )
    sg_id, sg_name = resolve_support_group_snapshot(session, body.support_group_id, body.support_group)
    app.support_group_id = sg_id
    app.support_group_name_snapshot = sg_name
    app.support_group = sg_name
    session.add(app)
    session.flush()
    session.refresh(app)
    log_audit(
        user_id=actor.user_id,
        action="create",
        entity_type="application",
        entity_id=app.id,
        new_value=serialize_app(app),
    )
    return app


def list_for_product(session: Session, *, product_id: int, actor: CurrentUser) -> List[Application]:
    _check_product_access(session, product_id=product_id, actor=actor)
    return (
        session.query(Application)
        .filter(Application.product_id == product_id)
        .order_by(Application.created_at.desc())
        .all()
    )


def update(session: Session, *, app_id: int, body: AppUpdate, actor: CurrentUser) -> Application:
    _validate_cml_app_type(body.cml_app_type)
    app = session.query(Application).filter(Application.id == app_id).first()
    if app is None:
        raise NotFoundError("Application not found")
    product = _check_product_access(session, product_id=app.product_id, actor=actor, require_owner=True)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, product)

    old_val = serialize_app(app)
    for field in (
        "application_url",
        "health_check_url",
        "description",
        "restart_supported",
        "restart_summary",
        "owner_contact",
        "cml_project_name",
        "cml_application_name",
        "cml_subdomain",
        "cml_app_type",
        "cml_serving_url",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(app, field, value)

    # Mirror the single Serving URL input into all three URL columns. The new
    # UI exposes one input; whichever side the caller filled wins, the
    # others are back-filled. The AppInterface probes cml_serving_url so
    # keeping it in sync is what makes the single-input UI drive monitoring.
    url_touched = (
        body.application_url is not None
        or body.cml_serving_url is not None
        or body.health_check_url is not None
    )
    if url_touched:
        unified_url = (
            (app.cml_serving_url or "").strip()
            or (app.application_url or "").strip()
            or (app.health_check_url or "").strip()
        )
        app.application_url = unified_url
        app.cml_serving_url = unified_url
        app.health_check_url = unified_url

    # Re-resolve cached ids when any binding input changed. ``app.cml_project_name``
    # acts as the override sentinel — empty means inherit from the owning Ops
    # Project, non-empty means this asset targets a different CML project.
    if (
        body.cml_application_name is not None
        or body.cml_subdomain is not None
        or body.cml_project_name is not None
    ):
        owning_project = _owning_project(session, app.product_id)
        cml_project_id, cml_application_id, cml_binding_error = _resolve_cml_app(
            owning_project,
            (app.cml_project_name or "").strip(),
            (app.cml_application_name or "").strip(),
            (app.cml_subdomain or "").strip(),
        )
        app.cml_project_id = cml_project_id
        app.cml_application_id = cml_application_id
        app.cml_binding_error = cml_binding_error
    sg_id, sg_name = resolve_support_group_snapshot(session, body.support_group_id, body.support_group)
    app.support_group_id = sg_id
    app.support_group_name_snapshot = sg_name
    app.support_group = sg_name
    app.updated_at = datetime.utcnow()
    session.flush()
    session.refresh(app)
    log_audit(
        user_id=actor.user_id,
        action="update",
        entity_type="application",
        entity_id=app.id,
        old_value=old_val,
        new_value=serialize_app(app),
    )
    return app


def duplicate(
    session: Session,
    *,
    source_app_id: int,
    body: AppDuplicateRequest,
    actor: CurrentUser,
) -> Application:
    """Duplicate an Application (and selected recovery scenarios) into a target product.

    Cross-project is allowed. CML ids are cleared so the resolver re-runs
    against the new owning project's binding; ``cml_project_name`` (the
    override sentinel) is carried over verbatim. Recovery scenarios have
    their signoff fields cleared since they need fresh approval.
    """
    source = session.query(Application).filter(Application.id == source_app_id).first()
    if source is None:
        raise NotFoundError("Source application not found")
    _check_product_access(session, product_id=source.product_id, actor=actor)

    target_product = _check_product_access(
        session, product_id=body.target_product_id, actor=actor, require_owner=True
    )
    if target_product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, target_product)

    new_name = (body.cml_application_name or "").strip()
    if not new_name:
        raise ValidationError("cml_application_name is required")

    owning_project = _owning_project(session, body.target_product_id)
    cml_project_id, cml_application_id, cml_binding_error = _resolve_cml_app(
        owning_project,
        (source.cml_project_name or "").strip(),
        new_name,
        (source.cml_subdomain or "").strip() or None,
    )

    new_app = Application(
        product_id=body.target_product_id,
        application_url=source.application_url or "",
        health_check_url=source.health_check_url or "",
        cml_project_name=source.cml_project_name or "",
        cml_application_name=new_name,
        cml_subdomain=source.cml_subdomain or "",
        cml_app_type=source.cml_app_type or "generic",
        cml_project_id=cml_project_id,
        cml_application_id=cml_application_id,
        cml_binding_error=cml_binding_error,
        cml_serving_url=source.cml_serving_url or "",
        description=source.description or "",
        restart_supported=bool(source.restart_supported),
        restart_summary=source.restart_summary or "",
        owner_contact=source.owner_contact or "",
        support_group_id=source.support_group_id,
        support_group_name_snapshot=source.support_group_name_snapshot or "",
        support_group=source.support_group or "",
        is_system=0,
    )
    session.add(new_app)
    session.flush()
    session.refresh(new_app)

    source_scenarios = (
        session.query(ApplicationRecoveryScenario)
        .filter(ApplicationRecoveryScenario.application_id == source.id)
        .all()
    )
    if body.recovery_scenario_ids is not None:
        wanted = set(body.recovery_scenario_ids)
        source_scenarios = [s for s in source_scenarios if s.id in wanted]

    for s in source_scenarios:
        cloned = ApplicationRecoveryScenario(
            application_id=new_app.id,
            scenario_type=s.scenario_type,
            scenario_name=s.scenario_name,
            condition_description=s.condition_description or "",
            action_steps=s.action_steps,
            verification_steps=s.verification_steps,
            escalation_target=s.escalation_target or "",
            fallback_owner_type=s.fallback_owner_type,
            email_template=s.email_template,
            is_not_applicable=bool(s.is_not_applicable),
            not_applicable_signoff_by=None,
            not_applicable_signoff_at=None,
            is_active=bool(s.is_active),
        )
        session.add(cloned)
    session.flush()
    session.refresh(new_app)

    log_audit(
        user_id=actor.user_id,
        action="duplicate",
        entity_type="application",
        entity_id=new_app.id,
        new_value={
            **serialize_app(new_app),
            "duplicated_from_application_id": source.id,
            "scenarios_copied": len(source_scenarios),
        },
    )
    return new_app


def resolve_binding(session: Session, *, app_id: int, actor: CurrentUser) -> Application:
    """Re-resolve the cached CML ids for an Application using the owning Project's binding."""
    app = session.query(Application).filter(Application.id == app_id).first()
    if app is None:
        raise NotFoundError("Application not found")
    _check_product_access(session, product_id=app.product_id, actor=actor)

    owning_project = _owning_project(session, app.product_id)
    # Honor an existing override on the row — re-resolve under whichever CML
    # project the asset is currently bound to (override if set, parent's if not).
    cml_project_id, cml_application_id, cml_binding_error = _resolve_cml_app(
        owning_project,
        (app.cml_project_name or "").strip(),
        (app.cml_application_name or "").strip(),
        (app.cml_subdomain or "").strip(),
    )
    app.cml_project_id = cml_project_id
    app.cml_application_id = cml_application_id
    app.cml_binding_error = cml_binding_error
    app.updated_at = datetime.utcnow()
    session.flush()
    session.refresh(app)
    return app


def delete(session: Session, *, app_id: int, actor: CurrentUser) -> None:
    app = session.query(Application).filter(Application.id == app_id).first()
    if app is None:
        raise NotFoundError("Application not found")
    product = _check_product_access(session, product_id=app.product_id, actor=actor, require_owner=True)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, product)

    old_val = serialize_app(app)
    session.delete(app)
    log_audit(
        user_id=actor.user_id,
        action="delete",
        entity_type="application",
        entity_id=app_id,
        old_value=old_val,
    )


# ── Recovery-scenario use cases ────────────────────────────────────────


def list_scenarios(session: Session, *, app_id: int, actor: CurrentUser) -> List[ApplicationRecoveryScenario]:
    app = session.query(Application).filter(Application.id == app_id).first()
    if app is None:
        raise NotFoundError("Application not found")
    _check_product_access(session, product_id=app.product_id, actor=actor)
    return (
        session.query(ApplicationRecoveryScenario)
        .filter(ApplicationRecoveryScenario.application_id == app_id)
        .order_by(ApplicationRecoveryScenario.created_at.asc(), ApplicationRecoveryScenario.id.asc())
        .all()
    )


def create_scenario(
    session: Session,
    *,
    app_id: int,
    body: ApplicationRecoveryScenarioCreate,
    actor: CurrentUser,
) -> ApplicationRecoveryScenario:
    _validate_scenario_body(body)
    app = session.query(Application).filter(Application.id == app_id).first()
    if app is None:
        raise NotFoundError("Application not found")
    product = _check_product_access(session, product_id=app.product_id, actor=actor, require_owner=True)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, product)

    scenario = ApplicationRecoveryScenario(
        application_id=app_id,
        scenario_type=body.scenario_type,
        scenario_name=body.scenario_name,
        condition_description=body.condition_description or "",
        action_steps=body.action_steps,
        verification_steps=body.verification_steps,
        escalation_target=body.escalation_target or "",
        fallback_owner_type=body.fallback_owner_type,
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
        entity_type="application_recovery_scenario",
        entity_id=scenario.id,
        new_value=serialize_application_recovery_scenario(scenario),
    )
    return scenario


def update_scenario(
    session: Session,
    *,
    scenario_id: int,
    body: ApplicationRecoveryScenarioUpdate,
    actor: CurrentUser,
) -> ApplicationRecoveryScenario:
    _validate_scenario_body(body)
    scenario = (
        session.query(ApplicationRecoveryScenario)
        .filter(ApplicationRecoveryScenario.id == scenario_id)
        .first()
    )
    if scenario is None:
        raise NotFoundError("Application recovery scenario not found")
    app = session.query(Application).filter(Application.id == scenario.application_id).first()
    if app is None:
        raise NotFoundError("Application recovery scenario not found")
    product = _check_product_access(session, product_id=app.product_id, actor=actor, require_owner=True)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, product)

    old_val = serialize_application_recovery_scenario(scenario)
    for field in (
        "scenario_type",
        "scenario_name",
        "condition_description",
        "action_steps",
        "verification_steps",
        "escalation_target",
        "fallback_owner_type",
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
        entity_type="application_recovery_scenario",
        entity_id=scenario.id,
        old_value=old_val,
        new_value=serialize_application_recovery_scenario(scenario),
    )
    return scenario


def delete_scenario(session: Session, *, scenario_id: int, actor: CurrentUser) -> None:
    scenario = (
        session.query(ApplicationRecoveryScenario)
        .filter(ApplicationRecoveryScenario.id == scenario_id)
        .first()
    )
    if scenario is None:
        raise NotFoundError("Application recovery scenario not found")
    app = session.query(Application).filter(Application.id == scenario.application_id).first()
    if app is None:
        raise NotFoundError("Application recovery scenario not found")
    product = _check_product_access(session, product_id=app.product_id, actor=actor, require_owner=True)
    if product.is_system == 1:
        raise SystemLockedError("System-managed product is read-only")
    _ensure_mutable(session, product)

    old_val = serialize_application_recovery_scenario(scenario)
    session.delete(scenario)
    log_audit(
        user_id=actor.user_id,
        action="delete",
        entity_type="application_recovery_scenario",
        entity_id=scenario_id,
        old_value=old_val,
    )
