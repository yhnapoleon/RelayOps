"""Support-group helpers and seeded directory-preview data."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Optional

from core.auth import ldap_auth
from core.models.entities import (
    ProjectMember,
    ProjectRole,
    ProjectSupportGroup,
    SupportGroup,
    SupportGroupSourceType,
)


DEFAULT_SUPPORT_GROUP_KEY = "relayops-group"
DEFAULT_SUPPORT_GROUP_NAME = "Ops Group"
DEFAULT_SUPPORT_GROUP_DESCRIPTION = "Default Ops support group used for all routing."


SEEDED_SUPPORT_GROUPS = [
    {
        "group_key": "relayops-data-platform",
        "group_name": "Ops Data Platform",
        "description": "Handles scheduled data jobs and platform operations.",
        "source_type": SupportGroupSourceType.SEEDED,
    },
    {
        "group_key": "relayops-app-ops",
        "group_name": "Ops App Ops",
        "description": "Handles application health, restarts, and runtime incidents.",
        "source_type": SupportGroupSourceType.SEEDED,
    },
    {
        "group_key": "relayops-ml-monitoring",
        "group_name": "Ops ML Monitoring",
        "description": "Handles model monitoring alerts and drift follow-up.",
        "source_type": SupportGroupSourceType.SEEDED,
    },
    {
        "group_key": "relayops-demo-shared-ops",
        "group_name": "Demo Shared Ops",
        "description": "Demo group used to show shared Demo support ownership across admin, testuser, and relayopsmember1.",
        "source_type": SupportGroupSourceType.SEEDED,
        "external_ref": "cn=relayops-demo-shared-ops,ou=groups,dc=example,dc=com",
    },
]


def normalize_group_key(value: Optional[str]) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    chars = []
    for char in raw:
        if char.isalnum():
            chars.append(char)
        elif char in {" ", "_", ".", "/"}:
            chars.append("-")
        elif char == "-":
            chars.append("-")
    normalized = "".join(chars)
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized.strip("-")


def resolve_support_group_snapshot(
    session,
    support_group_id: Optional[int],
    support_group_text: Optional[str] = None,
) -> tuple[Optional[int], str]:
    """Always resolve to the default Ops support-group snapshot."""
    default_group = ensure_default_support_group(session)
    return default_group.id, default_group.group_name


def ensure_default_support_group(session) -> SupportGroup:
    """Ensure the default Ops support group exists and return it."""
    support_group = (
        session.query(SupportGroup)
        .filter(SupportGroup.group_key == DEFAULT_SUPPORT_GROUP_KEY)
        .first()
    )
    if support_group is None:
        support_group = SupportGroup(
            group_key=DEFAULT_SUPPORT_GROUP_KEY,
            group_name=DEFAULT_SUPPORT_GROUP_NAME,
            description=DEFAULT_SUPPORT_GROUP_DESCRIPTION,
            source_type=SupportGroupSourceType.SEEDED,
            external_ref="",
            sync_status="seeded",
            last_synced_at=datetime.utcnow(),
            is_active=True,
            created_by=None,
        )
        session.add(support_group)
        session.flush()
        return support_group

    changed = False
    if support_group.group_name != DEFAULT_SUPPORT_GROUP_NAME:
        support_group.group_name = DEFAULT_SUPPORT_GROUP_NAME
        changed = True
    if not support_group.is_active:
        support_group.is_active = True
        changed = True
    if support_group.source_type != SupportGroupSourceType.SEEDED:
        support_group.source_type = SupportGroupSourceType.SEEDED
        changed = True
    if changed:
        support_group.last_synced_at = datetime.utcnow()
    return support_group


def preview_directory_groups(query: str) -> list[dict[str, Any]]:
    """Preview group-like entries from the configured LDAP search provider."""
    results = ldap_auth.search_directory_entities(query)
    previews: list[dict[str, Any]] = []
    for item in results or []:
        item_type = str(item.get("type") or "").lower()
        if item_type and item_type != "group":
            continue
        name = (item.get("name") or item.get("display_name") or item.get("cn") or item.get("dn") or "").strip()
        if not name:
            continue
        previews.append(
            {
                "group_key": normalize_group_key(name),
                "group_name": name,
                "description": item.get("description") or "",
                "source_type": SupportGroupSourceType.DIRECTORY,
                "external_ref": item.get("dn") or item.get("id") or name,
            }
        )
    return previews


def import_support_groups(session, groups: list[dict[str, Any]], created_by: Optional[int]) -> list[SupportGroup]:
    """Upsert support groups into the local registry."""
    imported: list[SupportGroup] = []
    now = datetime.utcnow()
    for item in groups:
        group_key = normalize_group_key(item.get("group_key") or item.get("group_name"))
        if not group_key:
            continue
        existing = session.query(SupportGroup).filter(SupportGroup.group_key == group_key).first()
        if existing is None:
            existing = SupportGroup(
                group_key=group_key,
                group_name=item.get("group_name") or group_key,
                description=item.get("description") or "",
                source_type=item.get("source_type") or SupportGroupSourceType.MANUAL,
                external_ref=item.get("external_ref") or "",
                sync_status="imported",
                last_synced_at=now,
                is_active=True,
                created_by=created_by,
            )
            session.add(existing)
        else:
            existing.group_name = item.get("group_name") or existing.group_name
            existing.description = item.get("description") or existing.description
            existing.source_type = item.get("source_type") or existing.source_type
            existing.external_ref = item.get("external_ref") or existing.external_ref
            existing.sync_status = "imported"
            existing.last_synced_at = now
            existing.is_active = True
        imported.append(existing)
    return imported


def _extract_group_cn_from_dn(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    first_segment = raw.split(",", 1)[0].strip()
    if "=" in first_segment:
        _, rhs = first_segment.split("=", 1)
        return rhs.strip()
    return raw


def build_group_match_tokens(group_refs: Optional[Iterable[str]]) -> set[str]:
    """Normalize LDAP group refs/DNs into comparable tokens and DNs."""
    tokens: set[str] = set()
    for group_ref in group_refs or []:
        raw = str(group_ref or "").strip()
        if not raw:
            continue
        tokens.add(raw.lower())
        normalized_cn = normalize_group_key(_extract_group_cn_from_dn(raw))
        if normalized_cn:
            tokens.add(normalized_cn)
        normalized_raw = normalize_group_key(raw)
        if normalized_raw:
            tokens.add(normalized_raw)
    return tokens


def user_matches_support_group(session, support_group_id: Optional[int], group_refs: Optional[Iterable[str]]) -> bool:
    """Return True when the current user's LDAP groups match a support group."""
    if support_group_id is None:
        return False
    support_group = session.query(SupportGroup).filter(SupportGroup.id == support_group_id).first()
    if support_group is None:
        return False
    tokens = build_group_match_tokens(group_refs)
    if not tokens:
        return False

    candidates = {
        (support_group.group_key or "").strip().lower(),
        normalize_group_key(support_group.group_name or ""),
    }
    external_ref = (support_group.external_ref or "").strip().lower()
    if external_ref:
        candidates.add(external_ref)
        normalized_external_ref = normalize_group_key(_extract_group_cn_from_dn(external_ref))
        if normalized_external_ref:
            candidates.add(normalized_external_ref)
    candidates.discard("")
    return bool(tokens.intersection(candidates))


