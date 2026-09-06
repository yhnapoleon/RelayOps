"""Issue routes — thin HTTP layer.

Most behavior is delegated to core/services/issue_service.py.
Helpers in this module are response-shaping (enrichment, workspace
payload) and access checks. Sessions are injected via Depends.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.deps.rbac import AdminOnly
from api.schema import (
    ClaimIssuesRequest,
    ClaimIssuesResponse,
    ExportPayloadResponse,
    HandoverApproveRequest,
    HandoverRejectRequest,
    IssueActionRequest,
    IssueActionWorkspaceResponse,
    IssueAssigneeCandidateResponse,
    IssueCreateRequest,
    IssueResponse,
    IssueRunbookScenarioResponse,
    IssueUpdateRequest,
)
from core.config import get_config
from core.services import issue_service
from core.exceptions import ForbiddenError, NotFoundError, ValidationError
from core.logging import get_logger
from core.models.database import Database, get_db
from core.models.entities import (
    Application,
    ApplicationRecoveryScenario,
    AuditLog,
    Issue,
    IssueActionType,
    IssueStatus,
    IssueType,
    Job,
    JobFailureScenario,
    Notification,
    Product,
    ProductVersion,
    Project,
    ProjectMember,
    ProjectVersion,
)
from core.models.user import User, UserRole, is_elevated_role
from core.services.support_group_service import resolve_support_group_snapshot, user_has_project_group_access
from core.services.user_service import get_user_by_id

logger = get_logger(__name__)
router = APIRouter(tags=["issues"])


# ── Access helpers ────────────────────────────────────────────────────


def _is_platform_admin(username: str) -> bool:
    return username in get_config().platform_owners


def _is_elevated(current_user: CurrentUser) -> bool:
    """See-all + act-as-owner tier: platform admin OR elevated global role
    (admin / relayops_member). Distinct from ``_is_platform_admin`` — the latter
    still solely gates issue reassignment and handover-review visibility."""
    return _is_platform_admin(current_user.username) or is_elevated_role(current_user.role)


def _get_owned_and_member_project_ids(
    session: Session, user_id: int, groups: list[str] | None = None
) -> list[int]:
    owned = [row.id for row in session.query(Project.id).filter(Project.owner_id == user_id).all()]
    member = [
        row.project_id
        for row in session.query(ProjectMember.project_id).filter(ProjectMember.user_id == user_id).all()
    ]
    grouped = [
        project.id
        for project in session.query(Project).all()
        if user_has_project_group_access(session, project, groups)
    ]
    return list(set(owned + member + grouped))


def _get_accessible_product_ids(
    session: Session, user_id: int, groups: list[str] | None = None
) -> list[int]:
    project_ids = _get_owned_and_member_project_ids(session, user_id, groups)
    if not project_ids:
        return []
    return [
        row.id for row in session.query(Product.id).filter(Product.project_id.in_(project_ids)).all()
    ]


def _user_can_access_issue(
    session: Session, issue: Issue, current_user: CurrentUser, is_admin: bool
) -> bool:
    if is_admin or _is_elevated(current_user):  # platform admin OR admin/relayops_member see-all
        return True
    if issue.created_by == current_user.user_id or issue.assignee_id == current_user.user_id:
        return True
    # Accept legacy 'business_owner' alongside the renamed 'regular_user'
    # so pre-rename JWTs / DB rows still pass the access check.
    if current_user.role in ("regular_user", "business_owner", "relayops_member"):
        return issue.product_id in _get_accessible_product_ids(
            session, current_user.user_id, current_user.groups
        )
    return False


# ── Response enrichment ───────────────────────────────────────────────


def _coerce_string_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _scenario_to_response(entity_type: str, scenario) -> IssueRunbookScenarioResponse:
    return IssueRunbookScenarioResponse(
        scenario_id=scenario.id,
        entity_type=entity_type,
        scenario_type=scenario.scenario_type,
        scenario_name=scenario.scenario_name,
        condition_description=getattr(scenario, "condition_description", "") or "",
        detection_source=getattr(scenario, "detection_source", "") or "",
        diagnostic_steps=_coerce_string_list(getattr(scenario, "diagnostic_steps", None)),
        action_steps=_coerce_string_list(getattr(scenario, "action_steps", None)),
        verification_steps=_coerce_string_list(getattr(scenario, "verification_steps", None)),
        escalation_target=getattr(scenario, "escalation_target", "") or "",
        fallback_owner_type=getattr(scenario, "fallback_owner_type", None),
        threshold_operator=getattr(scenario, "threshold_operator", "") or "",
        threshold_value=getattr(scenario, "threshold_value", None),
        threshold_feature_list=_coerce_string_list(getattr(scenario, "threshold_feature_list", None)),
        email_template=getattr(scenario, "email_template", None),
        is_active=bool(getattr(scenario, "is_active", True)),
        is_not_applicable=bool(getattr(scenario, "is_not_applicable", False)),
    )


def _issue_type_to_scenario_types(issue_type: str) -> list[str]:
    # MMP_DRIFT prefers mmp_drift_detected; the perf/feature/data-quality
    # sub-drift scenarios are listed after so an owner who has only
    # configured the sub-drift runbook (because their model's drift
    # signal is always one specific sub-type) still gets a recommendation
    # instead of falling all the way through to scenarios[0]. The legacy
    # pass/fail_threshold types stay at the tail so old runbooks keep
    # working but are never preferred over the new ones.
    return {
        IssueType.JOB_NOT_TRIGGERED: ["not_triggered"],
        IssueType.JOB_FAILED: ["triggered_but_failed", "dependency_failed", "logic_issue", "external_system_issue", "other"],
        # A stale/miss (success-but-not-refreshed, stopped, or timed-out) almost
        # always means the job didn't actually run as scheduled — prefer the
        # not_triggered / dependency / external runbooks (mirrors the canonical
        # knowledge.ISSUE_SCENARIO_HINTS[JOB_STALE]); the failed-run types stay
        # at the tail as a fallback so a job that only configured those still
        # gets a recommendation instead of dropping to scenarios[0].
        IssueType.JOB_STALE: ["not_triggered", "dependency_failed", "external_system_issue", "triggered_but_failed", "logic_issue", "other"],
        IssueType.APP_OFFLINE: ["offline", "healthcheck_failed", "restart_required", "deployment_issue", "other"],
        # ``mmp_no_significant_drift`` is a benign *outcome* runbook the
        # operator picks after inspecting the metrics — never the system's
        # recommendation for a freshly-raised drift Issue, so it sits at the
        # tail (reachable as a fallback, never preferred).
        IssueType.MMP_DRIFT: [
            "mmp_drift_detected",
            "mmp_perf_drift",
            "mmp_feature_drift",
            "mmp_data_quality_drift",
            "fail_threshold",
            "pass_threshold",
            "mmp_no_significant_drift",
        ],
        IssueType.MMP_FAIRNESS_RISK: ["mmp_fairness_risk"],
        IssueType.MMP_RUN_PENDING_APPROVAL: ["mmp_run_pending_approval"],
        IssueType.MMP_UNAPPROVED_EXP_RUN: ["mmp_unapproved_exp_run"],
    }.get(issue_type, [])


def _select_recommended_scenario(
    issue_type: str, scenarios: list[IssueRunbookScenarioResponse]
) -> Optional[IssueRunbookScenarioResponse]:
    # The picker now lists inactive / NA scenarios too, but the system's
    # "matched" recommendation should never default to a disabled runbook —
    # restrict the match (and the scenarios[0] fallback) to active scenarios
    # when any exist.
    pool = [s for s in scenarios if s.is_active] or scenarios
    for preferred_type in _issue_type_to_scenario_types(issue_type):
        for scenario in pool:
            if scenario.scenario_type == preferred_type:
                return scenario
    return pool[0] if pool else None


def _enrich_issue(session: Session, issue: Issue) -> dict:
    """Attach assignee/creator and project/product context to an issue dict."""
    db = get_db()
    result = {
        "id": issue.id,
        "type": issue.type,
        "status": issue.status,
        "title": issue.title,
        "description": issue.description or "",
        "project_id": None,
        "project_name": None,
        "project_is_system": False,
        "product_id": issue.product_id,
        "product_version_id": issue.product_version_id,
        "product_version_number": None,
        "product_version_status": None,
        "project_version_id": issue.project_version_id,
        "project_version_number": None,
        "project_version_status": None,
        "product_name": None,
        "product_is_system": False,
        "job_id": issue.job_id,
        "job_name": None,
        "app_id": issue.app_id,
        "app_name": None,
        # Owner email of the underlying asset (Job/App), surfaced so the
        # actions list can offer a one-click "Contact Owner" mailto.
        "owner_contact": "",
        "created_by": issue.created_by,
        "assignee_id": issue.assignee_id,
        "support_group_id": issue.support_group_id,
        "support_group_name": issue.support_group_name or "",
        "owner_group_id": issue.owner_group_id,
        "owner_group_name": issue.owner_group_name or "",
        "assigned_via": issue.assigned_via or "",
        "resolution_description": issue.resolution_description,
        "rejection_reason": issue.rejection_reason,
        "selected_scenario_type": issue.selected_scenario_type,
        "selected_scenario_name": issue.selected_scenario_name,
        "external_url": getattr(issue, "external_url", None),
        "action_summary_json": issue.action_summary_json,
        "resolution_summary_json": issue.resolution_summary_json,
        "sla_deadline": issue.sla_deadline,
        "resolved_at": issue.resolved_at,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "assignee_username": None,
        "assignee_display_name": None,
        "created_by_username": None,
        "created_by_display_name": None,
        "project_owner_id": None,
        "project_owner_username": None,
        "project_owner_display_name": None,
        "project_prod_stat_url": None,
    }

    if issue.assignee_id:
        assignee = get_user_by_id(db, issue.assignee_id)
        if assignee:
            result["assignee_username"] = assignee.username
            result["assignee_display_name"] = assignee.display_name
    if issue.created_by:
        creator = get_user_by_id(db, issue.created_by)
        if creator:
            result["created_by_username"] = creator.username
            result["created_by_display_name"] = creator.display_name

    if issue.product_id:
        if issue.job_id:
            job = session.query(Job).filter(Job.id == issue.job_id).first()
            if job:
                result["job_name"] = job.control_m_job_name or f"Job #{job.id}"
                result["owner_contact"] = job.owner_contact or ""
        if issue.app_id:
            app = session.query(Application).filter(Application.id == issue.app_id).first()
            if app:
                result["app_name"] = app.application_url or f"Application #{app.id}"
                result["owner_contact"] = app.owner_contact or ""
        product = session.query(Product).filter(Product.id == issue.product_id).first()
        if product:
            result["product_name"] = product.name
            result["product_is_system"] = bool(product.is_system)
            result["project_id"] = product.project_id
            if issue.product_version_id:
                version = session.query(ProductVersion).filter(ProductVersion.id == issue.product_version_id).first()
                if version:
                    result["product_version_number"] = version.version_number
                    result["product_version_status"] = version.version_status
            project = session.query(Project).filter(Project.id == product.project_id).first()
            if project:
                result["project_name"] = project.name
                result["project_is_system"] = bool(project.is_system)
                result["project_owner_id"] = project.owner_id
                result["project_prod_stat_url"] = project.prod_stat_url or None
                owner = get_user_by_id(db, project.owner_id)
                if owner:
                    result["project_owner_username"] = owner.username
                    result["project_owner_display_name"] = owner.display_name

    # Project-scoped issues (handover review) carry project_id directly and
    # have no owning product. Resolve project + project-version context here.
    if issue.project_id:
        project = session.query(Project).filter(Project.id == issue.project_id).first()
        if project:
            result["project_id"] = project.id
            result["project_name"] = project.name
            result["project_is_system"] = bool(project.is_system)
            result["project_owner_id"] = project.owner_id
            result["project_prod_stat_url"] = project.prod_stat_url or None
            owner = get_user_by_id(db, project.owner_id)
            if owner:
                result["project_owner_username"] = owner.username
                result["project_owner_display_name"] = owner.display_name
    if issue.project_version_id:
        pversion = (
            session.query(ProjectVersion)
            .filter(ProjectVersion.id == issue.project_version_id)
            .first()
        )
        if pversion:
            result["project_version_number"] = pversion.version_number
            result["project_version_status"] = pversion.version_status
    return result


def _build_issue_workspace_payload(session: Session, issue: Issue) -> dict:
    fresh_issue = session.query(Issue).filter(Issue.id == issue.id).first() or issue
    enriched = _enrich_issue(session, fresh_issue)
    scenarios: list[IssueRunbookScenarioResponse] = []
    entity_type = entity_label = None
    support_group = ""
    owner_contact = ""
    cml_app_type = None
    restart_supported = None
    restart_summary = None

    if fresh_issue.job_id is not None:
        entity_type = "job"
        job = session.query(Job).filter(Job.id == fresh_issue.job_id).first()
        if job:
            entity_label = job.control_m_job_name or f"Job #{job.id}"
            support_group = job.support_group or ""
            owner_contact = job.owner_contact or ""
            # Return every scenario (active, inactive, NA) so a wrongly-placed
            # runbook is still selectable in the workbench picker — the UI marks
            # state with badges rather than hiding rows.
            raw_scenarios = (
                session.query(JobFailureScenario)
                .filter(JobFailureScenario.job_id == job.id)
                .order_by(JobFailureScenario.created_at.asc(), JobFailureScenario.id.asc())
                .all()
            )
            scenarios = [_scenario_to_response("job", s) for s in raw_scenarios]
    elif fresh_issue.app_id is not None:
        entity_type = "application"
        app = session.query(Application).filter(Application.id == fresh_issue.app_id).first()
        if app:
            entity_label = app.application_url or f"Application #{app.id}"
            support_group = app.support_group or ""
            owner_contact = app.owner_contact or ""
            cml_app_type = app.cml_app_type
            restart_supported = bool(app.restart_supported)
            restart_summary = app.restart_summary or ""
            raw_scenarios = (
                session.query(ApplicationRecoveryScenario)
                .filter(ApplicationRecoveryScenario.application_id == app.id)
                .order_by(ApplicationRecoveryScenario.created_at.asc(), ApplicationRecoveryScenario.id.asc())
                .all()
            )
            scenarios = [_scenario_to_response("application", s) for s in raw_scenarios]
    elif fresh_issue.product_id is not None:
        entity_type = "product"
        product = session.query(Product).filter(Product.id == fresh_issue.product_id).first()
        if product:
            entity_label = product.name

    if not support_group:
        support_group = enriched.get("support_group_name") or ""

    recommended = _select_recommended_scenario(fresh_issue.type, scenarios)
    if fresh_issue.selected_scenario_name:
        for s in scenarios:
            if s.scenario_name == fresh_issue.selected_scenario_name:
                recommended = s
                break

    return IssueActionWorkspaceResponse(
        issue=IssueResponse.model_validate(enriched),
        entity_type=entity_type,
        entity_label=entity_label,
        support_group_id=enriched.get("support_group_id"),
        support_group_name=enriched.get("support_group_name") or support_group,
        support_group=support_group,
        owner_group_id=enriched.get("owner_group_id"),
        owner_group_name=enriched.get("owner_group_name"),
        owner_contact=owner_contact,
        cml_app_type=cml_app_type,
        restart_supported=restart_supported,
        restart_summary=restart_summary,
        recommended_scenario_id=recommended.scenario_id if recommended else None,
        recommended_scenario_name=recommended.scenario_name if recommended else None,
        recommended_scenario_type=recommended.scenario_type if recommended else None,
        scenarios=scenarios,
    ).model_dump()


def _build_workspace_payload_for_service(issue: Issue, db: Database) -> dict:
    """Adapter for issue_service.run_action which expects (issue, db) signature."""
    session = db.get_session()
    try:
        return _build_issue_workspace_payload(session, issue)
    finally:
        session.close()


# ── Routes ─────────────────────────────────────────────────────────────


@router.post("/api/issues", response_model=IssueResponse, status_code=status.HTTP_201_CREATED)
def create_issue_endpoint(
    body: IssueCreateRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if body.type not in IssueType.ALL:
        raise ValidationError(f"Invalid issue type. Must be one of: {IssueType.ALL}")

    issue = issue_service.create_issue_with_dispatch(
        issue_type=body.type,
        title=body.title,
        description=body.description or "",
        product_id=body.product_id,
        job_id=body.job_id,
        app_id=body.app_id,
        assignee_id=body.assignee_id,
        support_group_id=body.support_group_id,
        actor_user_id=current_user.user_id,
        actor_username=current_user.username,
    )
    return _enrich_issue(session, issue)


@router.get("/api/issues", response_model=List[IssueResponse])
def list_issues(
    type: Optional[str] = Query(None, description="Filter by issue type"),
    issue_status: Optional[str] = Query(None, alias="status", description="Filter by issue status"),
    assignee_id: Optional[int] = Query(None, description="Filter by assignee"),
    product_id: Optional[int] = Query(None, description="Filter by product"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    # relayops_member now sees all issues (platform elevation), same as admin.
    see_all = _is_elevated(current_user)
    query = session.query(Issue)
    if type:
        query = query.filter(Issue.type == type)
    if issue_status:
        query = query.filter(Issue.status == issue_status)
    if assignee_id:
        query = query.filter(Issue.assignee_id == assignee_id)
    if product_id:
        query = query.filter(Issue.product_id == product_id)

    if see_all:
        pass
    elif current_user.role in ("regular_user", "business_owner"):
        my_product_ids = _get_accessible_product_ids(session, current_user.user_id)
        if my_product_ids:
            query = query.filter(Issue.product_id.in_(my_product_ids))
        else:
            query = query.filter(Issue.id == -1)
    else:
        query = query.filter(
            (Issue.created_by == current_user.user_id)
            | (Issue.assignee_id == current_user.user_id)
        )

    issues = query.order_by(Issue.created_at.desc()).all()
    return [_enrich_issue(session, i) for i in issues]


@router.get("/api/issues/my", response_model=List[IssueResponse])
def list_my_issues(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    issues = (
        session.query(Issue)
        .filter(Issue.assignee_id == current_user.user_id)
        .filter(Issue.type != IssueType.HANDOVER_REVIEW)
        .order_by(Issue.status.desc(), Issue.created_at.desc())
        .all()
    )
    return [_enrich_issue(session, i) for i in issues]


@router.get("/api/issues/pending-handovers", response_model=List[IssueResponse])
def list_pending_handovers(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not _is_platform_admin(current_user.username):
        raise ForbiddenError("Only Platform Admins can view pending handovers")
    issues = (
        session.query(Issue)
        .filter(Issue.type == IssueType.HANDOVER_REVIEW)
        .filter(Issue.status == IssueStatus.OPEN)
        .order_by(Issue.created_at.desc())
        .all()
    )
    return [_enrich_issue(session, i) for i in issues]


@router.get("/api/issues/assignable-users", response_model=List[IssueAssigneeCandidateResponse])
def list_assignable_issue_users(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(AdminOnly),
):
    users = (
        session.query(User)
        .filter(
            User.role.in_([UserRole.RELAYOPS_MEMBER, UserRole.ADMIN])
            | (User.id == current_user.user_id)
        )
        .order_by(
            User.role.desc(),
            User.display_name.is_(None),
            User.display_name,
            User.username,
        )
        .all()
    )
    return [
        IssueAssigneeCandidateResponse(
            user_id=u.id, username=u.username, display_name=u.display_name, role=u.role
        )
        for u in users
    ]


@router.post("/api/issues/claim", response_model=ClaimIssuesResponse)
def claim_issues_endpoint(
    body: ClaimIssuesRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Self-assign one or more open issues to the acting user.

    Available to the elevated tier (admin / relayops_member) — the platform-wide
    Ops triage board where an on-duty member picks up open issues for
    themselves, individually or in bulk. Distinct from admin reassignment
    (PUT /api/issues/{id} with assignee_id), which stays platform-admin only.
    """
    claimed, skipped = issue_service.claim_issues(
        issue_ids=body.issue_ids,
        actor_user_id=current_user.user_id,
        actor_username=current_user.username,
        is_elevated=_is_elevated(current_user),
    )
    return ClaimIssuesResponse(
        claimed=[_enrich_issue(session, i) for i in claimed],
        skipped=[{"issue_id": s["issue_id"], "reason": s["reason"]} for s in skipped],
    )


