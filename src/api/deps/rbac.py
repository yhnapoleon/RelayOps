"""
RBAC (Role-Based Access Control) dependencies for FastAPI routes.

Note: ``UserRole.REGULAR_USER`` replaced ``UserRole.BUSINESS_OWNER`` —
the global role is now "regular user" (login identity) because per-
project bizowner moved to ``ProjectMember.role``. The legacy
``BusinessOwnerOrAdmin`` name is kept as an alias for now so the
hundred-odd existing routes don't need to rename their dependency.

Usage:
    from api.deps.rbac import require_role, AdminOnly, BusinessOwnerOrAdmin

    @router.post("/admin-only")
    async def admin_endpoint(current_user = Depends(AdminOnly)):
        ...

    @router.post("/flexible")
    async def flexible_endpoint(current_user = Depends(require_role("admin", "regular_user"))):
        ...
"""

from typing import List

from fastapi import Depends, HTTPException, status

from api.deps.auth import CurrentUser, get_current_user
from core.models.user import UserRole, normalize_role


def require_role(*roles: str):
    """
    Factory that returns a FastAPI dependency enforcing one of the given roles.

    Legacy 'business_owner' tokens are normalized to 'regular_user' so
    old JWTs and any in-flight DB rows transition transparently.

    Args:
        *roles: Allowed role strings (e.g. "admin", "regular_user", "relayops_member").

    Returns:
        A FastAPI dependency function that raises 403 if the user's role is not allowed.
    """
    allowed: List[str] = list(roles)

    async def _check(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        effective_role = normalize_role(current_user.role)
        if effective_role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role}' is not allowed. Required: {allowed}",
            )
        return current_user

    return _check


# ── Convenience pre-built dependencies ───────────────────────────────

AdminOnly = require_role(UserRole.ADMIN)
"""Dependency: only Platform Admins (role=admin) may proceed."""

ProjectEditorOrAdmin = require_role(UserRole.REGULAR_USER, UserRole.ADMIN, UserRole.RELAYOPS_MEMBER)
"""Dependency: any authenticated user (regular user / Ops member /
admin) may proceed. Project-level scoping (am I the owner of THIS
project, or a member of it?) is enforced separately by
``_project_access_state`` / ``_check_project_access`` at the service
layer — this gate just keeps unauthenticated requests out."""

# Legacy alias — kept so the many call sites don't need to rename.
# Semantically identical to ProjectEditorOrAdmin post-refactor.
BusinessOwnerOrAdmin = ProjectEditorOrAdmin

OpsMemberOrAdmin = require_role(UserRole.RELAYOPS_MEMBER, UserRole.ADMIN)
"""Dependency: Ops Members and Admins may proceed."""

AnyRole = require_role(UserRole.ADMIN, UserRole.REGULAR_USER, UserRole.RELAYOPS_MEMBER)
"""Dependency: any authenticated user with a known role may proceed."""
