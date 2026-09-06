"""Auth dependency: get_current_user and CurrentUser (re-export from core)."""

from core.auth.jwt import CurrentUser, get_current_user, get_optional_current_user

__all__ = ["get_current_user", "get_optional_current_user", "CurrentUser"]
