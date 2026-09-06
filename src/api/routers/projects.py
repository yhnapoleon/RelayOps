"""Project CRUD routes: POST/GET/PUT/DELETE /api/projects."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, status

from fastapi import HTTPException

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.deps.rbac import BusinessOwnerOrAdmin
from api.schema import (
    CmlAppOption,
    CmlProjectOption,
    CmlProjectSearchResponse,
    CmlResourceOption,
    HandoverApproveRequest,
    HandoverCompletenessResponse,
    HandoverRejectRequest,
    HandoverRequest,
    IssueResponse,
    MmpModelOption,
    MmpProjectOption,
    MmpUrlResolveResponse,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    ProjectVersionResponse,
    ProjectVersionSubmitRequest,
)
from core.integrations.mmp_interface import MmpApiError, MmpInterface
from core.config import get_config
from core.exceptions import ForbiddenError, NotFoundError, ValidationError
from core.logging import get_logger
from core.models.database import get_db
from core.models.entities import Project, ProjectVersion
from core.models.user import UserRole, is_elevated_role
from core.services import handover_service, project_service as db_project, user_service as db_user
from core.services.audit_service import log_audit, serialize_project

logger = get_logger(__name__)
router = APIRouter(prefix="/api/projects", tags=["projects"])


def _enrich_project(project: Project, owner_cache: dict | None = None) -> ProjectResponse:
    resp = ProjectResponse.model_validate(project)
    if owner_cache is not None and project.owner_id in owner_cache:
        user = owner_cache[project.owner_id]
    else:
        user = db_user.get_user_by_id(get_db(), project.owner_id)
        if owner_cache is not None and user:
            owner_cache[project.owner_id] = user
    if user:
        resp.owner_username = user.username
        resp.owner_display_name = user.display_name
    resp.owner_group_id = project.owner_group_id
    resp.owner_group_name = project.owner_group_name_snapshot or None

    # Derived binding state — drives the UI badge.
    if project.cml_binding_error:
        resp.cml_binding_status = "error"
    elif project.cml_project_name:
        resp.cml_binding_status = "resolved" if project.cml_project_id else "pending"
    else:
        resp.cml_binding_status = "unconfigured"

    # Resolve the pointed-to draft/approved version statuses so the UI can
    # decide button states (open draft vs under review). Best-effort: a
    # missing version simply leaves the status None.
    if project.current_draft_version_id or project.current_approved_version_id:
        session = get_db().get_session()
        try:
            if project.current_draft_version_id:
                draft = session.query(ProjectVersion).filter(
                    ProjectVersion.id == project.current_draft_version_id
                ).first()
                resp.current_draft_version_status = draft.version_status if draft else None
            if project.current_approved_version_id:
                approved = session.query(ProjectVersion).filter(
                    ProjectVersion.id == project.current_approved_version_id
                ).first()
                resp.current_approved_version_status = approved.version_status if approved else None
        finally:
            session.close()
    return resp


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreate,
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    db = get_db()
    project = db_project.create_project(
        db,
        name=body.name,
        description=body.description,
        owner_id=current_user.user_id,
        owner_group_id=body.owner_group_id,
        cml_project_name=body.cml_project_name,
        cml_project_id=body.cml_project_id,
        mmp_project_id=body.mmp_project_id,
        prod_stat_url=body.prod_stat_url,
    )
    log_audit(
        user_id=current_user.user_id,
        action="create",
        entity_type="project",
        entity_id=project.id,
        new_value=serialize_project(project),
    )
    return _enrich_project(project)


@router.get("/cml/search", response_model=CmlProjectSearchResponse)
def search_cml_projects(
    q: str = "",
    current_user: CurrentUser = Depends(get_current_user),
):
    """First-page slice of CML projects visible to the Ops service identity.

    Backs the CML project picker in the Create-Project dialog and the inline
    binding editor. ``q`` is forwarded to CML as a ``search_filter={"name":q}``
    substring match. Returns ``has_more=True`` whenever CML signals
    ``next_page_token`` so the UI can switch to type-to-filter mode rather
    than render a misleadingly-truncated list.

    On CML errors we surface 502 with the underlying message so the UI can
    fall back to manual text entry without silently hiding the failure.
    """
    from core.integrations.control_interface import CmlApiError
    from core.services.cml_binding_resolver import build_control_interface

    control = build_control_interface()
    name_filter = (q or "").strip() or None
    try:
        items, has_more = control.list_projects_page(name_filter=name_filter)
    except CmlApiError as exc:
        logger.warning(
            "CML list_projects_page(q={}) failed for user {}: {}",
            name_filter, current_user.user_id, exc,
        )
        raise HTTPException(
            status_code=502,
            detail=f"CML unreachable: {exc.message}",
        )

    # /api/v2/projectnames returns name-only rows (no id, no owner). We
    # accept rows with just a name and leave id empty — project_service
    # resolves name → id at save time via resolve_project_id. Owner fields stay None.
    options: List[CmlProjectOption] = []
    for it in items:
        pid = str(it.get("id") or "")
        name = str(it.get("name") or "")
        if not name:
            continue
        owner = it.get("owner") or {}
        options.append(
            CmlProjectOption(
                id=pid,
                name=name,
                owner_username=(owner.get("username") or None),
                owner_email=(owner.get("email") or None),
            )
        )
    return CmlProjectSearchResponse(items=options, has_more=has_more)


@router.get("/mmp-projects", response_model=List[MmpProjectOption])
def list_mmp_projects_for_picker(
    current_user: CurrentUser = Depends(get_current_user),
) -> List[MmpProjectOption]:
    """Workspace-wide MMP project picker — feeds the Project create/edit form.

    Declared *before* the ``/{project_id}`` GET handler so FastAPI doesn't
    try to parse the literal ``mmp-projects`` as an int project_id (which
    is what produces a 422 from the parameterized route). The MMP directory
    is workspace-scoped, so no ``project_id`` is needed here.
    """
    return _build_mmp_project_options(_make_mmp_interface())


@router.get("/mmp/resolve-url", response_model=MmpUrlResolveResponse)
def resolve_mmp_url(
    url: str = "",
    current_user: CurrentUser = Depends(get_current_user),
) -> MmpUrlResolveResponse:
    """Resolve a pasted MMP web link into a Ops MMP binding.

    Accepts a URL like ``…/project/160/projectDetails``, pulls the numeric
    project id out of the path, fetches that project from the MMP API, and
    returns ``project_repo_name`` (= ``mmp_project_id``) + the model list so
    the Job/Project form can auto-fill the binding. ``suggested_model_name``
    is set only when the project has exactly one production model.

    Declared before ``/{project_id}`` so ``mmp`` isn't parsed as an id.
    Errors degrade into the ``error`` field (never a 5xx) so the form can show
    a hint and fall back to manual entry — same posture as the pickers.
    """
    import re

    m = re.search(r"/projects?/(\d+)", url or "")
    if not m:
        return MmpUrlResolveResponse(error="No MMP project id found in URL (expected …/project/<id>/…)")
    pid = int(m.group(1))

    iface = _make_mmp_interface()
    if not iface.is_configured():
        return MmpUrlResolveResponse(project_id=pid, error="MMP is not configured on this server")
    try:
        payload = iface.get_project(pid)
    except MmpApiError as exc:
        return MmpUrlResolveResponse(project_id=pid, error=f"MMP {exc.status_code or 'error'}: {exc.message}")
    except Exception as exc:  # noqa: BLE001 — UI fallback path, never raise
        return MmpUrlResolveResponse(project_id=pid, error=f"Unexpected error: {exc}")

    repo_name = payload.get("project_repo_name") or None
    business = payload.get("business_understanding_project_name") or None
    models: List[MmpModelOption] = []
    prod_names: List[str] = []
    for mdl in payload.get("models") or []:
        name = mdl.get("model_name") or mdl.get("name") or ""
        if not name:
            continue
        is_prod = bool(mdl.get("is_production"))
        models.append(
            MmpModelOption(
                model_name=name,
                project_repo_name=repo_name or "",
                business_name=business,
                is_production=is_prod,
            )
        )
        if is_prod:
            prod_names.append(name)
    models.sort(key=lambda o: o.model_name.lower())
    suggested = prod_names[0] if len(prod_names) == 1 else None
    return MmpUrlResolveResponse(
        project_id=pid,
        project_repo_name=repo_name,
        business_name=business,
        models=models,
        suggested_model_name=suggested,
    )


@router.get("", response_model=List[ProjectResponse])
def list_projects(current_user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    is_admin = is_elevated_role(current_user.role)  # admin or relayops_member (see-all + edit)
    projects = db_project.list_projects_for_user(
        db,
        user_id=current_user.user_id,
        is_admin=is_admin,
        ad_groups=current_user.ad_groups,
    )
    owner_cache: dict = {}
    return [_enrich_project(p, owner_cache) for p in projects]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    is_admin = is_elevated_role(current_user.role)  # admin or relayops_member (see-all + edit)
    result = db_project.get_project_for_user(
        db,
        project_id=project_id,
        user_id=current_user.user_id,
        is_admin=is_admin,
        ad_groups=current_user.ad_groups,
    )
    if result.status == "not_found":
        raise NotFoundError("Project not found")
    if result.status == "forbidden":
        raise ForbiddenError("Not authorized to access this project")
    return _enrich_project(result.project)


@router.put("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: int,
    body: ProjectUpdate,
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    db = get_db()
    is_admin = is_elevated_role(current_user.role)  # admin or relayops_member (see-all + edit)
    response = db_project.update_project(
        db,
        project_id=project_id,
        actor_user_id=current_user.user_id,
        is_admin=is_admin,
        name=body.name,
        description=body.description,
        owner_group_id=body.owner_group_id,
        cml_project_name=body.cml_project_name,
        cml_project_id=body.cml_project_id,
        mmp_project_id=body.mmp_project_id,
        prod_stat_url=body.prod_stat_url,
        serializer=serialize_project,
    )
    if response.status == "not_found":
        raise NotFoundError("Project not found")
    if response.status == "system_locked":
        raise ValidationError("System-managed project is read-only")
    if response.status == "forbidden":
        raise ForbiddenError("Not authorized to update this project")
    log_audit(
        user_id=current_user.user_id,
        action="update",
        entity_type="project",
        entity_id=response.project.id,
        old_value=response.old_value,
        new_value=serialize_project(response.project),
    )
    return _enrich_project(response.project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: int,
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    db = get_db()
    is_admin = is_elevated_role(current_user.role)  # admin or relayops_member (see-all + edit)
    response = db_project.delete_project(
        db,
        project_id=project_id,
        actor_user_id=current_user.user_id,
        is_admin=is_admin,
        serializer=serialize_project,
    )
    if response.status == "not_found":
        raise NotFoundError("Project not found")
    if response.status == "system_locked":
        raise ValidationError("System-managed project is read-only")
    if response.status == "forbidden":
        raise ForbiddenError("Not authorized to delete this project")
    log_audit(
        user_id=current_user.user_id,
        action="delete",
        entity_type="project",
        entity_id=project_id,
        old_value=response.old_value,
    )


@router.post("/{project_id}/copy", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def copy_project(
    project_id: int,
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    """Duplicate a project (and every product/job/app/scenario under it).

    The new project's name is the source's name with a "(copy)" suffix —
    bumped to "(copy 2)" / "(copy 3)" on collision. CML bindings and
    asset configuration carry over verbatim per the design intent;
    only the top-level project name changes.
    """
    db = get_db()
    response = db_project.copy_project(
        db,
        source_project_id=project_id,
        actor_user_id=current_user.user_id,
        is_admin=is_elevated_role(current_user.role),
        ad_groups=current_user.ad_groups,
    )
    if response.status == "not_found":
        raise NotFoundError("Project not found")
    if response.status == "forbidden":
        raise ForbiddenError("Not authorized to copy this project")
    log_audit(
        user_id=current_user.user_id,
        action="create",
        entity_type="project",
        entity_id=response.project.id,
        new_value=serialize_project(response.project),
    )
    return _enrich_project(response.project)


@router.post("/{project_id}/resolve-binding", response_model=ProjectResponse)
def resolve_project_binding(
    project_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Re-attempt CML name → id resolution for this Project and persist.

    Read-side only — refreshes the cached CML project_id from the project's
    current cml_project_name. Useful when CML was offline at create time
    and the user wants to retry without editing the project.
    """
    db = get_db()
    is_admin = is_elevated_role(current_user.role)  # admin or relayops_member (see-all + edit)

    # Reuse existing access check before mutating cache.
    fetch = db_project.get_project_for_user(
        db,
        project_id=project_id,
        user_id=current_user.user_id,
        is_admin=is_admin,
        ad_groups=current_user.ad_groups,
    )
    if fetch.status == "not_found":
        raise NotFoundError("Project not found")
    if fetch.status == "forbidden":
        raise ForbiddenError("Not authorized to access this project")

    response = db_project.resolve_project_binding(
        db,
        project_id=project_id,
        actor_user_id=current_user.user_id,
        is_admin=is_admin,
    )
    if response.status == "not_found":
        raise NotFoundError("Project not found")
    return _enrich_project(response.project)


