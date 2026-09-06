"""User entity model."""
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String

from core.models.database import Base


class UserRole:
    """Global user role constants — this is the *login identity*, not the
    per-project role.

    Project-scoped roles live on ``ProjectMember.role`` and are entirely
    decoupled from these values. A user whose global role is REGULAR_USER
    can still be a project's business owner, and a global RELAYOPS_MEMBER can
    be a relayops_member in some projects and not at all in others.

    REGULAR_USER replaces the older 'business_owner' global role — the
    rename happened when project ownership was extracted into
    ProjectMember and the global 'business_owner' label became
    misleading. ``LEGACY_BUSINESS_OWNER`` is kept as an accepted alias so
    pre-rename JWTs and DB rows still authenticate, but new code should
    write REGULAR_USER.
    """

    ADMIN = "admin"
    REGULAR_USER = "regular_user"
    RELAYOPS_MEMBER = "relayops_member"

    # BC alias — pre-rename value still accepted on read paths.
    LEGACY_BUSINESS_OWNER = "business_owner"

    ALL = [ADMIN, REGULAR_USER, RELAYOPS_MEMBER]


def normalize_role(value: Optional[str]) -> str:
    """Map any legacy role string to its current canonical value.

    Only the 'business_owner' -> 'regular_user' rename needs translating;
    everything else passes through unchanged. Unknown values are returned
    as-is (callers downstream do strict membership checks against
    ``UserRole.ALL``).
    """
    if value == UserRole.LEGACY_BUSINESS_OWNER:
        return UserRole.REGULAR_USER
    return value or UserRole.REGULAR_USER


def is_elevated_role(value: Optional[str]) -> bool:
    """True for global roles that get owner-equivalent reach over project
    assets (see-all + act-as-owner on edit/cron-SLA/delete-assets).

    = ``admin`` or ``relayops_member``. Per the platform decision, relayops_member is a
    small, trusted, high-privilege population and should hold full reach
    EXCEPT platform-admin-only powers, which are gated separately and NOT
    covered here: admin panel, audit-log full view, account management, global templates,
    issue reassignment, handover/version approval, and owner transfer.

    This is a pure role check (no ``platform_owners`` config dependency) so
    the service layer can call it directly; callers that also honour the
    username-based ``platform_owners`` allow-list OR this in on top.
    """
    return normalize_role(value) in (UserRole.ADMIN, UserRole.RELAYOPS_MEMBER)


class User(Base):
    """
    User account model.

    Stores a salted password hash, session version, and local profile.

    Attributes:
        id: Primary key.
        username: Unique username (normalized to lowercase).
        display_name: Display name from local account.
        role: Global user role (admin / regular_user / relayops_member). NOT
            the per-project role — that lives on ProjectMember.role.
        role_locked: Records that an administrator assigned the global role.
        password_hash: Salted password hash; unset accounts cannot sign in.
        token_version: Incremented to revoke all sessions.
        group_keys: Locally assigned support-group memberships.
        created_at: Account creation timestamp.
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(255), unique=True, nullable=False)
    display_name = Column(String(255), nullable=True)
    password_hash = Column(String(255), nullable=True)
    token_version = Column(Integer, nullable=False, default=0)
    group_keys = Column(JSON, nullable=True, default=list)
    # Optional stored notification address. See resolve_user_email_candidates
    # for configured fallback addresses when it is unset.
    email = Column(String(320), nullable=True)
    role = Column(String(50), nullable=False, default=UserRole.REGULAR_USER)
    role_locked = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
