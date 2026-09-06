"""Support group schemas — create, update, response, import, binding."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class SupportGroupCreate(BaseModel):
    """Create a structured support-group registry entry."""

    group_key: Optional[str] = Field(default=None, min_length=1, max_length=255)
    group_name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = ""
    source_type: str = "manual"
    external_ref: Optional[str] = ""
    is_active: bool = True


class SupportGroupUpdate(BaseModel):
    """Update a structured support-group registry entry."""

    group_key: Optional[str] = Field(default=None, min_length=1, max_length=255)
    group_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    source_type: Optional[str] = None
    external_ref: Optional[str] = None
    sync_status: Optional[str] = None
    is_active: Optional[bool] = None


class SupportGroupResponse(BaseModel):
    """Support-group registry response."""

    id: int
    group_key: str
    group_name: str
    description: Optional[str] = ""
    source_type: str
    external_ref: Optional[str] = ""
    sync_status: Optional[str] = ""
    last_synced_at: Optional[datetime] = None
    is_active: bool = True
    created_by: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SupportGroupImportPreviewItemResponse(BaseModel):
    """Preview item for a seeded/directory support-group import."""

    group_key: str
    group_name: str
    description: Optional[str] = ""
    source_type: str
    external_ref: Optional[str] = ""


class SupportGroupImportRequest(BaseModel):
    """Import support groups from a seeded or directory-backed source."""

    source_type: str = Field(..., min_length=1, max_length=50)
    query: Optional[str] = ""
    items: List[SupportGroupImportPreviewItemResponse] = Field(default_factory=list)


class ProjectSupportGroupBindRequest(BaseModel):
    """Bind a support group to a project."""

    support_group_id: int


class ProjectOwnerGroupUpdateRequest(BaseModel):
    """Update the owner-group binding for a project."""

    owner_group_id: Optional[int] = None


class ProjectSupportGroupResponse(BaseModel):
    """Project-to-support-group binding."""

    id: int
    project_id: int
    support_group_id: int
    support_group_name: str
    created_by: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}