def _fetch_cml_items(
    project_id: int,
    current_user: CurrentUser,
    kind: str,
    cml_project_name_override: str | None = None,
) -> list[dict]:
    """Shared backbone for the CML-jobs / CML-apps combobox feeds.

    Returns the raw CML payload rows (or an empty list when the project has
    no resolved ``cml_project_id``) so each caller can pick the fields it
    needs. Returns 502 only when CML was reachable but failed for this
    lookup — UI shows a "couldn't reach CML" hint and still allows manual
    typing.

    When ``cml_project_name_override`` is provided (non-empty), the lookup
    targets that CML project instead of the owning Ops project's binding —
    mirrors the per-asset override added in app_service / job_service so
    the Job / Application form's pickers stay in sync with the override.
    """
    db = get_db()
    is_admin = is_elevated_role(current_user.role)  # admin or relayops_member (see-all + edit)
    fetch = db_project.get_project_for_user(
        db,
        project_id=project_id,
        user_id=current_user.user_id,
        is_admin=is_admin,
        ad_groups=current_user.ad_groups,
    )
    if fetch.status == "not_found":
        raise NotFoundError("Project not found")
    if fetch.status == "forbidden":
        raise ForbiddenError("Not authorized to access this project")

    from core.integrations.control_interface import CmlApiError
    from core.services.cml_binding_resolver import (
        build_control_interface,
        resolve_project_id as _resolve_cml_project_id,
    )

    control = build_control_interface()

    override = (cml_project_name_override or "").strip()
    if override:
        # Resolve the override name on the fly. We don't persist anything —
        # this is just the combobox feed for the form.
        cml_project_id, perr = _resolve_cml_project_id(control, override)
        if perr or not cml_project_id:
            logger.info(
                "CML override project '{}' did not resolve for Ops project {}: {}",
                override, project_id, perr or "no id returned",
            )
            return []
    else:
        cml_project_id = (fetch.project.cml_project_id or "").strip()
        if not cml_project_id:
            return []

    try:
        if kind == "jobs":
            items = control.list_jobs(cml_project_id)
        else:
            items = control.list_applications(cml_project_id)
    except CmlApiError as exc:
        logger.warning(
            "CML list_{}({}) failed for Ops project {} (cml_project_id={}): {}",
            kind, project_id, cml_project_id, exc,
        )
        raise HTTPException(
            status_code=502,
            detail=f"CML unreachable: {exc.message}",
        )
    return items


