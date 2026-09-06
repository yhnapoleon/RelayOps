"""Audit log schemas — response, issue audit summary, timeline."""

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel


class AuditLogResponse(BaseModel):
    """Audit log entry response object."""

    id: int
    user_id: int
    action: str
    entity_type: str
    entity_id: int
    old_value: Optional[Dict[str, Any]] = None
    new_value: Optional[Dict[str, Any]] = None
    timestamp: datetime
    # Enriched fields
    username: Optional[str] = None
    display_name: Optional[str] = None

    model_config = {"from_attributes": True}


class IssueAuditSummaryResponse(BaseModel):
    """Issue-centric audit summary for the main audit view."""

    issue_id: int
    issue_type: str
    issue_status: str
    issue_title: str
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    project_is_system: bool = False
    product_id: Optional[int] = None
    product_version_id: Optional[int] = None
    product_version_number: Optional[str] = None
    product_version_status: Optional[str] = None
    product_name: Optional[str] = None
    product_is_system: bool = False
    support_group_id: Optional[int] = None
    support_group_name: Optional[str] = None
    owner_group_id: Optional[int] = None
    owner_group_name: Optional[str] = None
    assignee_id: Optional[int] = None
    assignee_username: Optional[str] = None
    assignee_display_name: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    latest_event_at: datetime
    latest_event_label: str
    event_count: int = 0
    notification_count: int = 0


class IssueAuditTimelineEventResponse(BaseModel):
    """Single event in an issue audit timeline."""

    audit_log_id: int
    timestamp: datetime
    action: str
    entity_type: str
    label: str
    summary: str
    actor_user_id: int
    actor_username: Optional[str] = None
    actor_display_name: Optional[str] = None
    recipient_user_id: Optional[int] = None
    recipient_username: Optional[str] = None
    recipient_display_name: Optional[str] = None
    is_notification: bool = False
    details: Optional[Dict[str, Any]] = None
