"""JWT sessions backed by current local account permissions."""
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from core.config import get_config

_cfg = get_config()
JWT_SECRET_KEY = _cfg.jwt_secret_key
JWT_ALGORITHM = 'HS256'
JWT_EXPIRE_MINUTES = _cfg.jwt_expire_minutes
security = HTTPBearer()


class TokenPayload(BaseModel):
    sub: str
    user_id: int
    exp: datetime
    token_version: int = 0


class CurrentUser(BaseModel):
    username: str
    user_id: int
    role: str = 'regular_user'
    display_name: Optional[str] = None
    email: Optional[str] = None
    groups: list[str] = Field(default_factory=list)


def create_access_token(username: str, user_id: int, role: str = 'regular_user',
                        display_name: Optional[str] = None, email: Optional[str] = None,
                        expires_delta: Optional[timedelta] = None, token_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    payload = {'sub': username, 'user_id': user_id, 'role': role,
               'exp': now + (expires_delta if expires_delta is not None else timedelta(minutes=JWT_EXPIRE_MINUTES)),
               'iat': now, 'token_version': token_version}
    if display_name:
        payload['display_name'] = display_name
    if email:
        payload['email'] = email
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> Optional[TokenPayload]:
    try:
        data = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM],
                          options={'require': ['sub', 'user_id', 'exp', 'token_version']})
        return TokenPayload(**data)
    except (jwt.InvalidTokenError, ValueError, TypeError):
        return None


def _current_account(payload: TokenPayload) -> Optional[CurrentUser]:
    from core.models.database import get_db
    from core.models.user import User, UserRole, normalize_role
    with get_db().get_session() as session:
        user = session.query(User).filter(User.id == payload.user_id, User.username == payload.sub).first()
        if user is None or not user.password_hash or (user.token_version or 0) != payload.token_version:
            return None
        role = normalize_role(user.role)
        if role not in UserRole.ALL:
            return None
        return CurrentUser(username=user.username, user_id=user.id, role=role,
                           display_name=user.display_name, email=user.email, groups=user.group_keys or [])


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> CurrentUser:
    payload = verify_token(credentials.credentials)
    account = _current_account(payload) if payload else None
    if account is None:
        raise HTTPException(status_code=401, detail='Could not validate credentials',
                            headers={'WWW-Authenticate': 'Bearer'})
    return account


def get_optional_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False))) -> Optional[CurrentUser]:
    if credentials is None:
        return None
    payload = verify_token(credentials.credentials)
    return _current_account(payload) if payload else None
