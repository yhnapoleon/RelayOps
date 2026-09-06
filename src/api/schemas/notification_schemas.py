"""Notification schemas — response."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class NotificationResponse(BaseModel):
    """Notification response object."""

    id: int
    user_id: int
    title: str
    message: Optional[str] = ""
    type: str
    is_read: int = 0
    related_entity_type: Optional[str] = None
    related_entity_id: Optional[int] = None
    issue_id: Optional[int] = None
    issue_type: Optional[str] = None
    issue_title: Optional[str] = None
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    project_is_system: bool = False
    product_id: Optional[int] = None
    product_name: Optional[str] = None
    product_is_system: bool = False
    support_group_id: Optional[int] = None
    support_group_name: Optional[str] = None
    owner_group_id: Optional[int] = None
    owner_group_name: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}
