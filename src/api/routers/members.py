"""Project member and project-support-group management routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.deps.rbac import BusinessOwnerOrAdmin
from api.schema import (
    ProjectMemberAdd,
    ProjectMemberCandidateResponse,
    ProjectMemberResponse,
    ProjectMemberRoleUpdate,
    ProjectOwnerGroupUpdateRequest,
    ProjectOwnerTransferRequest,
    ProjectResponse,
    ProjectSupportGroupBindRequest,
    ProjectSupportGroupResponse,
)
from core.exceptions import ConflictError, ForbiddenError, NotFoundError, SystemLockedError, ValidationError
from core.logging import get_logger
from core.models.database import get_db
from core.models.entities import (
    Issue,
    IssueStatus,
    Product,
    Project,
    ProjectMember,
    ProjectSupportGroup,
    SupportGroup,
)
from core.models.user import User, is_elevated_role
from core.services.user_service import get_user_by_id
from core.services.audit_service import (
    log_audit,
    serialize_project,
    serialize_project_support_group,
)
from core.services.support_group_service import (
    resolve_support_group_snapshot,
    user_has_project_group_access,
)

logger = get_logger(__name__)
router = APIRouter(tags=["members"])


def _enrich_project_response(project: Project) -> ProjectResponse:
    owner = get_user_by_id(get_db(), project.owner_id)
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description or "",
        owner_id=project.owner_id,
        owner_group_id=project.owner_group_id,
        owner_group_name=project.owner_group_name_snapshot or None,
        is_system=bool(project.is_system),
        owner_username=owner.username if owner else None,
        owner_display_name=owner.display_name if owner else None,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def _serialize_project_member(
    pm: ProjectMember,
    *,
    username: str | None = None,
    display_name: str | None = None,
    role: str | None = None,
    active_issue_count: int = 0,
    is_owner: bool = False,
) -> Dict[str, Any]:
    return {
        "id": pm.id,
        "project_id": pm.project_id,
        "user_id": pm.user_id,
        "username": username,
        "display_name": display_name,
        "role": role,
        "support_group_id": pm.support_group_id,
        "support_group_name": pm.support_group_name_snapshot or None,
        "active_issue_count": active_issue_count,
        "is_owner": is_owner,
        "added_by": pm.added_by,
        "created_at": pm.created_at.isoformat() if isinstance(pm.created_at, datetime) else pm.created_at,
    }


def _project_access_state(
    session: Session, project_id: int, current_user: CurrentUser
) -> tuple[Optional[Project], str]:
    project = session.query(Project).filter(Project.id == project_id).first()
    if project is None:
        return None, "not_found"
    if project.is_system == 1:
        return project, "system_locked"
    # Platform-level elevation (global admin OR global relayops_member) grants
    # owner-equivalent reach over a project's membership — the same reach
    # ``is_elevated_role`` already gives these roles over the project's other
    # assets (edit / delete / cron-SLA). A global relayops_member can therefore
    # add / edit / remove members in ANY project they can reach. The one
    # carve-out is owner transfer, which stays a platform-admin/true-owner
    # power and is guarded explicitly in ``transfer_project_owner``.
    if is_elevated_role(current_user.role) or project.owner_id == current_user.user_id:
        return project, "owner"
    is_member = (
        session.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == current_user.user_id,
        )
        .first()
        is not None
    )
    if is_member:
        return project, "member"
    if user_has_project_group_access(session, project, current_user.ad_groups):
        return project, "member"
    return None, "forbidden"


def _require_access(state: str, *, allow_member: bool = False) -> None:
    if state == "not_found":
        raise NotFoundError("Project not found")
    if state == "system_locked":
        raise SystemLockedError("System-managed project is read-only")
    if state == "forbidden":
        raise ForbiddenError("Not authorized")
    if not allow_member and state == "member":
        raise ForbiddenError("Only the project owner can perform this action")


def _issue_counts_for_project(session: Session, project_id: int) -> dict[int, int]:
    product_ids = [
        row.id
        for row in session.query(Product.id).filter(Product.project_id == project_id).all()
    ]
    if not product_ids:
        return {}
    rows = (
        session.query(Issue.assignee_id)
        .filter(
            Issue.product_id.in_(product_ids),
            Issue.assignee_id.is_not(None),
            Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS]),
        )
        .all()
    )
    counts: dict[int, int] = {}
    for (assignee_id,) in rows:
        if assignee_id is None:
            continue
        counts[assignee_id] = counts.get(assignee_id, 0) + 1
    return counts


def _build_project_support_group_response(binding: ProjectSupportGroup) -> ProjectSupportGroupResponse:
    return ProjectSupportGroupResponse(
        id=binding.id,
        project_id=binding.project_id,
        support_group_id=binding.support_group_id,
        support_group_name=binding.support_group_name_snapshot or f"Group #{binding.support_group_id}",
        created_by=binding.created_by,
        created_at=binding.created_at,
    )


@router.get("/api/projects/{project_id}/members", response_model=List[ProjectMemberResponse])
def list_members(
    project_id: int,
    search: str | None = Query(None, description="Search by username or display name"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    project, state = _project_access_state(session, project_id, current_user)
    if state == "not_found":
        raise NotFoundError("Project not found")
    if state == "forbidden":
        raise ForbiddenError("Not authorized")
    if state == "system_locked":
        return []

    issue_counts = _issue_counts_for_project(session, project_id)
    rows = (
        session.query(ProjectMember, User)
        .join(User, ProjectMember.user_id == User.id)
        .filter(ProjectMember.project_id == project_id)
        .all()
    )
    query_text = (search or "").strip().lower()
    result = []
    for pm, user in rows:
        haystack = f"{user.username} {user.display_name or ''}".lower()
        if query_text and query_text not in haystack:
            continue
        result.append(
            ProjectMemberResponse(
                id=pm.id,
                project_id=pm.project_id,
                user_id=pm.user_id,
                username=user.username,
                display_name=user.display_name,
                # `role` is now the per-project role from the membership row.
                # `global_role` carries the user's login identity for callers
                # that still want to display it (e.g. "Admin" badge next to
                # the project role).
                role=pm.role or "relayops_member",
                global_role=user.role,
                support_group_id=pm.support_group_id,
                support_group_name=pm.support_group_name_snapshot or None,
                active_issue_count=issue_counts.get(pm.user_id, 0),
                is_owner=project.owner_id == pm.user_id if project else False,
                added_by=pm.added_by,
                created_at=pm.created_at,
            )
        )
    # Sort: owner first, then by display_name/username.
    result.sort(key=lambda m: (
        not m.is_owner,
        (m.display_name or m.username or "").lower(),
    ))
    return result


@router.get(
    "/api/projects/{project_id}/member-candidates",
    response_model=List[ProjectMemberCandidateResponse],
)
def list_member_candidates(
    project_id: int,
    search: str | None = Query(None, description="Search by username or display name"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    project, state = _project_access_state(session, project_id, current_user)
    _require_access(state)

    existing_member_user_ids = {
        row.user_id
        for row in session.query(ProjectMember.user_id)
        .filter(ProjectMember.project_id == project_id)
        .all()
    }
    # Owner is now part of existing_member_user_ids (post-refactor he's
    # an actual ProjectMember row), so we no longer need a separate
    # owner_id filter — the membership check below handles it uniformly.
    rows = (
        session.query(User)
        .order_by(
            User.display_name.is_(None),
            User.display_name,
            User.username,
        )
        .all()
    )
    query_text = (search or "").strip().lower()
    result = []
    for user in rows:
        if user.id in existing_member_user_ids:
            continue
        haystack = f"{user.username} {user.display_name or ''}".lower()
        if query_text and query_text not in haystack:
            continue
        result.append(
            ProjectMemberCandidateResponse(
                user_id=user.id,
                username=user.username,
                display_name=user.display_name,
                role=user.role,
            )
        )
    return result


@router.post(
    "/api/projects/{project_id}/members",
    response_model=ProjectMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    project_id: int,
    body: ProjectMemberAdd,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    project, state = _project_access_state(session, project_id, current_user)
    _require_access(state)

    # Normalize the target username before looking up the account.
    target_username = body.username.strip().lower()
    if not target_username:
        raise ValidationError("Username is required")
    # Per-project role only — body validator already restricts to 'relayops_member'.
    # The project's business_owner role is held by Project.owner_id and only
    # changes via the owner-transfer endpoint below, never by /members.
    project_role = body.role
    target_user = session.query(User).filter(User.username == target_username).first()

    # Conflict checks first, before mutating any state, so a rejected
    # request never touches the database.
    if target_user is not None:
        if project and project.owner_id == target_user.id:
            raise ValidationError(
                "Cannot add the project owner as a member — they are already "
                "the project's business owner."
            )
        existing = (
            session.query(ProjectMember)
            .filter(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == target_user.id,
            )
            .first()
        )
        if existing is not None:
            raise ConflictError("User is already a member of this project")

    # Pre-provision a stub user row when the username hasn't logged in yet,
    # so the membership can be granted ahead of their first login. Unlike
    # the pre-refactor flow, we no longer overwrite the user's global role
    # from this endpoint — the stub starts as 'regular_user' and LDAP /
    # admin processes own that field. Membership lives entirely in the
    # ProjectMember row.
    if target_user is None:
        target_user = User(
            username=target_username,
            display_name=None,
            # Default global role; will be updated by LDAP sync on first
            # login if applicable. Project membership is independent.
            role="regular_user",
            role_locked=False,
        )
        session.add(target_user)
        session.flush()
        session.refresh(target_user)
        logger.info(
            "Pre-provisioned stub user {} for project {} membership by {}",
            target_username, project_id, current_user.username,
        )

    sg_id, sg_name = resolve_support_group_snapshot(session, body.support_group_id)
    pm = ProjectMember(
        project_id=project_id,
        user_id=target_user.id,
        role=project_role,
        support_group_id=sg_id,
        support_group_name_snapshot=sg_name,
        added_by=current_user.user_id,
    )
    session.add(pm)
    session.flush()
    session.refresh(pm)

    response = ProjectMemberResponse(
        id=pm.id,
        project_id=pm.project_id,
        user_id=pm.user_id,
        username=target_user.username,
        display_name=target_user.display_name,
        role=pm.role,
        global_role=target_user.role,
        support_group_id=pm.support_group_id,
        support_group_name=pm.support_group_name_snapshot or None,
        active_issue_count=0,
        is_owner=False,
        added_by=pm.added_by,
        created_at=pm.created_at,
    )
    audit_value = _serialize_project_member(
        pm,
        username=target_user.username,
        display_name=target_user.display_name,
        role=pm.role,
    )
    log_audit(
        user_id=current_user.user_id,
        action="create",
        entity_type="project_member",
        entity_id=response.id,
        new_value=audit_value,
    )
    logger.info("Added member {} to project {} by {}", body.username, project_id, current_user.username)
    return response


@router.delete(
    "/api/projects/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_member(
    project_id: int,
    user_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    _, state = _project_access_state(session, project_id, current_user)
    _require_access(state)

    row = (
        session.query(ProjectMember, User)
        .join(User, ProjectMember.user_id == User.id)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        .first()
    )
    if row is None:
        raise NotFoundError("Member not found in this project")

    pm, user = row
    # Removing the project's business owner would break the invariant
    # "every project has exactly one bizowner member row" and orphan
    # Project.owner_id. Disallow — ownership has to be transferred first
    # via POST /api/projects/{id}/owner.
    if pm.role == "business_owner":
        raise ValidationError(
            "Cannot remove the project's business owner. Transfer ownership "
            "before removing this member."
        )
    old_val = _serialize_project_member(
        pm,
        username=user.username,
        display_name=user.display_name,
        role=pm.role,
    )
    removed_member_id = pm.id
    session.delete(pm)
    log_audit(
        user_id=current_user.user_id,
        action="delete",
        entity_type="project_member",
        entity_id=removed_member_id,
        old_value=old_val,
    )
    logger.info("Removed member {} from project {} by {}", user_id, project_id, current_user.username)


@router.patch(
    "/api/projects/{project_id}/members/{user_id}/role",
    response_model=ProjectMemberResponse,
)
def update_member_role(
    project_id: int,
    user_id: int,
    body: ProjectMemberRoleUpdate,
    session: Session = Depends(get_session),
    # Note: not BusinessOwnerOrAdmin — a project member is allowed to flip
    # their *own* role between relayops_member and product_member. Per-target
    # authorization is enforced below (admin / project owner / self).
    current_user: CurrentUser = Depends(get_current_user),
):
    """Change a project member's per-project role between ``relayops_member`` and
    ``product_member``.

    Authorization:
      - Platform admins may change anyone's role.
      - Platform-level Ops members (global ``relayops_member``) may change anyone's
        role in any project they can reach — owner-equivalent membership reach.
      - The project's Business Owner may change anyone's role.
      - A member may change their *own* role.

    The ``business_owner`` row is invariant-bound to ``Project.owner_id`` and
    changes only via the owner-transfer endpoint — attempts to touch it here
    are rejected.
    """
    project = session.query(Project).filter(Project.id == project_id).first()
    if project is None:
        raise NotFoundError("Project not found")
    if project.is_system == 1:
        raise SystemLockedError("System-managed project is read-only")

    row = (
        session.query(ProjectMember, User)
        .join(User, ProjectMember.user_id == User.id)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        .first()
    )
    if row is None:
        raise NotFoundError("Member not found in this project")
    pm, user = row

    # Global admin OR global relayops_member both get owner-equivalent membership
    # reach here (platform-level elevation); the project owner and the member
    # themselves are also allowed.
    is_elevated = is_elevated_role(current_user.role)
    is_project_owner = project.owner_id == current_user.user_id
    is_self = pm.user_id == current_user.user_id
    if not (is_elevated or is_project_owner or is_self):
        raise ForbiddenError("Not authorized to change this member's role")

    if pm.role == "business_owner":
        raise ValidationError(
            "Cannot change the role of the project's business owner. "
            "Transfer ownership instead."
        )

    if pm.role != body.role:
        old_val = _serialize_project_member(
            pm,
            username=user.username,
            display_name=user.display_name,
            role=pm.role,
        )
        pm.role = body.role
        session.flush()
        session.refresh(pm)
        new_val = _serialize_project_member(
            pm,
            username=user.username,
            display_name=user.display_name,
            role=pm.role,
        )
        log_audit(
            user_id=current_user.user_id,
            action="update",
            entity_type="project_member",
            entity_id=pm.id,
            old_value=old_val,
            new_value=new_val,
        )
        logger.info(
            "Changed role of member {} in project {} from {} to {} by {}",
            user_id, project_id, old_val.get("role"), pm.role, current_user.username,
        )

    issue_count = _issue_counts_for_project(session, project_id).get(pm.user_id, 0)
    return ProjectMemberResponse(
        id=pm.id,
        project_id=pm.project_id,
        user_id=pm.user_id,
        username=user.username,
        display_name=user.display_name,
        role=pm.role,
        global_role=user.role,
        support_group_id=pm.support_group_id,
        support_group_name=pm.support_group_name_snapshot or None,
        active_issue_count=issue_count,
        # business_owner case rejected above, so this row is never the owner.
        is_owner=False,
        added_by=pm.added_by,
        created_at=pm.created_at,
    )


@router.get(
    "/api/projects/{project_id}/support-groups",
    response_model=List[ProjectSupportGroupResponse],
)
def list_project_support_groups(
    project_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _, state = _project_access_state(session, project_id, current_user)
    if state == "not_found":
        raise NotFoundError("Project not found")
    if state == "forbidden":
        raise ForbiddenError("Not authorized")

    bindings = (
        session.query(ProjectSupportGroup)
        .filter(ProjectSupportGroup.project_id == project_id)
        .order_by(ProjectSupportGroup.created_at.asc(), ProjectSupportGroup.id.asc())
        .all()
    )
    return [_build_project_support_group_response(b) for b in bindings]


@router.post(
    "/api/projects/{project_id}/support-groups",
    response_model=ProjectSupportGroupResponse,
    status_code=status.HTTP_201_CREATED,
)
def bind_project_support_group(
    project_id: int,
    body: ProjectSupportGroupBindRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    _, state = _project_access_state(session, project_id, current_user)
    _require_access(state)

    support_group = session.query(SupportGroup).filter(SupportGroup.id == body.support_group_id).first()
    if support_group is None:
        raise NotFoundError("Support group not found")

    existing = (
        session.query(ProjectSupportGroup)
        .filter(
            ProjectSupportGroup.project_id == project_id,
            ProjectSupportGroup.support_group_id == body.support_group_id,
        )
        .first()
    )
    if existing is not None:
        raise ConflictError("Support group is already bound to this project")

    binding = ProjectSupportGroup(
        project_id=project_id,
        support_group_id=support_group.id,
        support_group_name_snapshot=support_group.group_name,
        created_by=current_user.user_id,
    )
    session.add(binding)
    session.flush()
    session.refresh(binding)

    response = _build_project_support_group_response(binding)
    log_audit(
        user_id=current_user.user_id,
        action="create",
        entity_type="project_support_group",
        entity_id=response.id,
        new_value=serialize_project_support_group(binding),
    )
    return response


@router.delete(
    "/api/projects/{project_id}/support-groups/{support_group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def unbind_project_support_group(
    project_id: int,
    support_group_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    _, state = _project_access_state(session, project_id, current_user)
    _require_access(state)

    binding = (
        session.query(ProjectSupportGroup)
        .filter(
            ProjectSupportGroup.project_id == project_id,
            ProjectSupportGroup.support_group_id == support_group_id,
        )
        .first()
    )
    if binding is None:
        raise NotFoundError("Project support-group binding not found")

    old_val = serialize_project_support_group(binding)
    session.delete(binding)
    log_audit(
        user_id=current_user.user_id,
        action="delete",
        entity_type="project_support_group",
        entity_id=support_group_id,
        old_value=old_val,
    )


@router.post("/api/projects/{project_id}/owner", response_model=ProjectResponse)
def transfer_project_owner(
    project_id: int,
    body: ProjectOwnerTransferRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    """Transfer a project's Business Owner to another username.

    Pre-provisions a stub user when the target username hasn't logged in
    yet (same flow as add_member). Demotes the current owner's
    ProjectMember row to 'relayops_member' and promotes (or creates) the
    target's ProjectMember row to 'business_owner', keeping the invariant
    that ``Project.owner_id`` always matches exactly one ProjectMember
    row with ``role='business_owner'``.
    """
    project, state = _project_access_state(session, project_id, current_user)
    _require_access(state)

    # Owner transfer is a platform-admin-only power (see the ``is_elevated_role``
    # docstring's exception list) — a global relayops_member's owner-equivalent reach
    # over membership deliberately stops short of handing ownership off. Only a
    # true platform admin or the project's own owner may do this.
    if current_user.role != "admin" and project.owner_id != current_user.user_id:
        raise ForbiddenError("Only the project owner or a platform admin can transfer ownership")

    target_username = body.username.strip().lower()
    if not target_username:
        raise ValidationError("Username is required")

    target_user = session.query(User).filter(User.username == target_username).first()
    if target_user is None:
        # Pre-provision a stub so ownership can land before the new owner's
        # first login; LDAP / admin processes still own User.role.
        target_user = User(
            username=target_username,
            display_name=None,
            role="regular_user",
            role_locked=False,
        )
        session.add(target_user)
        session.flush()
        session.refresh(target_user)
        logger.info(
            "Pre-provisioned stub user {} for owner transfer of project {} by {}",
            target_username, project_id, current_user.username,
        )

    if project.owner_id == target_user.id:
        raise ValidationError("This user is already the project's Business Owner")

    old_owner_id = project.owner_id
    old_val = serialize_project(project)

    # Demote the outgoing owner's ProjectMember row to relayops_member so the
    # invariant "exactly one business_owner row per project" holds. If the
    # row is somehow missing (legacy data), we skip — system_seed backfill
    # is the authoritative repair path.
    old_owner_pm = (
        session.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == old_owner_id,
        )
        .first()
    )
    if old_owner_pm is not None:
        old_owner_pm.role = "relayops_member"

    # Promote the new owner. Create a ProjectMember row when the target
    # isn't already a member — transferring directly to an outsider is a
    # supported flow (mirrors add_member's stub-user provisioning).
    new_owner_pm = (
        session.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == target_user.id,
        )
        .first()
    )
    if new_owner_pm is None:
        new_owner_pm = ProjectMember(
            project_id=project_id,
            user_id=target_user.id,
            role="business_owner",
            added_by=current_user.user_id,
        )
        session.add(new_owner_pm)
    else:
        new_owner_pm.role = "business_owner"

    project.owner_id = target_user.id
    project.updated_at = datetime.utcnow()
    session.flush()
    session.refresh(project)

    enriched = _enrich_project_response(project)
    log_audit(
        user_id=current_user.user_id,
        action="update",
        entity_type="project",
        entity_id=project.id,
        old_value=old_val,
        new_value=enriched.model_dump(mode="json"),
    )
    logger.info(
        "Transferred ownership of project {} from user {} to user {} ({}) by {}",
        project_id, old_owner_id, target_user.id, target_username, current_user.username,
    )
    return enriched


@router.put("/api/projects/{project_id}/owner-group", response_model=ProjectResponse)
def update_project_owner_group(
    project_id: int,
    body: ProjectOwnerGroupUpdateRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    project, state = _project_access_state(session, project_id, current_user)
    _require_access(state)

    old_val = serialize_project(project)
    owner_group_name = ""
    if body.owner_group_id is not None:
        owner_group = session.query(SupportGroup).filter(SupportGroup.id == body.owner_group_id).first()
        if owner_group is None:
            raise NotFoundError("Support group not found")
        project.owner_group_id = owner_group.id
        owner_group_name = owner_group.group_name
    else:
        project.owner_group_id = None
    project.owner_group_name_snapshot = owner_group_name
    project.updated_at = datetime.utcnow()
    session.flush()
    session.refresh(project)

    enriched = _enrich_project_response(project)
    log_audit(
        user_id=current_user.user_id,
        action="update",
        entity_type="project",
        entity_id=enriched.id,
        old_value=old_val,
        new_value=enriched.model_dump(mode="json"),
    )
    return enriched
