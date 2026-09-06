"""Auth schemas — login and token responses."""

from typing import Optional

from pydantic import BaseModel


class LoginRequest(BaseModel):
    """User login credentials."""

    username: str
    password: str


class TokenResponse(BaseModel):
    """JWT token response after successful login."""

    access_token: str
    token_type: str
    user_id: int
    username: str
    display_name: Optional[str] = None