def _workspace_host_for_serving_url() -> str:
    """Host part of the CML base URL — used to compose application serving
    URLs of the form ``https://<subdomain>.<workspace-host>/``. Returns "" when the base URL is unset or unparseable so
    the caller can skip serving_url composition without raising."""
    from urllib.parse import urlparse
    from core.config import get_config

    try:
        base = get_config().cml_platform_base_url or ""
    except Exception:  # pragma: no cover — config layer should never raise here
        return ""
    parsed = urlparse(base)
    return parsed.netloc or ""


# ── Version control & handover (project-level) ────────────────────────


def _require_project_view(project_id: int, current_user: CurrentUser) -> Project:
    """Resolve a project for read access or raise the matching domain error."""
    db = get_db()
    fetch = db_project.get_project_for_user(
        db,
        project_id=project_id,
        user_id=current_user.user_id,
        is_admin=is_elevated_role(current_user.role),
        ad_groups=current_user.ad_groups,
    )
    if fetch.status == "not_found":
        raise NotFoundError("Project not found")
    if fetch.status == "forbidden":
        raise ForbiddenError("Not authorized to access this project")
    return fetch.project


@router.get("/{project_id}/handover-completeness", response_model=HandoverCompletenessResponse)
def get_project_handover_completeness(
    project_id: int,
    session=Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_project_view(project_id, current_user)
    from core.services.handover_service import evaluate_project_handover_completeness
    result = evaluate_project_handover_completeness(session, project_id)
    return HandoverCompletenessResponse.model_validate(result)


@router.post("/{project_id}/drafts", response_model=ProjectVersionResponse)
def open_project_draft(
    project_id: int,
    session=Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    from core.exceptions import ConflictError, SystemLockedError
    from core.services.project_version_service import ensure_project_editable_draft
    project = _require_project_view(project_id, current_user)
    if project.is_system == 1:
        raise SystemLockedError("System-managed project is read-only")
    # Re-load inside the request session so mutations persist via get_session.
    project = session.query(Project).filter(Project.id == project_id).first()
    try:
        draft = ensure_project_editable_draft(session, project)
    except ValueError as exc:
        if str(exc) == "draft_under_review":
            raise ConflictError("Current draft is already under review")
        raise ValidationError(str(exc))
    return ProjectVersionResponse.model_validate(draft)


@router.post("/{project_id}/versions/submit", response_model=ProjectVersionResponse)
def submit_project_version_endpoint(
    project_id: int,
    body: ProjectVersionSubmitRequest,
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    version = handover_service.submit_version(
        project_id=project_id,
        actor_user_id=current_user.user_id,
        actor_username=current_user.username,
        actor_role=current_user.role,
        actor_ad_groups=current_user.ad_groups,
        change_summary=body.change_summary,
        requested_version_number=body.requested_version_number,
    )
    return ProjectVersionResponse.model_validate(version)


@router.post("/{project_id}/handover", response_model=IssueResponse, status_code=status.HTTP_201_CREATED)
def initiate_project_handover(
    project_id: int,
    body: HandoverRequest = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    issue = handover_service.initiate_handover(
        project_id=project_id,
        actor_user_id=current_user.user_id,
        actor_username=current_user.username,
        actor_role=current_user.role,
        actor_ad_groups=current_user.ad_groups,
    )
    return IssueResponse.model_validate(issue)


@router.get("/{project_id}/versions", response_model=List[ProjectVersionResponse])
def list_project_versions(
    project_id: int,
    session=Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_project_view(project_id, current_user)
    from core.services.project_version_service import (
        _sorted_project_versions,
        ensure_project_baseline_version,
        refresh_project_version_artifacts,
    )
    project = session.query(Project).filter(Project.id == project_id).first()
    ensure_project_baseline_version(session, project)
    session.flush()
    versions = _sorted_project_versions(session, project_id)
    for version in versions:
        if version.version_status == "draft":
            refresh_project_version_artifacts(session, version)
    return [ProjectVersionResponse.model_validate(v) for v in versions]


@router.get("/versions/{version_id}", response_model=ProjectVersionResponse)
def get_project_version(
    version_id: int,
    session=Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    version = session.query(ProjectVersion).filter(ProjectVersion.id == version_id).first()
    if version is None:
        raise NotFoundError("Project version not found")
    _require_project_view(version.project_id, current_user)
    if version.version_status == "draft":
        from core.services.project_version_service import refresh_project_version_artifacts
        refresh_project_version_artifacts(session, version)
        session.flush()
        session.refresh(version)
    return ProjectVersionResponse.model_validate(version)


@router.post("/versions/{version_id}/rollback", response_model=ProjectVersionResponse)
def rollback_project_version_endpoint(
    version_id: int,
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    draft = handover_service.rollback_version(
        version_id=version_id,
        actor_user_id=current_user.user_id,
        actor_username=current_user.username,
        actor_role=current_user.role,
        actor_ad_groups=current_user.ad_groups,
    )
    return ProjectVersionResponse.model_validate(draft)


@router.post("/versions/{version_id}/approve", response_model=ProjectVersionResponse)
def approve_project_version_endpoint(
    version_id: int,
    body: HandoverApproveRequest = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    if current_user.username not in get_config().platform_owners:
        raise ForbiddenError("Only Platform Admins can approve handovers")
    version = handover_service.approve_version(
        version_id=version_id,
        actor_user_id=current_user.user_id,
        actor_username=current_user.username,
    )
    return ProjectVersionResponse.model_validate(version)


@router.post("/versions/{version_id}/reject", response_model=ProjectVersionResponse)
def reject_project_version_endpoint(
    version_id: int,
    body: HandoverRejectRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    if current_user.username not in get_config().platform_owners:
        raise ForbiddenError("Only Platform Admins can reject handovers")
    version = handover_service.reject_version(
        version_id=version_id,
        rejection_reason=body.rejection_reason,
        actor_user_id=current_user.user_id,
        actor_username=current_user.username,
    )
    return ProjectVersionResponse.model_validate(version)


@router.get("/{project_id}/cml-jobs", response_model=List[CmlResourceOption])
def list_cml_jobs_for_project(
    project_id: int,
    cml_project_name: str | None = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    """List CML jobs under the project's bound CML project — feeds the
    Job-binding combobox so users pick instead of type.

    Pass ``cml_project_name`` to target a different CML project than the
    owning Ops project's binding (per-asset override).
    """
    items = _fetch_cml_items(project_id, current_user, "jobs", cml_project_name)
    return [
        CmlResourceOption(id=str(it.get("id") or ""), name=str(it.get("name") or ""))
        for it in items
        if it.get("id") and it.get("name")
    ]


@router.get("/{project_id}/cml-apps", response_model=List[CmlAppOption])
def list_cml_apps_for_project(
    project_id: int,
    cml_project_name: str | None = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    """List CML applications under the project's bound CML project.

    Returns ``subdomain`` and a composed ``serving_url`` so the Application
    form can auto-fill those fields once the user picks a row — no manual
    typing of subdomain or URL required.

    Pass ``cml_project_name`` to target a different CML project than the
    owning Ops project's binding (per-asset override).
    """
    items = _fetch_cml_items(project_id, current_user, "apps", cml_project_name)
    host = _workspace_host_for_serving_url()
    options: List[CmlAppOption] = []
    for it in items:
        if not (it.get("id") and it.get("name")):
            continue
        subdomain = str(it.get("subdomain") or "").strip()
        serving_url = f"https://{subdomain}.{host}/" if subdomain and host else None
        options.append(
            CmlAppOption(
                id=str(it.get("id")),
                name=str(it.get("name")),
                subdomain=subdomain or None,
                status=str(it.get("status") or "") or None,
                serving_url=serving_url,
            )
        )
    return options


def _build_mmp_model_options(
    iface: MmpInterface,
    *,
    repo_name_filter: Optional[str] = None,
) -> List[MmpModelOption]:
    """Pure helper that turns MmpInterface's shallow directory into a flat
    sorted list of MmpModelOption rows. Returns [] on any failure so the
    Job form degrades to plain text input rather than blocking on errors.

    When ``repo_name_filter`` is set, only models from that MMP project are
    returned — used by the Job picker to scope to the parent Ops Project's
    MMP binding by default. None / empty returns the full workspace list.
    """
    if not iface.is_configured():
        logger.warning(
            "MMP model picker: returning [] because MmpInterface is not configured "
            "(mmp.base_url and mmp.bearer_token must both be set; check env vars "
            "RELAYOPS_MMP_BASE_URL / RELAYOPS_MMP_BEARER_TOKEN or config.yaml mmp.* block)"
        )
        return []
    try:
        directory = iface.list_projects_shallow()
    except MmpApiError as exc:
        logger.warning("MMP picker: failed to load directory: {}", exc.message)
        return []
    except Exception as exc:  # noqa: BLE001 — UI fallback path, do not raise
        logger.warning("MMP picker: unexpected error loading directory: {}", exc)
        return []

    options: List[MmpModelOption] = []
    target = (repo_name_filter or "").strip()
    for repo_name, info in directory.items():
        if target and repo_name != target:
            continue
        business_name = (info.get("business_name") or "") or None
        for model in info.get("models") or []:
            name = model.get("name") or ""
            if not name:
                continue
            options.append(
                MmpModelOption(
                    model_name=name,
                    project_repo_name=repo_name,
                    business_name=business_name,
                    is_production=bool(model.get("is_production")),
                )
            )
    # Sort by model_name primary (the picker's typing key), repo secondary
    # to keep duplicate-named models stable across calls.
    options.sort(key=lambda o: (o.model_name.lower(), o.project_repo_name.lower()))
    return options


def _build_mmp_project_options(iface: MmpInterface) -> List[MmpProjectOption]:
    """Pure helper that turns MmpInterface's shallow directory into a list
    of MmpProjectOption rows — one per MMP project (not per model).

    Used by the Ops Project create/edit form to bind a Project to an MMP
    project. Returns [] on any failure so the form degrades gracefully.
    """
    if not iface.is_configured():
        logger.warning(
            "MMP project picker: returning [] because MmpInterface is not configured "
            "(mmp.base_url and mmp.bearer_token must both be set; check env vars "
            "RELAYOPS_MMP_BASE_URL / RELAYOPS_MMP_BEARER_TOKEN or config.yaml mmp.* block)"
        )
        return []
    try:
        directory = iface.list_projects_shallow()
    except MmpApiError as exc:
        logger.warning("MMP project picker: failed to load directory: {}", exc.message)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("MMP project picker: unexpected error: {}", exc)
        return []

    options: List[MmpProjectOption] = []
    for repo_name, info in directory.items():
        options.append(
            MmpProjectOption(
                project_repo_name=repo_name,
                business_name=(info.get("business_name") or "") or None,
                model_count=len(info.get("models") or []),
            )
        )
    # Sort by business_name (more readable to users), fall back to repo_name.
    options.sort(
        key=lambda o: (
            (o.business_name or o.project_repo_name).lower(),
            o.project_repo_name.lower(),
        )
    )
    return options


def _make_mmp_interface() -> MmpInterface:
    cfg = get_config()
    return MmpInterface(
        base_url=cfg.mmp_base_url,
        bearer_token=cfg.mmp_bearer_token,
        refresh_token=cfg.mmp_refresh_token,
        verify_ssl=cfg.mmp_verify_ssl,
        ca_bundle=cfg.mmp_ca_bundle_path or None,
        timeout=float(cfg.mmp_timeout_seconds),
    )


@router.get("/{project_id}/mmp-models", response_model=List[MmpModelOption])
def list_mmp_models_for_picker(
    project_id: int,
    include_all: bool = False,
    current_user: CurrentUser = Depends(get_current_user),
) -> List[MmpModelOption]:
    """Return a flat list of (model_name, project_repo_name) pairs for the
    Job form's MMP picker.

    Default behaviour: when the parent Ops Project has ``mmp_project_id``
    set, the list is filtered to just models under that MMP project (less
    noise — usually 1-5 rows). Pass ``include_all=true`` to bypass the
    filter and return the full workspace directory (used when the Job
    monitors a model that isn't under the parent Project's MMP binding).

    The frontend renders a single combobox with model_name as the primary
    typing key and business_name / project_repo_name as visual secondaries.
    Picking a row writes both Job.mmp_model_id (model_name) and
    Job.mmp_project_id (project_repo_name) in one shot.
    """
    repo_filter: Optional[str] = None
    if not include_all:
        db = get_db()
        session = db.get_session()
        try:
            project = session.query(Project).filter(Project.id == project_id).first()
            if project is not None and (project.mmp_project_id or "").strip():
                repo_filter = project.mmp_project_id.strip()
        finally:
            session.close()

    return _build_mmp_model_options(_make_mmp_interface(), repo_name_filter=repo_filter)
