"""Auth schemas — login and token responses."""

from typing import Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """User login credentials."""

    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class TokenResponse(BaseModel):
    """JWT token response after successful login."""

    access_token: str
    token_type: str
    user_id: int
    username: str
    display_name: Optional[str] = None
