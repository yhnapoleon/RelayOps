"""Schedule schemas — create, update, response."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ScheduleCreate(BaseModel):
    """Request body for creating a schedule entry."""

    start_time: datetime = Field(..., description="Start time of the duty")
    end_time: datetime = Field(..., description="End time of the duty")
    assignee_id: int = Field(..., description="User ID of the Ops Member on duty")
    duty_role: str = Field(default="Primary", description="Duty role (e.g., Primary, Secondary)")
    note: Optional[str] = Field(default="", max_length=1000)


class ScheduleUpdate(BaseModel):
    """Request body for updating a schedule entry."""

    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    assignee_id: Optional[int] = None
    duty_role: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=1000)


class ScheduleResponse(BaseModel):
    """Schedule response object."""

    id: int
    start_time: datetime
    end_time: datetime
    assignee_id: int
    assignee_username: Optional[str] = None
    assignee_display_name: Optional[str] = None
    created_by: int
    duty_role: str
    note: Optional[str] = ""
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
