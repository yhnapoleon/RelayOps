"""Issue schemas — create, update, response, action workspace, runbook, preferences."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class IssueCreateRequest(BaseModel):
    """Request body for creating a new issue."""

    type: str = Field(..., description="Issue type (e.g., job_not_triggered, job_failed, app_offline)")
    title: str = Field(..., min_length=1, max_length=512)
    description: Optional[str] = ""
    product_id: Optional[int] = None
    job_id: Optional[int] = None
    app_id: Optional[int] = None
    support_group_id: Optional[int] = None
    assignee_id: Optional[int] = Field(
        default=None,
        description="Explicit assignee. If omitted, auto-assigns to on-duty Ops Member.",
    )


class IssueResponse(BaseModel):
    """Issue response object with enriched assignee/creator info."""

    id: int
    type: str
    status: str
    title: str
    description: Optional[str] = ""
    product_id: Optional[int] = None
    product_version_id: Optional[int] = None
    product_version_number: Optional[str] = None
    product_version_status: Optional[str] = None
    project_version_id: Optional[int] = None
    project_version_number: Optional[str] = None
    project_version_status: Optional[str] = None
    job_id: Optional[int] = None
    job_name: Optional[str] = None
    app_id: Optional[int] = None
    app_name: Optional[str] = None
    owner_contact: Optional[str] = ""
    created_by: int
    assignee_id: Optional[int] = None
    support_group_id: Optional[int] = None
    support_group_name: Optional[str] = ""
    owner_group_id: Optional[int] = None
    owner_group_name: Optional[str] = ""
    assigned_via: Optional[str] = ""
    resolution_description: Optional[str] = None
    rejection_reason: Optional[str] = None
    selected_scenario_type: Optional[str] = None
    selected_scenario_name: Optional[str] = None
    external_url: Optional[str] = None
    action_summary_json: Optional[Dict[str, Any]] = None
    resolution_summary_json: Optional[Dict[str, Any]] = None
    sla_deadline: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    # Enriched fields
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    project_is_system: bool = False
    product_name: Optional[str] = None
    product_is_system: bool = False
    assignee_username: Optional[str] = None
    assignee_display_name: Optional[str] = None
    created_by_username: Optional[str] = None
    created_by_display_name: Optional[str] = None
    project_owner_id: Optional[int] = None
    project_owner_username: Optional[str] = None
    project_owner_display_name: Optional[str] = None
    # Production-statistics deep link configured on the owning project, surfaced
    # so the workbench/detail surfaces can offer a one-click prodstat link.
    project_prod_stat_url: Optional[str] = None

    model_config = {"from_attributes": True}


class IssueAssigneeCandidateResponse(BaseModel):
    """Assignable issue user for admin reassignment."""

    user_id: int
    username: str
    display_name: Optional[str] = None
    role: str

    model_config = {"from_attributes": True}


class IssueUpdateRequest(BaseModel):
    """Request body for updating an issue (e.g., changing status)."""

    status: Optional[str] = None
    assignee_id: Optional[int] = None
    resolution_description: Optional[str] = None


class IssueRunbookScenarioResponse(BaseModel):
    """Generic runbook scenario used by the action workspace."""

    scenario_id: int
    entity_type: str
    scenario_type: str
    scenario_name: str
    condition_description: Optional[str] = ""
    detection_source: Optional[str] = ""
    diagnostic_steps: List[str] = Field(default_factory=list)
    action_steps: List[str] = Field(default_factory=list)
    verification_steps: List[str] = Field(default_factory=list)
    escalation_target: Optional[str] = ""
    fallback_owner_type: Optional[str] = None
    threshold_operator: Optional[str] = ""
    threshold_value: Optional[float] = None
    threshold_feature_list: List[str] = Field(default_factory=list)
    email_template: Optional[Dict[str, Any]] = None
    # Surfaced so the workbench scenario picker can flag wrongly-placed or
    # disabled runbooks instead of silently dropping them: inactive scenarios
    # render with an "Inactive" badge, NA ones with a "Don't apply" badge, and
    # the operator can still select any of them.
    is_active: bool = True
    is_not_applicable: bool = False


class IssueActionWorkspaceResponse(BaseModel):
    """Runbook workspace payload for the My Actions execution surface."""

    issue: "IssueResponse"
    entity_type: Optional[str] = None
    entity_label: Optional[str] = None
    support_group_id: Optional[int] = None
    support_group_name: Optional[str] = ""
    support_group: Optional[str] = ""
    owner_group_id: Optional[int] = None
    owner_group_name: Optional[str] = ""
    owner_contact: Optional[str] = ""
    cml_app_type: Optional[str] = None
    restart_supported: Optional[bool] = None
    restart_summary: Optional[str] = None
    recommended_scenario_id: Optional[int] = None
    recommended_scenario_name: Optional[str] = None
    recommended_scenario_type: Optional[str] = None
    scenarios: List[IssueRunbookScenarioResponse] = Field(default_factory=list)


class IssueActionRequest(BaseModel):
    """Structured action emitted from the execution workspace."""

    action: str = Field(..., min_length=1, max_length=50)
    scenario_id: Optional[int] = None
    step_title: Optional[str] = None
    notes: Optional[str] = None
    verification_notes: Optional[str] = None
    escalation_target: Optional[str] = None
    actions_taken: List[str] = Field(default_factory=list)
    final_conclusion: Optional[str] = None


class IssuePreferenceUpdateRequest(BaseModel):
    """Replace the current user's issue-type preferences."""

    issue_types: List[str] = Field(default_factory=list)


class IssuePreferenceResponse(BaseModel):
    """Issue-type preference entry for the current user."""

    issue_type: str

    model_config = {"from_attributes": True}


class ClaimIssuesRequest(BaseModel):
    """Self-assign one or more open issues to the acting user."""

    issue_ids: List[int] = Field(..., min_length=1)


class ClaimSkippedItem(BaseModel):
    """An issue that could not be claimed, with the reason why."""

    issue_id: int
    reason: str


class ClaimIssuesResponse(BaseModel):
    """Result of a (possibly bulk) claim: successfully claimed issues plus
    any that were skipped (not found / already resolved / not claimable)."""

    claimed: List[IssueResponse] = Field(default_factory=list)
    skipped: List[ClaimSkippedItem] = Field(default_factory=list)