def user_has_project_group_access(session, project, group_refs: Optional[Iterable[str]]) -> bool:
    """Return True when a user's LDAP groups match project-level owner/support groups."""
    if project is None:
        return False
    if user_matches_support_group(session, getattr(project, "owner_group_id", None), group_refs):
        return True

    bindings = (
        session.query(ProjectSupportGroup.support_group_id)
        .filter(ProjectSupportGroup.project_id == project.id)
        .all()
    )
    for row in bindings:
        support_group_id = row.support_group_id if hasattr(row, "support_group_id") else row[0]
        if user_matches_support_group(session, support_group_id, group_refs):
            return True
    return False


def is_project_editor(session, project_id: int, user_id: int) -> bool:
    """Return True when the user is a project member holding an *editor* role
    (currently ``product_member``).

    Editor members carry the same modify rights as the project owner over the
    project and its assets. The owner (``business_owner``) is resolved
    separately via ``Project.owner_id`` at each call site, so this only needs
    to catch the non-owner editor roles.
    """
    pm = (
        session.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        .first()
    )
    return pm is not None and pm.role in ProjectRole.EDITOR_ROLES


def user_project_relayops_member_ids(session, user_id: int) -> set[int]:
    """Project ids where the user is a *project-level* Ops member
    (``ProjectMember.role == 'relayops_member'``).

    This is the per-project role, decoupled from the global ``UserRole``.
    Being a project Ops member grants operational reach over that project's
    issues (see + claim + work), even for a global regular_user.
    """
    rows = (
        session.query(ProjectMember.project_id)
        .filter(
            ProjectMember.user_id == user_id,
            ProjectMember.role == ProjectRole.RELAYOPS_MEMBER,
        )
        .all()
    )
    return {row.project_id if hasattr(row, "project_id") else row[0] for row in rows}


def is_project_relayops_member(session, project_id: int, user_id: int) -> bool:
    """Return True when the user holds the ``relayops_member`` role on this project."""
    pm = (
        session.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        .first()
    )
    return pm is not None and pm.role == ProjectRole.RELAYOPS_MEMBER
