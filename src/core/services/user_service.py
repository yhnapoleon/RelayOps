"""Local account bootstrap, lookup, and notification address helpers."""
import os
import re
from typing import List, Optional

from core.models.database import Database, get_db
from core.models.user import User, UserRole
from core.auth.auth import hash_password


def get_user_by_id(db: Database, user_id: int) -> Optional[User]:
    with db.get_session() as session:
        return session.query(User).filter(User.id == user_id).first()


def bootstrap_local_accounts() -> None:
    """Initialize local accounts without resetting existing credentials or roles."""
    from core.config import get_config
    cfg = get_config()
    accounts = []
    if cfg.auth_demo_accounts and not cfg.is_production:
        accounts = [
            ('admin', 'admin123', 'System Administrator', 'admin', ['relayops-demo-shared-ops']),
            ('testuser', 'test123', 'Test User', 'regular_user', ['relayops-demo-shared-ops']),
            ('bizowner2', 'biz123', 'Demo Owner', 'regular_user', []),
            ('relayopsmember1', 'relayops123', 'Demo Operator One', 'relayops_member', ['relayops-demo-shared-ops']),
            ('relayopsmember2', 'relayops123', 'Demo Operator Two', 'relayops_member', []),
        ]
    admin_password = os.getenv('RELAYOPS_ADMIN_PASSWORD', '')
    if admin_password:
        if not 12 <= len(admin_password) <= 1024:
            raise ValueError('RELAYOPS_ADMIN_PASSWORD must contain 12 to 1024 characters')
        admin_name = os.getenv('RELAYOPS_ADMIN_USERNAME', 'admin').strip().lower()
        if len(admin_name) > 255 or not re.fullmatch(r'[a-z0-9_.-]+', admin_name):
            raise ValueError('Invalid RELAYOPS_ADMIN_USERNAME')
        accounts = [a for a in accounts if a[0] != admin_name]
        accounts.insert(0, (admin_name, admin_password, 'Administrator', 'admin', []))
    with get_db().get_session() as session:
        for username, password, display_name, role, groups in accounts:
            user = session.query(User).filter(User.username == username).first()
            if user is None:
                session.add(User(username=username, display_name=display_name,
                                 email=f'{username}@example.com', role=role,
                                 password_hash=hash_password(password), group_keys=groups))
            elif not user.password_hash:
                user.password_hash = hash_password(password)
                user.group_keys = groups
                # Preserve roles already assigned to existing accounts.
        session.commit()
        if not session.query(User.id).filter(User.role == UserRole.ADMIN, User.password_hash.isnot(None)).first():
            raise RuntimeError('Configure RELAYOPS_ADMIN_USERNAME and RELAYOPS_ADMIN_PASSWORD to initialize an administrator')


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
    # Stored address (account email) is authoritative — try it first so a name
    # collision (MorganLee vs MorganLee2) can't misroute the mail.
    stored_email = getattr(user, "email", None)
    if stored_email:
        raw.append(stored_email)
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
