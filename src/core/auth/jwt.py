"""
JWT Token utilities for authentication.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from core.config import get_config
from core.logging import get_logger

logger = get_logger(__name__)

_cfg = get_config()
JWT_SECRET_KEY = _cfg.jwt_secret_key
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = _cfg.jwt_expire_minutes

# Security scheme for FastAPI
security = HTTPBearer()


class TokenPayload(BaseModel):
    """JWT token payload structure."""

    sub: str  # username
    user_id: int
    exp: datetime
    role: Optional[str] = None
    display_name: Optional[str] = None
    email: Optional[str] = None
    ad_groups: Optional[List[str]] = None


class CurrentUser(BaseModel):
    """Current authenticated user info."""

    username: str
    user_id: int
    role: str = "regular_user"
    display_name: Optional[str] = None
    email: Optional[str] = None
    ad_groups: Optional[List[str]] = None


def create_access_token(
    username: str,
    user_id: int,
    role: str = "regular_user",
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    ad_groups: Optional[List[str]] = None,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Create a JWT access token.

    Args:
        username: The username to encode in the token
        user_id: The user's database ID
        role: The user's role (admin / business_owner / relayops_member)
        display_name: The user's display name from LDAP
        email: The user's email from LDAP
        ad_groups: List of AD group DNs the user belongs to
        expires_delta: Optional custom expiration time

    Returns:
        Encoded JWT token string
    """
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)

    payload = {
        "sub": username,
        "user_id": user_id,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }

    # Only include optional fields if they have values
    if display_name:
        payload["display_name"] = display_name
    if email:
        payload["email"] = email
    if ad_groups:
        payload["ad_groups"] = ad_groups

    logger.debug("Created access token for user: {} role={}", username, role)
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> Optional[TokenPayload]:
    """
    Verify and decode a JWT token.

    Args:
        token: The JWT token string

    Returns:
        TokenPayload if valid, None otherwise
    """
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        # Translate legacy 'business_owner' tokens to 'regular_user' on
        # decode — pre-rename JWTs are still valid until they expire.
        from core.models.user import normalize_role
        return TokenPayload(
            sub=payload["sub"],
            user_id=payload["user_id"],
            exp=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
            role=normalize_role(payload.get("role", "regular_user")),
            display_name=payload.get("display_name"),
            email=payload.get("email"),
            ad_groups=payload.get("ad_groups"),
        )
    except jwt.ExpiredSignatureError:
        logger.warning("Token verification failed: expired signature")
        return None
    except jwt.InvalidTokenError:
        logger.warning("Token verification failed: invalid token")
        return None


def _jwt_user_row_exists(user_id: int) -> bool:
    """Verify the JWT's ``user_id`` still maps to a live ``users`` row.

    The JWT secret is persistent (config/env), so tokens minted before a
    DB rebuild remain signature-valid afterwards — but the user row they
    point at is gone, and any write that uses ``user_id`` as a FK ends
    up as an opaque 500. Surfacing this as 401 lets the frontend's auth
    interceptor kick the user back to login (which re-runs
    ``get_or_create_user`` and gets a fresh, valid id).
    """
    from core.models.database import get_db
    from core.models.user import User

    session = get_db().get_session()
    try:
        return (
            session.query(User.id).filter(User.id == user_id).first() is not None
        )
    finally:
        session.close()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> CurrentUser:
    """
    FastAPI dependency to get the current authenticated user from JWT token.

    Args:
        credentials: HTTP Bearer token from Authorization header

    Returns:
        CurrentUser with username, user_id, display_name, and ad_groups

    Raises:
        HTTPException: If token is invalid, expired, or references a
            user row that no longer exists (typically a stale token from
            before a DB rebuild).
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token = credentials.credentials
    payload = verify_token(token)

    if payload is None:
        raise credentials_exception

    if not _jwt_user_row_exists(payload.user_id):
        logger.warning(
            "Rejecting stale JWT for user_id={} (username={}): users row missing",
            payload.user_id, payload.sub,
        )
        raise credentials_exception

    return CurrentUser(
        username=payload.sub,
        user_id=payload.user_id,
        role=payload.role or "regular_user",
        display_name=payload.display_name,
        email=payload.email,
        ad_groups=payload.ad_groups,
    )


def get_optional_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
) -> Optional[CurrentUser]:
    """
    FastAPI dependency to optionally get the current user.
    Returns None if no valid token is provided instead of raising an exception.
    """
    if credentials is None:
        return None

    payload = verify_token(credentials.credentials)
    if payload is None:
        return None

    if not _jwt_user_row_exists(payload.user_id):
        return None

    return CurrentUser(
        username=payload.sub,
        user_id=payload.user_id,
        role=payload.role or "regular_user",
        display_name=payload.display_name,
        email=payload.email,
        ad_groups=payload.ad_groups,
    )
