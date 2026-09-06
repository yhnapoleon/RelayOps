"""
API Key authentication dependency for external system endpoints.

External systems authenticate using an API Key in the X-API-Key header
instead of JWT tokens. Used for service-to-service callers (e.g. mock
MMP drift posts) that don't carry a user JWT.

Usage:
    from api.deps.api_key import verify_api_key

    @router.post("/api/some-service-endpoint")
    async def handler(api_key = Depends(verify_api_key), ...):
        ...
"""

import hashlib
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from core.logging import get_logger
from core.models.database import get_db
from core.models.entities import ApiKey

logger = get_logger(__name__)


def _hash_key(key: str) -> str:
    """Hash an API key using SHA-256."""
    return hashlib.sha256(key.encode()).hexdigest()


async def verify_api_key(x_api_key: Optional[str] = Header(default=None)) -> ApiKey:
    """
    Verify the X-API-Key header against stored API keys.

    Returns the ApiKey record if valid, raises 401 otherwise.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )

    key_hash = _hash_key(x_api_key)

    db = get_db()
    session = db.get_session()
    try:
        api_key = session.query(ApiKey).filter(
            ApiKey.key_hash == key_hash,
            ApiKey.is_active == 1,
        ).first()

        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key",
            )

        return api_key
    finally:
        session.close()
