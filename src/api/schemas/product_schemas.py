"""Product schemas — create, update, response, versioning, handover."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    """Request body for creating a product under a project."""

    name: str = Field(..., min_length=1, max_length=255)


class ProductUpdate(BaseModel):
    """Request body for updating a product."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)


class ProductResponse(BaseModel):
    """Product response object.

    Version control and the submit/approve lifecycle now live on the project,
    so a product carries only a lightweight ``status`` used for monitoring
    gating (active assets are checked unless the product is archived).
    """

    id: int
    project_id: int
    name: str
    status: str = "draft"
    is_system: bool = False
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ProductVersionSummaryResponse(BaseModel):
    """Lightweight product-version reference embedded in other payloads."""

    id: int
    version_number: str
    version_status: str
    submitted_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    model_config = {"from_attributes": True}


class ProductVersionResponse(BaseModel):
    """Full product-version response for version-aware handover workflows."""

    id: int
    product_id: int
    version_number: str
    version_status: str
    change_summary: Optional[str] = ""
    snapshot_json: Optional[Dict[str, Any]] = None
    completeness_summary_json: Optional[Dict[str, Any]] = None
    submitted_by: Optional[int] = None
    submitted_at: Optional[datetime] = None
    reviewed_by: Optional[int] = None
    reviewed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    derived_from_version_id: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ProductVersionSubmitRequest(BaseModel):
    """Request body for submitting the current draft version for review."""

    change_summary: Optional[str] = ""
    requested_version_number: Optional[str] = Field(default=None, min_length=1, max_length=50)


class EmailTemplateApplyRequest(BaseModel):
    """Apply one scenario's owner-email template across an asset or product.

    ``template`` is the canonical 4-field owner-email shape
    (``to`` / ``cc`` / ``subject`` / ``body``); unknown keys are ignored and
    missing ones default to "". ``mode`` chooses between filling only
    scenarios that have no template yet (``fill_empty`` — "set as default")
    and overwriting every target scenario (``overwrite`` — "replace all").
    ``scope`` selects the blast radius; ``job_id`` / ``application_id`` are
    required only for the matching asset-level scope.
    """

    template: Dict[str, Any] = Field(default_factory=dict)
    mode: str = Field("fill_empty")  # fill_empty | overwrite
    scope: str = Field("product")  # product | job | app
    job_id: Optional[int] = None
    application_id: Optional[int] = None


class EmailTemplateApplyResponse(BaseModel):
    """Result of an :class:`EmailTemplateApplyRequest`."""

    updated: int = 0
    scope: str = "product"
    mode: str = "fill_empty"


class HandoverRequest(BaseModel):
    """Request body for initiating a handover."""

    pass


class HandoverApproveRequest(BaseModel):
    """Request body for approving a handover."""

    pass


class HandoverRejectRequest(BaseModel):
    """Request body for rejecting a handover."""

    rejection_reason: str = Field(..., min_length=1, max_length=2000)


class HandoverCompletenessItemResponse(BaseModel):
    """Blocking item returned by handover completeness checks.

    ``parent_entity_*`` and ``field_code`` let the UI navigate from a blocking
    item directly into the owning asset's edit form and highlight the empty
    field in red. They're optional so legacy backends still validate.
    """

    code: str
    severity: str
    entity_type: str
    entity_id: Optional[int] = None
    entity_label: Optional[str] = None
    parent_entity_type: Optional[str] = None
    parent_entity_id: Optional[int] = None
    parent_entity_label: Optional[str] = None
    field_code: Optional[str] = None
    message: str


class HandoverCompletenessResponse(BaseModel):
    """Structured completeness result for versioned handover submission."""

    is_complete: bool = False
    blocking_items: List[HandoverCompletenessItemResponse] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)
