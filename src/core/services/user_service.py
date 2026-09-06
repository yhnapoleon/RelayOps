"""User business logic — role resolution, lookup, creation, LDAP sync."""
from typing import Dict, List, Optional

from core.models.database import Database, get_db
from core.models.user import User, UserRole, normalize_role
from core.logging import get_logger

logger = get_logger(__name__)


def resolve_role(username: str, ad_groups: Optional[list] = None) -> str:
    """
    Determine a user's *global* role.

    This is the login identity (admin / relayops_member / regular_user), not
    the per-project role — the latter is stored on ProjectMember.role and
    is fully independent of what this function returns.

    Priority:
      1. Platform Admin — username in config.yaml platform_owners
      2. Ops Member     — user belongs to a Ops-related LDAP group
      3. Regular User   — default fallback (formerly 'business_owner')

    Args:
        username: Normalized (lowercase) username.
        ad_groups: List of LDAP group DNs the user belongs to.

    Returns:
        One of UserRole.ADMIN / RELAYOPS_MEMBER / REGULAR_USER.
    """
    from core.config import get_config

    config = get_config()

    # 1. Platform Admin via config.yaml
    if username in config.platform_owners:
        return UserRole.ADMIN

    # 2. Ops Member via LDAP group (group CN contains "relayops")
    if ad_groups:
        for group_dn in ad_groups:
            group_dn_lower = group_dn.lower()
            if "relayops" in group_dn_lower:
                return UserRole.RELAYOPS_MEMBER

    # 3. Default: regular user (no special privileges)
    return UserRole.REGULAR_USER


def get_user_by_id(db: "Database", user_id: int) -> Optional["User"]:
    """
    Look up a user by primary key.

    Args:
        db: The Database instance.
        user_id: The user's primary key.

    Returns:
        The User instance, or None if not found.
    """
    session = db.get_session()
    try:
        return session.query(User).filter(User.id == user_id).first()
    finally:
        session.close()


def get_or_create_user(
    username: str,
    display_name: Optional[str] = None,
    ad_groups: Optional[list] = None,
    email: Optional[str] = None,
) -> "User":
    """
    Get an existing user by username or create a new one.
    Also updates role, display_name, and email on every login.

    Args:
        username: The normalized (lowercase) username.
        display_name: Optional display name from LDAP.
        ad_groups: LDAP group memberships for role resolution.
        email: Optional email (LDAP ``mail``) to persist for notifications.

    Returns:
        The existing or newly created User instance.
    """
    from core.config import get_config

    db = get_db()
    session = db.get_session()
    ldap_role = resolve_role(username, ad_groups)
    is_platform_owner = username in get_config().platform_owners
    try:
        user = session.query(User).filter(User.username == username).first()
        if user is None:
            user = User(username=username, display_name=display_name, role=ldap_role, email=email)
            session.add(user)
            session.commit()
            session.refresh(user)
            logger.info("Created new user: {} role={}", username, ldap_role)
        else:
            changed = False
            if display_name and user.display_name != display_name:
                user.display_name = display_name
                changed = True
            if email and user.email != email:
                user.email = email
                changed = True
            # Role precedence: a config platform owner is always admin; an
            # admin-assigned (locked) role is preserved; otherwise the LDAP
            # resolver wins. This lets the Members dialog pin a role that
            # survives subsequent logins.
            if is_platform_owner:
                target_role = UserRole.ADMIN
            elif user.role_locked:
                # Translate legacy 'business_owner' to its current name
                # while preserving the locked-by-admin intent.
                target_role = normalize_role(user.role)
            else:
                target_role = ldap_role
            if user.role != target_role:
                user.role = target_role
                changed = True
            if changed:
                session.commit()
                session.refresh(user)
        return user
    finally:
        session.close()


def sync_users_from_ldap(ldap_users: List[Dict]) -> Dict[str, int]:
    """
    Upsert LDAP users into local users table for startup bootstrap.

    Args:
        ldap_users: List of LDAP user dicts with keys:
            username, name(display_name), member_of.

    Returns:
        Sync statistics: total/created/updated/skipped.
    """
    db = get_db()
    session = db.get_session()
    created = 0
    updated = 0
    skipped = 0
    try:
        for item in ldap_users:
            username_raw = item.get("username")
            if not username_raw:
                skipped += 1
                continue

            username = str(username_raw).strip().lower()
            if not username:
                skipped += 1
                continue

            display_name = item.get("name")
            email = item.get("email")
            ad_groups = item.get("member_of")
            if ad_groups and isinstance(ad_groups, str):
                ad_groups = [ad_groups]

            role = resolve_role(username, ad_groups)

            user = session.query(User).filter(User.username == username).first()
            if user is None:
                user = User(username=username, display_name=display_name, role=role, email=email)
                session.add(user)
                created += 1
                continue

            changed = False
            if display_name and user.display_name != display_name:
                user.display_name = display_name
                changed = True
            if email and user.email != email:
                user.email = email
                changed = True
            # Don't clobber a manually-assigned (locked) role during bulk sync.
            if not user.role_locked and user.role != role:
                user.role = role
                changed = True
            if changed:
                updated += 1

        session.commit()
        return {
            "total": len(ldap_users),
            "created": created,
            "updated": updated,
            "skipped": skipped,
        }
    except Exception:
        session.rollback()
        logger.opt(exception=True).error("Failed to sync users from LDAP")
        raise
    finally:
        session.close()


def _email_local_part_from_name(display_name: Optional[str]) -> str:
    """RelayOps  email local part from name."""
    return "".join((display_name or "").split())


def resolve_user_email_candidates(user: Optional["User"]) -> List[str]:
    """RelayOps resolve user email candidates."""
    if user is None:
        return []
    from core.config import get_config

    cfg = get_config()
    raw: List[str] = []
    # Stored address (LDAP `mail`) is authoritative — try it first so a name
    # collision (MorganLee vs MorganLee2) can't misroute the mail.
    ldap_email = getattr(user, "email", None)
    if ldap_email:
        raw.append(ldap_email)
    # Constructed forms are best-effort fallbacks for users with no stored email.
    local = _email_local_part_from_name(getattr(user, "display_name", "") or "")
    if local and cfg.email_domain:
        raw.append(f"{local}@{cfg.email_domain}")
    username = getattr(user, "username", "") or ""
    if username and cfg.email_fallback_domain:
        raw.append(f"{username}@{cfg.email_fallback_domain}")

    seen = set()
    ordered: List[str] = []
    for addr in raw:
        cleaned = (addr or "").strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            ordered.append(cleaned)
    return ordered


def resolve_user_email(user: Optional["User"]) -> Optional[str]:
    """Primary deliverable address for ``user`` (first candidate) or None.

    Thin wrapper over :func:`resolve_user_email_candidates` for callers that
    only need the single best address.
    """
    candidates = resolve_user_email_candidates(user)
    return candidates[0] if candidates else None