@router.get("/api/issues/{issue_id}", response_model=IssueResponse)
def get_issue(
    issue_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    is_admin = _is_platform_admin(current_user.username)
    issue = session.query(Issue).filter(Issue.id == issue_id).first()
    if issue is None:
        raise NotFoundError("Issue not found")
    if not _user_can_access_issue(session, issue, current_user, is_admin):
        raise ForbiddenError("Not authorized")
    return _enrich_issue(session, issue)


def _issue_mmp_run_id(issue: Issue) -> Optional[int]:
    """The MMP production run that triggered this issue, for tracing it in the
    live body. Prefers the stored ``mmp_run_id`` pointer; falls back to the run
    id encoded in a pending issue's ``dedup_key`` (``flag:run_id``) so issues
    raised before the column existed still trace correctly."""
    if getattr(issue, "mmp_run_id", None) is not None:
        return issue.mmp_run_id
    dedup = issue.dedup_key or ""
    if ":" in dedup:
        tail = dedup.rsplit(":", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return None


@router.get("/api/issues/{issue_id}/mmp-raw")
def get_issue_mmp_raw(
    issue_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Live MMP "complete response body" for an MMP issue's bound model.

    Resolves the issue's Job → MMP binding and fetches the raw model object
    straight from MMP on demand (not a stored snapshot), so the issue detail
    shows the current attention flags + the latest production run, every run's
    ``approval_status`` annotated with its meaning. Returns ``{"error": ...}``
    when MMP is unreachable so the UI degrades gracefully.
    """
    is_admin = _is_platform_admin(current_user.username)
    issue = session.query(Issue).filter(Issue.id == issue_id).first()
    if issue is None:
        raise NotFoundError("Issue not found")
    if not _user_can_access_issue(session, issue, current_user, is_admin):
        raise ForbiddenError("Not authorized")
    if not (issue.type or "").startswith("mmp"):
        raise ValidationError("This issue is not an MMP issue")
    if not issue.job_id:
        raise ValidationError("Issue has no bound job to resolve the MMP model")

    job = session.query(Job).filter(Job.id == issue.job_id).first()
    repo = (job.mmp_project_id or "").strip() if job else ""
    model = (job.mmp_model_id or "").strip() if job else ""
    if not repo or not model:
        raise ValidationError(
            "Bound job has no MMP binding (mmp_project_id / mmp_model_id)")

    from core.agent.live_tools import _mmp, build_mmp_model_raw_view
    from core.integrations.mmp_interface import MmpApiError

    iface = _mmp()
    if not iface.is_configured():
        return {"error": "MMP platform not configured (base_url / bearer_token)"}
    try:
        return build_mmp_model_raw_view(
            iface, repo, model, max_runs=20,
            focus_run_id=_issue_mmp_run_id(issue))
    except LookupError as exc:
        return {"error": str(exc),
                "hint": "MMP project/model not found — the job binding may be stale."}
    except MmpApiError as exc:
        return {"error": f"MMP unreachable or returned an error: {exc}"}
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning("get_issue_mmp_raw failed")
        return {"error": f"Failed to read MMP model body: {exc}"}


@router.get("/api/issues/{issue_id}/export", response_model=ExportPayloadResponse)
def export_issue(
    issue_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    from api.routers.audit_logs import _build_issue_timeline_event

    is_admin = _is_platform_admin(current_user.username)
    issue = session.query(Issue).filter(Issue.id == issue_id).first()
    if issue is None:
        raise NotFoundError("Issue not found")
    if not _user_can_access_issue(session, issue, current_user, is_admin):
        raise ForbiddenError("Not authorized")

    raw_logs = (
        session.query(AuditLog)
        .filter(
            ((AuditLog.entity_type == "issue") & (AuditLog.entity_id == issue_id))
            | (AuditLog.entity_type == "notification")
        )
        .order_by(AuditLog.timestamp.asc())
        .all()
    )
    timeline = []
    db = get_db()
    for log in raw_logs:
        if log.entity_type == "notification":
            if (log.new_value or {}).get("related_entity_type") != "issue":
                continue
            if (log.new_value or {}).get("related_entity_id") != issue_id:
                continue
        timeline.append(_build_issue_timeline_event(log, db))

    workspace = None
    if issue.type != IssueType.HANDOVER_REVIEW:
        workspace = _build_issue_workspace_payload(session, issue)

    return ExportPayloadResponse(
        export_type="issue",
        generated_at=datetime.utcnow(),
        filters={"issue_id": issue_id},
        data={
            "issue": _enrich_issue(session, issue),
            "workspace": workspace,
            "timeline": timeline,
        },
    )


@router.get("/api/issues/{issue_id}/workspace", response_model=IssueActionWorkspaceResponse)
def get_issue_workspace(
    issue_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    is_admin = _is_platform_admin(current_user.username)
    issue = session.query(Issue).filter(Issue.id == issue_id).first()
    if issue is None:
        raise NotFoundError("Issue not found")
    if not _user_can_access_issue(session, issue, current_user, is_admin):
        raise ForbiddenError("Not authorized")
    if issue.type == IssueType.HANDOVER_REVIEW:
        raise ValidationError("handover_review issues do not use the action workspace")
    return _build_issue_workspace_payload(session, issue)


@router.post("/api/issues/{issue_id}/actions", response_model=IssueResponse)
def run_issue_action(
    issue_id: int,
    body: IssueActionRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    # Acting on a non-assigned issue requires the elevated tier (admin/relayops_member);
    # run_action has no reassignment, so platform-admin is not needed here.
    is_admin = _is_elevated(current_user)
    if body.action not in IssueActionType.ALL:
        raise ValidationError(f"Invalid action. Must be one of: {IssueActionType.ALL}")

    issue = issue_service.run_action(
        issue_id=issue_id,
        action=body.action,
        actor_user_id=current_user.user_id,
        actor_username=current_user.username,
        is_admin=is_admin,
        scenario_id=body.scenario_id,
        step_title=body.step_title,
        notes=body.notes,
        verification_notes=body.verification_notes,
        escalation_target=body.escalation_target,
        actions_taken=body.actions_taken,
        final_conclusion=body.final_conclusion,
        build_workspace_payload_fn=_build_workspace_payload_for_service,
    )
    return _enrich_issue(session, issue)


@router.post("/api/issues/{issue_id}/approve", response_model=IssueResponse)
def approve_handover(
    issue_id: int,
    body: HandoverApproveRequest = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not _is_platform_admin(current_user.username):
        raise ForbiddenError("Only Platform Admins can approve handovers")
    issue = issue_service.approve_handover(
        issue_id=issue_id,
        actor_user_id=current_user.user_id,
        actor_username=current_user.username,
    )
    return _enrich_issue(session, issue)


@router.post("/api/issues/{issue_id}/reject", response_model=IssueResponse)
def reject_handover(
    issue_id: int,
    body: HandoverRejectRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not _is_platform_admin(current_user.username):
        raise ForbiddenError("Only Platform Admins can reject handovers")
    issue = issue_service.reject_handover(
        issue_id=issue_id,
        rejection_reason=body.rejection_reason,
        actor_user_id=current_user.user_id,
        actor_username=current_user.username,
    )
    return _enrich_issue(session, issue)


@router.put("/api/issues/{issue_id}", response_model=IssueResponse)
def update_issue(
    issue_id: int,
    body: IssueUpdateRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    # Split tiers: reassignment + skipping the resolution note stay platform-admin
    # only; acting on a non-assigned issue is allowed for the elevated tier.
    is_admin = _is_platform_admin(current_user.username)
    is_elevated = _is_elevated(current_user)
    issue = issue_service.update_issue(
        issue_id=issue_id,
        actor_user_id=current_user.user_id,
        actor_username=current_user.username,
        is_admin=is_admin,
        is_elevated=is_elevated,
        new_status=body.status,
        new_assignee_id=body.assignee_id,
        resolution_description=body.resolution_description,
    )
    return _enrich_issue(session, issue)
