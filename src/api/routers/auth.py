"""Local password login, session logout, profile, and password changes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.schema import LoginRequest, TokenResponse
from core.auth.auth import DUMMY_PASSWORD_HASH, hash_password, verify_password
from core.auth.jwt import create_access_token
from core.models.user import User

router = APIRouter(tags=['auth'])


@router.post('/login', response_model=TokenResponse)
def login(request: LoginRequest, session: Session = Depends(get_session)):
    username = request.username.strip().lower()
    user = session.query(User).filter(User.username == username).first()
    encoded = user.password_hash if user is not None and user.password_hash else DUMMY_PASSWORD_HASH
    valid = verify_password(request.password, encoded)
    if not valid or user is None or not user.password_hash:
        raise HTTPException(status_code=401, detail='Invalid credentials')
    token = create_access_token(username=user.username, user_id=user.id, role=user.role,
                                display_name=user.display_name, email=user.email,
                                token_version=user.token_version or 0)
    return TokenResponse(access_token=token, token_type='bearer', user_id=user.id,
                         username=user.username, display_name=user.display_name)


@router.post('/logout')
def logout(current_user: CurrentUser = Depends(get_current_user), session: Session = Depends(get_session)):
    session.query(User).filter(User.id == current_user.user_id).update({User.token_version: User.token_version + 1})
    session.commit()
    return {'success': True, 'message': 'Logged out of all sessions'}


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


@router.post('/auth/password')
def change_password(body: PasswordChange, current_user: CurrentUser = Depends(get_current_user),
                    session: Session = Depends(get_session)):
    user = session.query(User).filter(User.id == current_user.user_id).one()
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail='Current password is incorrect')
    user.password_hash = hash_password(body.new_password)
    user.token_version = User.token_version + 1
    session.commit()
    return {'success': True, 'message': 'Password changed; sign in again'}


@router.get('/me')
def get_me(current_user: CurrentUser = Depends(get_current_user)):
    projects = _project_relayops_member_ids(current_user.user_id)
    return {**current_user.model_dump(), 'project_relayops_member': bool(projects),
            'project_relayops_member_project_ids': projects}


def _project_relayops_member_ids(user_id: int) -> list[int]:
    from core.models.database import get_db
    from core.services.support_group_service import user_project_relayops_member_ids
    with get_db().get_session() as session:
        return sorted(user_project_relayops_member_ids(session, user_id))
