"""API Key management — create, list, revoke. Admin only."""

import hashlib
import secrets
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser
from api.deps.db import get_session
from api.deps.rbac import AdminOnly
from api.schema import ApiKeyCreateRequest, ApiKeyCreateResponse, ApiKeyResponse
from core.exceptions import NotFoundError
from core.logging import get_logger
from core.models.entities import ApiKey
from core.services.audit_service import log_audit

logger = get_logger(__name__)
router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


def _generate_api_key() -> str:
    return f"relayops_{secrets.token_hex(24)}"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@router.post("", response_model=ApiKeyCreateResponse, status_code=201)
def create_api_key(
    body: ApiKeyCreateRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(AdminOnly),
):
    plaintext_key = _generate_api_key()
    api_key = ApiKey(
        name=body.name,
        key_hash=_hash_key(plaintext_key),
        key_prefix=plaintext_key[:8],
        created_by=current_user.user_id,
        is_active=1,
    )
    session.add(api_key)
    session.flush()
    session.refresh(api_key)
    logger.info("API key created: id={} name={} prefix={}", api_key.id, body.name, api_key.key_prefix)
    log_audit(
        user_id=current_user.user_id,
        action="create",
        entity_type="api_key",
        entity_id=api_key.id,
        new_value={"name": body.name, "key_prefix": api_key.key_prefix},
    )
    return ApiKeyCreateResponse(
        id=api_key.id,
        name=api_key.name,
        key=plaintext_key,
        key_prefix=api_key.key_prefix,
        created_at=api_key.created_at,
    )


@router.get("", response_model=List[ApiKeyResponse])
def list_api_keys(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(AdminOnly),
):
    keys = session.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    return [
        ApiKeyResponse(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            is_active=k.is_active,
            created_by=k.created_by,
            created_at=k.created_at,
        )
        for k in keys
    ]


@router.delete("/{key_id}", status_code=204)
def revoke_api_key(
    key_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(AdminOnly),
):
    api_key = session.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not api_key:
        raise NotFoundError("API key not found")
    old_active = api_key.is_active
    api_key.is_active = 0
    logger.info("API key revoked: id={} name={}", key_id, api_key.name)
    log_audit(
        user_id=current_user.user_id,
        action="delete",
        entity_type="api_key",
        entity_id=key_id,
        old_value={"name": api_key.name, "key_prefix": api_key.key_prefix, "is_active": old_active},
        new_value={"is_active": 0},
    )
