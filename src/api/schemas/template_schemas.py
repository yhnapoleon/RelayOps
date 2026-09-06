"""Scenario-template schemas — create, update, response.

A scenario template stores the raw authoring fields of a Job/App scenario
(verbatim from the frontend form) so it can be saved, named, and one-click
refilled into a new scenario. ``payload`` is intentionally a free-form dict
so the server doesn't need to track the scenario form's field list.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ScenarioTemplateCreate(BaseModel):
    """Request body for saving a new scenario template."""

    asset_kind: str = Field(..., pattern="^(job|app)$")
    name: str = Field(..., min_length=1, max_length=255)
    scenario_type: str = Field(default="", max_length=50)
    description: str = Field(default="", max_length=2000)
    payload: Dict[str, Any] = Field(default_factory=dict)


class ScenarioTemplateUpdate(BaseModel):
    """Request body for updating a scenario template (partial)."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    scenario_type: Optional[str] = Field(default=None, max_length=50)
    description: Optional[str] = Field(default=None, max_length=2000)
    payload: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class ScenarioTemplateResponse(BaseModel):
    """Scenario template response object."""

    id: int
    asset_kind: str
    name: str
    scenario_type: Optional[str] = ""
    description: Optional[str] = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_by: Optional[int] = None
    is_active: bool = True
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
