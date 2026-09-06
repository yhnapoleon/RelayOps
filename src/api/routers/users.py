"""Admin-only user management routes.

The Members dialog edits per-project membership. This module edits the
*global* login identity (User.role) — admin / regular_user / relayops_member —
and is gated to Platform Admins.

Why role_locked is auto-set on every change: the LDAP sync in
``user_service.get_or_create_user`` re-applies the LDAP-resolved role
on each login unless ``role_locked=True``. If the admin UI didn't lock
the role, the change would silently revert the next time the user
logged in. Locking it pins the admin's decision.

Platform owners (config.yaml ``platform_owners``) are special: their
role is always re-asserted to admin on login, regardless of the locked
flag. The list endpoint surfaces this so the UI can disable the role
selector for those rows rather than letting an admin attempt a change
that will silently revert.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser
from api.deps.db import get_session
from api.deps.rbac import AdminOnly
from api.schema import AdminUserResponse, UserRoleChangeRequest
from core.config import get_config
from core.exceptions import ConflictError, NotFoundError, ValidationError
from core.logging import get_logger
from core.models.entities import ProjectMember
from core.models.user import User, UserRole, normalize_role
from core.services.audit_service import log_audit

logger = get_logger(__name__)
router = APIRouter(tags=["admin-users"])


def _serialize_user(
    user: User,
    *,
    is_platform_owner: bool,
    project_count: int,
) -> Dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": normalize_role(user.role),
        "role_locked": bool(user.role_locked),
        "is_platform_owner": is_platform_owner,
        "project_count": project_count,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("/api/admin/users", response_model=List[AdminUserResponse])
def list_admin_users(
    search: Optional[str] = Query(None, description="Filter by username or display name"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(AdminOnly),
):
    """Return every user the admin can manage.

    ``project_count`` is the number of ProjectMember rows the user owns,
    so the UI can show "if I demote this person they're a bizowner on 4
    projects." ``is_platform_owner`` flags users whose admin role is
    pinned by config (UI should disable the role selector for them).
    """
    _ = current_user  # gating only — audit happens on writes
    platform_owners = {str(o).lower() for o in get_config().platform_owners}

    rows: List[User] = (
        session.query(User)
        .order_by(User.display_name.is_(None), User.display_name, User.username)
        .all()
    )

    # One pass for project counts so we don't N+1 on the listing.
    count_rows = (
        session.query(ProjectMember.user_id)
        .all()
    )
    project_counts: Dict[int, int] = {}
    for (user_id,) in count_rows:
        project_counts[user_id] = project_counts.get(user_id, 0) + 1

    query_text = (search or "").strip().lower()
    result = []
    for user in rows:
        haystack = f"{user.username} {user.display_name or ''}".lower()
        if query_text and query_text not in haystack:
            continue
        result.append(
            _serialize_user(
                user,
                is_platform_owner=user.username.lower() in platform_owners,
                project_count=project_counts.get(user.id, 0),
            )
        )
    return result


@router.patch("/api/admin/users/{user_id}/role", response_model=AdminUserResponse)
def change_user_role(
    user_id: int,
    body: UserRoleChangeRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(AdminOnly),
):
    """Change a user's global role and pin it so LDAP sync won't undo it.

    Guard rails:
      - role must be one of admin / regular_user / relayops_member
      - cannot change a platform_owner's role (they're pinned by config)
      - cannot demote the last remaining admin (would lock the platform
        out of further admin actions)
    """
    new_role = normalize_role(body.role)
    if new_role not in UserRole.ALL:
        raise ValidationError(f"Invalid role '{body.role}'. Allowed: {UserRole.ALL}")

    user = session.query(User).filter(User.id == user_id).first()
    if user is None:
        raise NotFoundError("User not found")

    platform_owners = {str(o).lower() for o in get_config().platform_owners}
    if user.username.lower() in platform_owners and new_role != UserRole.ADMIN:
        raise ValidationError(
            "Cannot change a platform owner's role — admin is pinned by "
            "config.yaml. Remove them from platform_owners first."
        )

    current_role = normalize_role(user.role)
    if current_role == new_role and bool(user.role_locked):
        # No-op: nothing to do. Surface as a conflict so the UI knows
        # to refresh instead of pretending it succeeded.
        raise ConflictError(f"User already has role '{new_role}' (locked)")

    # Last-admin guard: if we're demoting the only admin left, refuse.
    # The admin count read is in the same transaction, so it sees a
    # consistent snapshot relative to the upcoming flush.
    if current_role == UserRole.ADMIN and new_role != UserRole.ADMIN:
        admin_count = (
            session.query(User)
            .filter(User.role == UserRole.ADMIN)
            .count()
        )
        if admin_count <= 1:
            raise ValidationError(
                "Cannot demote the last remaining admin. Promote another "
                "user to admin first."
            )

    old_val = _serialize_user(
        user,
        is_platform_owner=user.username.lower() in platform_owners,
        project_count=0,  # counts not needed in the diff
    )
    user.role = new_role
    user.role_locked = True
    session.flush()
    session.refresh(user)

    new_val = _serialize_user(
        user,
        is_platform_owner=user.username.lower() in platform_owners,
        project_count=0,
    )
    log_audit(
        user_id=current_user.user_id,
        action="update",
        entity_type="user_role",
        entity_id=user.id,
        old_value=old_val,
        new_value=new_val,
    )
    logger.info(
        "Admin {} changed user {} ({}) role: {} -> {} (locked=True)",
        current_user.username, user.id, user.username, current_role, new_role,
    )

    # Re-compute project_count for the response so the UI doesn't have
    # to refetch the whole list just to update one row.
    pc = (
        session.query(ProjectMember)
        .filter(ProjectMember.user_id == user.id)
        .count()
    )
    return _serialize_user(
        user,
        is_platform_owner=user.username.lower() in platform_owners,
        project_count=pc,
    )
