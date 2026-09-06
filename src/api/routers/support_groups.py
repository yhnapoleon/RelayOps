"""Support-group registry and import-preview routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.deps.rbac import BusinessOwnerOrAdmin
from api.schema import (
    SupportGroupCreate,
    SupportGroupImportPreviewItemResponse,
    SupportGroupImportRequest,
    SupportGroupResponse,
    SupportGroupUpdate,
)
from core.exceptions import ConflictError, NotFoundError, ValidationError
from core.logging import get_logger
from core.models.entities import SupportGroup, SupportGroupSourceType
from core.services.audit_service import log_audit, serialize_support_group
from core.services.support_group_service import (
    SEEDED_SUPPORT_GROUPS,
    import_support_groups,
    normalize_group_key,
    preview_directory_groups,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/support-groups", tags=["support-groups"])


def _validate_source_type(source_type: str) -> None:
    if source_type not in SupportGroupSourceType.ALL:
        raise ValidationError(f"Invalid source_type. Must be one of: {SupportGroupSourceType.ALL}")


@router.get("", response_model=list[SupportGroupResponse])
def list_support_groups(
    search: str | None = Query(None, description="Search by group key or name"),
    active_only: bool = Query(True, description="When true, only returns active groups"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    query = session.query(SupportGroup)
    if active_only:
        query = query.filter(SupportGroup.is_active.is_(True))
    if search:
        keyword = f"%{search.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(SupportGroup.group_key).like(keyword),
                func.lower(SupportGroup.group_name).like(keyword),
            )
        )
    return query.order_by(SupportGroup.group_name.asc(), SupportGroup.group_key.asc()).all()


@router.post("", response_model=SupportGroupResponse, status_code=status.HTTP_201_CREATED)
def create_support_group(
    body: SupportGroupCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    _validate_source_type(body.source_type)
    group_key = normalize_group_key(body.group_key or body.group_name)
    if not group_key:
        raise ValidationError("group_key/group_name did not produce a valid normalized key")
    if session.query(SupportGroup).filter(SupportGroup.group_key == group_key).first():
        raise ConflictError("A support group with this key already exists")

    sg = SupportGroup(
        group_key=group_key,
        group_name=body.group_name.strip(),
        description=body.description or "",
        source_type=body.source_type,
        external_ref=body.external_ref or "",
        is_active=body.is_active,
        created_by=current_user.user_id,
    )
    session.add(sg)
    session.flush()
    session.refresh(sg)
    log_audit(
        user_id=current_user.user_id,
        action="create",
        entity_type="support_group",
        entity_id=sg.id,
        new_value=serialize_support_group(sg),
    )
    return sg


@router.put("/{support_group_id}", response_model=SupportGroupResponse)
def update_support_group(
    support_group_id: int,
    body: SupportGroupUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    if body.source_type is not None:
        _validate_source_type(body.source_type)
    sg = session.query(SupportGroup).filter(SupportGroup.id == support_group_id).first()
    if sg is None:
        raise NotFoundError("Support group not found")

    old_val = serialize_support_group(sg)
    if body.group_key is not None or body.group_name is not None:
        new_key = normalize_group_key(body.group_key or body.group_name or sg.group_name)
        if not new_key:
            raise ValidationError("group_key/group_name did not produce a valid normalized key")
        conflict = (
            session.query(SupportGroup)
            .filter(SupportGroup.group_key == new_key, SupportGroup.id != support_group_id)
            .first()
        )
        if conflict is not None:
            raise ConflictError("A support group with this key already exists")
        sg.group_key = new_key
    if body.group_name is not None:
        sg.group_name = body.group_name.strip()
    if body.description is not None:
        sg.description = body.description
    if body.source_type is not None:
        sg.source_type = body.source_type
    if body.external_ref is not None:
        sg.external_ref = body.external_ref
    if body.sync_status is not None:
        sg.sync_status = body.sync_status
    if body.is_active is not None:
        sg.is_active = body.is_active
    session.flush()
    session.refresh(sg)
    log_audit(
        user_id=current_user.user_id,
        action="update",
        entity_type="support_group",
        entity_id=sg.id,
        old_value=old_val,
        new_value=serialize_support_group(sg),
    )
    return sg


@router.get("/import-preview", response_model=list[SupportGroupImportPreviewItemResponse])
def import_preview(
    source_type: str = Query(..., description="manual/seeded/directory"),
    query: str = Query("", description="Optional search text for directory preview"),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    _validate_source_type(source_type)
    if source_type == SupportGroupSourceType.SEEDED:
        return [SupportGroupImportPreviewItemResponse(**item) for item in SEEDED_SUPPORT_GROUPS]
    if source_type == SupportGroupSourceType.DIRECTORY:
        if len(query.strip()) < 2:
            return []
        return [SupportGroupImportPreviewItemResponse(**item) for item in preview_directory_groups(query)]
    return []


@router.post("/import", response_model=list[SupportGroupResponse])
def import_groups(
    body: SupportGroupImportRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    _validate_source_type(body.source_type)
    preview_items = body.items
    if not preview_items:
        if body.source_type == SupportGroupSourceType.SEEDED:
            preview_items = [SupportGroupImportPreviewItemResponse(**item) for item in SEEDED_SUPPORT_GROUPS]
        elif body.source_type == SupportGroupSourceType.DIRECTORY and len((body.query or "").strip()) >= 2:
            preview_items = [
                SupportGroupImportPreviewItemResponse(**item)
                for item in preview_directory_groups(body.query or "")
            ]

    imported = import_support_groups(
        session,
        [item.model_dump() for item in preview_items],
        created_by=current_user.user_id,
    )
    session.flush()
    for item in imported:
        session.refresh(item)

    for sg in imported:
        log_audit(
            user_id=current_user.user_id,
            action="import",
            entity_type="support_group",
            entity_id=sg.id,
            new_value=serialize_support_group(sg),
        )
    logger.info("Imported {} support group(s) via source_type={}", len(imported), body.source_type)
    return imported
