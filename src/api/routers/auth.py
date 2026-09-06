"""Auth routes: /login, /logout, /me."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from api.app_config import ldap_auth
from api.deps.auth import CurrentUser, get_current_user
from api.schema import LoginRequest, TokenResponse
from core.auth.jwt import create_access_token
from core.logging import get_logger
from core.services import user_service as db_user

logger = get_logger(__name__)
router = APIRouter(tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(request: LoginRequest):
    """Authenticate user via LDAP and return JWT token."""
    try:
        if not ldap_auth.login(request.username, request.password):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        normalized_username = request.username.lower()
        display_name = email = ad_groups = None
        user_info = ldap_auth.search_users_and_groups(request.username)
        logger.debug("Login user_info: {}", user_info)
        if user_info:
            display_name = user_info.get("name")
            email = user_info.get("email")
            ad_groups = user_info.get("member_of")
            if ad_groups and isinstance(ad_groups, str):
                ad_groups = [ad_groups]

        user = db_user.get_or_create_user(normalized_username, display_name, ad_groups, email=email)
        access_token = create_access_token(
            username=user.username,
            user_id=user.id,
            role=user.role,
            display_name=display_name,
            email=email,
            ad_groups=ad_groups,
        )
        return TokenResponse(
            access_token=access_token,
            token_type="bearer",
            user_id=user.id,
            username=user.username,
            display_name=display_name,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.opt(exception=True).error("Login error for user {}", request.username)
        raise HTTPException(status_code=401, detail=str(e))


@router.post("/logout")
def logout(current_user: CurrentUser = Depends(get_current_user)):
    ldap_auth.logout()
    return JSONResponse({"success": True, "message": "Logged out"})


@router.get("/me")
def get_me(current_user: CurrentUser = Depends(get_current_user)):
    return JSONResponse(
        {
            "user_id": current_user.user_id,
            "username": current_user.username,
            "role": current_user.role,
            "display_name": current_user.display_name,
            "email": current_user.email,
            "ad_groups": current_user.ad_groups,
            # Projects where the user holds the per-project Ops role. Drives the
            # operational surfaces (My Actions / Open Issues) for global
            # regular_users appointed to a project's Ops role — the global role
            # stays regular_user, and the Open Issues board is scoped to exactly
            # these projects. ``project_relayops_member`` is the convenience boolean.
            "project_relayops_member": bool(_project_relayops_member_ids(current_user.user_id)),
            "project_relayops_member_project_ids": _project_relayops_member_ids(current_user.user_id),
        }
    )


def _project_relayops_member_ids(user_id: int) -> list[int]:
    from core.models.database import get_db
    from core.services.support_group_service import user_project_relayops_member_ids

    session = get_db().get_session()
    try:
        return sorted(user_project_relayops_member_ids(session, user_id))
    finally:
        session.close()
