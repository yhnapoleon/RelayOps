"""Project schemas — create, update, response, versioning, handover."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    """Request body for creating a project."""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = ""
    owner_group_id: Optional[int] = None
    # Optional at create time so CML can still be unreachable; the binding
    # resolves and persists cml_project_id when set.
    cml_project_name: Optional[str] = ""
    # When the UI picked from the CML project list, it can hand the id over
    # directly. The service skips the name→id resolve step in that case,
    # which also avoids the "duplicate name" ambiguity error.
    cml_project_id: Optional[str] = None
    # Optional MMP project binding (project_repo_name from MMP). Independent
    # of cml_project_name above.
    mmp_project_id: Optional[str] = ""
    # A project usually maps to one repo, so it carries a single prod-stat URL.
    prod_stat_url: Optional[str] = ""


class ProjectUpdate(BaseModel):
    """Request body for updating a project."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    owner_group_id: Optional[int] = None
    cml_project_name: Optional[str] = None
    cml_project_id: Optional[str] = None
    mmp_project_id: Optional[str] = None
    prod_stat_url: Optional[str] = None


class ProjectResponse(BaseModel):
    """Project response object."""

    id: int
    name: str
    description: Optional[str] = ""
    owner_id: int
    owner_group_id: Optional[int] = None
    owner_group_name: Optional[str] = None
    is_system: bool = False
    owner_username: Optional[str] = None
    owner_display_name: Optional[str] = None
    cml_project_name: Optional[str] = ""
    cml_project_id: Optional[str] = None
    cml_binding_error: Optional[str] = None
    cml_binding_status: str = "unconfigured"  # resolved | pending | error | unconfigured
    mmp_project_id: Optional[str] = ""
    prod_stat_url: Optional[str] = ""
    # Version-control / handover lifecycle (moved up from product level).
    status: str = "draft"
    lifecycle_status: str = "drafting"
    current_draft_version_id: Optional[int] = None
    current_draft_version_status: Optional[str] = None
    current_approved_version_id: Optional[int] = None
    current_approved_version_status: Optional[str] = None
    latest_version_number: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ProjectVersionResponse(BaseModel):
    """Full project-version response for version-aware handover workflows."""

    id: int
    project_id: int
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


class ProjectVersionSubmitRequest(BaseModel):
    """Request body for submitting the current project draft version for review."""

    change_summary: Optional[str] = ""
    requested_version_number: Optional[str] = Field(default=None, min_length=1, max_length=50)


class CmlResourceOption(BaseModel):
    """Lightweight {id, name} pair surfaced by the /cml-jobs lookup. Feeds the
    Job name combobox in the UI."""

    id: str
    name: str


class CmlAppOption(BaseModel):
    """One row in the CML application picker. Carries enough metadata for the
    UI to auto-fill the Application form once a row is picked: ``subdomain``
    is the alternate binding key (CML/app.md §7) and ``serving_url`` is the
    deterministic ``https://<subdomain>.<workspace-host>/`` composition
    (CML/app.md §2) so the user doesn't have to type either by hand."""

    id: str
    name: str
    subdomain: Optional[str] = None
    status: Optional[str] = None
    serving_url: Optional[str] = None


class CmlProjectOption(BaseModel):
    """One row in the CML project picker — name + owner for disambiguation."""

    id: str
    name: str
    owner_username: Optional[str] = None


class MmpModelOption(BaseModel):
    """One row in the MMP model picker — flat (model_name, project) pair.

    The Job form uses this to render a single combobox with ``model_name``
    primary and ``business_name`` / ``project_repo_name`` as secondary
    disambiguators. Picking a row auto-fills BOTH ``Job.mmp_model_id`` (=
    ``model_name``) and ``Job.mmp_project_id`` (= ``project_repo_name``).
    """

    model_name: str
    project_repo_name: str
    business_name: Optional[str] = None
    is_production: bool = False


class MmpProjectOption(BaseModel):
    """One row in the Ops Project's MMP-binding picker.

    Coarser than ``MmpModelOption`` — one row per MMP project (not per
    model). ``model_count`` lets the UI show "(3 models)" so users can
    estimate scope before binding.
    """

    project_repo_name: str
    business_name: Optional[str] = None
    model_count: int = 0
    owner_email: Optional[str] = None


class MmpUrlResolveResponse(BaseModel):
    """Result of resolving a pasted MMP web link into a binding.

    Given an MMP URL like ``…/project/160/projectDetails`` the backend pulls
    project 160 from the MMP API and returns enough to auto-fill the Job/
    Project MMP binding: ``project_repo_name`` (= ``mmp_project_id``) and the
    model list. ``suggested_model_name`` is set only when exactly one
    production model exists (so the Job form can fill ``mmp_model_id`` too);
    otherwise the UI lets the user pick from ``models``.
    """

    project_id: Optional[int] = None
    project_repo_name: Optional[str] = None
    business_name: Optional[str] = None
    models: List[MmpModelOption] = Field(default_factory=list)
    suggested_model_name: Optional[str] = None
    error: Optional[str] = None


class CmlProjectSearchResponse(BaseModel):
    """First-page slice of CML projects visible to the current Ops API key.

    ``has_more`` is true iff CML returned a ``next_page_token`` — the UI
    uses it to switch the picker from "show all" to "type-to-search".
    """

    items: List[CmlProjectOption]
    has_more: bool
