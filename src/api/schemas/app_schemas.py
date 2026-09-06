"""Application schemas — create, update, response, verification, recovery scenarios."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


_CML_APP_TYPES = ("fastapi", "runtime", "ray", "generic")


class AppCreate(BaseModel):
    """Request body for creating an application under a product."""

    application_url: Optional[str] = ""
    health_check_url: Optional[str] = ""
    description: Optional[str] = ""
    restart_supported: bool = False
    restart_summary: Optional[str] = ""
    owner_contact: Optional[str] = ""
    support_group_id: Optional[int] = None
    support_group: Optional[str] = ""
    # CML v2 binding — user supplies names + app_type + (optional) subdomain;
    # the service resolves to project_id / application_id and may auto-build
    # cml_serving_url from the subdomain.
    cml_project_name: Optional[str] = ""
    cml_application_name: Optional[str] = ""
    cml_subdomain: Optional[str] = ""
    cml_app_type: Optional[str] = Field(default="generic", pattern=r"^(fastapi|runtime|ray|generic)$")
    cml_serving_url: Optional[str] = ""


class AppUpdate(BaseModel):
    """Request body for updating an application."""

    application_url: Optional[str] = None
    health_check_url: Optional[str] = None
    description: Optional[str] = None
    restart_supported: Optional[bool] = None
    restart_summary: Optional[str] = None
    owner_contact: Optional[str] = None
    support_group_id: Optional[int] = None
    support_group: Optional[str] = None
    cml_project_name: Optional[str] = None
    cml_application_name: Optional[str] = None
    cml_subdomain: Optional[str] = None
    cml_app_type: Optional[str] = Field(default=None, pattern=r"^(fastapi|runtime|ray|generic)$")
    cml_serving_url: Optional[str] = None


class AppResponse(BaseModel):
    """Application response object."""

    id: int
    product_id: int
    application_url: Optional[str] = ""
    health_check_url: Optional[str] = ""
    description: Optional[str] = ""
    restart_supported: bool = False
    restart_summary: Optional[str] = ""
    owner_contact: Optional[str] = ""
    support_group_id: Optional[int] = None
    support_group_name: Optional[str] = ""
    support_group: Optional[str] = ""
    # CML v2 binding fields — names + app_type + subdomain are user-input,
    # ids are server-resolved.
    cml_project_name: Optional[str] = ""
    cml_application_name: Optional[str] = ""
    cml_subdomain: Optional[str] = ""
    cml_app_type: Optional[str] = "generic"
    cml_project_id: Optional[str] = None
    cml_application_id: Optional[str] = None
    cml_serving_url: Optional[str] = ""
    cml_binding_error: Optional[str] = None
    cml_binding_status: str = "unconfigured"  # resolved | pending | error | unconfigured
    # Latest AppChecker outcome — populated by AppChecker on every probe so
    # the UI can show live health without waiting for an Issue.
    last_cml_status: Optional[str] = None
    last_relayops_health: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    last_check_error: Optional[str] = None
    is_system: bool = False
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ApplicationHealthCheckResponse(BaseModel):
    """One persisted health-check sample for the App Health time-series chart."""

    id: int
    application_id: int
    checked_at: datetime
    relayops_health: str
    cml_status: Optional[str] = None
    error: Optional[str] = None

    model_config = {"from_attributes": True}


class ApplicationVerificationRequest(BaseModel):
    """Request body for running an application verification HTTP check.

    The endpoint optionally also runs the CML name → application_id
    resolver against the owning project so the user gets one click that
    answers both questions: "is the URL reachable?" *and* "will CML find
    this app under my binding?". When ``project_id`` is provided we look
    up the Ops Project's cached ``cml_project_id`` and try
    ``resolve_application_id(project_id, name=..., subdomain=...)``.
    """

    url: str = Field(..., min_length=1, max_length=2048)
    method: str = Field(default="POST", pattern=r"^(?i)(GET|POST|PUT|PATCH|DELETE|HEAD)$")
    headers: Dict[str, str] = Field(default_factory=dict)
    body: Optional[Any] = None
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    # Optional CML binding context — when supplied, also resolves the app.
    project_id: Optional[int] = None
    cml_application_name: Optional[str] = None
    cml_subdomain: Optional[str] = None
    # Per-asset CML project override. Non-empty value tells the binding
    # probe to target this CML project instead of the owning Ops Project's
    # binding — matches the save path semantics in app_service.
    cml_project_name: Optional[str] = None


class ApplicationVerificationResponse(BaseModel):
    """Response payload for application verification checks."""

    ok: bool
    status_code: Optional[int] = None
    duration_ms: int
    response_headers: Dict[str, str] = Field(default_factory=dict)
    response_body: str = ""
    error: Optional[str] = None
    # Optional CML binding result (None when not requested).
    cml_binding_ok: Optional[bool] = None
    cml_application_id: Optional[str] = None
    cml_binding_error: Optional[str] = None
    # Real CML application name when the binding resolved (by name OR by
    # subdomain). Lets the form reverse-fill the App Name field from a
    # subdomain-only entry — mirrors the app picker's auto-fill behaviour.
    cml_resolved_application_name: Optional[str] = None
    # Cross-project rescue: when the subdomain was NOT found under the bound
    # CML project but DOES exist elsewhere in the workspace, these carry the
    # project / app it actually belongs to so the UI can tell the user where
    # to bind it. All None when no other-project match was found / searched.
    cml_other_project_id: Optional[str] = None
    cml_other_project_name: Optional[str] = None
    cml_other_application_id: Optional[str] = None
    cml_other_application_name: Optional[str] = None
    cml_scan_note: Optional[str] = None


class ApplicationBindingProbeRequest(BaseModel):
    """Resolve a CML app binding from name/subdomain WITHOUT an HTTP probe.

    Backs the App form's "Fetch" button next to the Subdomain field: given a
    subdomain (and/or name), reverse-fill the real CML app name and, on a
    same-project miss, report the project the subdomain actually belongs to.
    Same binding semantics as ``ApplicationVerificationRequest`` minus the URL.
    """

    project_id: Optional[int] = None
    cml_project_name: Optional[str] = None
    cml_application_name: Optional[str] = None
    cml_subdomain: Optional[str] = None


class ApplicationBindingProbeResponse(BaseModel):
    """Binding-only result — mirrors the CML fields of the verification run."""

    cml_binding_ok: Optional[bool] = None
    cml_application_id: Optional[str] = None
    cml_binding_error: Optional[str] = None
    cml_resolved_application_name: Optional[str] = None
    cml_other_project_id: Optional[str] = None
    cml_other_project_name: Optional[str] = None
    cml_other_application_id: Optional[str] = None
    cml_other_application_name: Optional[str] = None
    # Human-readable summary of the workspace scan when a subdomain didn't
    # match anywhere (e.g. "Searched 47 of 120 … — no match (…)"). None when a
    # match was found or no scan ran.
    cml_scan_note: Optional[str] = None


class ApplicationRecoveryScenarioResponse(BaseModel):
    """Structured application recovery scenario response."""

    id: int
    application_id: int
    scenario_type: str
    scenario_name: str
    condition_description: Optional[str] = ""
    action_steps: Optional[List[Any]] = None
    verification_steps: Optional[List[Any]] = None
    escalation_target: Optional[str] = ""
    fallback_owner_type: Optional[str] = None
    email_template: Optional[Dict[str, Any]] = None
    is_not_applicable: bool = False
    not_applicable_signoff_by: Optional[int] = None
    not_applicable_signoff_at: Optional[datetime] = None
    is_active: bool = True
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ApplicationRecoveryScenarioCreate(BaseModel):
    """Request body for creating a structured application recovery scenario."""

    scenario_type: str = Field(..., min_length=1, max_length=50)
    scenario_name: str = Field(default="", max_length=255)
    condition_description: str = ""
    action_steps: List[str] = Field(default_factory=list)
    verification_steps: List[str] = Field(default_factory=list)
    escalation_target: str = ""
    fallback_owner_type: Optional[str] = None
    email_template: Optional[Dict[str, Any]] = None
    is_not_applicable: bool = False
    is_active: bool = True


class ApplicationRecoveryScenarioUpdate(BaseModel):
    """Request body for updating a structured application recovery scenario."""

    scenario_type: Optional[str] = Field(default=None, min_length=1, max_length=50)
    scenario_name: Optional[str] = Field(default=None, max_length=255)
    condition_description: Optional[str] = None
    action_steps: Optional[List[str]] = None
    verification_steps: Optional[List[str]] = None
    escalation_target: Optional[str] = None
    fallback_owner_type: Optional[str] = None
    email_template: Optional[Dict[str, Any]] = None
    is_not_applicable: Optional[bool] = None
    is_active: Optional[bool] = None


class ApplicationApiCheckResponse(BaseModel):
    """One persisted API validation check belonging to an Application."""

    id: int
    application_id: int
    collection_name: Optional[str] = ""
    name: str
    method: str
    url: str
    headers: Dict[str, str] = Field(default_factory=dict)
    body: Optional[str] = ""
    body_is_json: bool = False
    timeout_seconds: int = 15
    expected_status: Optional[int] = None
    expected_body_contains: Optional[str] = None
    source: str = "manual"
    source_ref: Optional[str] = None
    is_active: bool = True
    sort_order: int = 0
    last_run_at: Optional[datetime] = None
    last_run_ok: Optional[bool] = None
    last_run_status_code: Optional[int] = None
    last_run_duration_ms: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    created_by: Optional[int] = None
    updated_by: Optional[int] = None

    model_config = {"from_attributes": True}


class ApplicationApiCheckCreate(BaseModel):
    """Create a new persisted API check under an application."""

    name: str = Field(..., min_length=1, max_length=255)
    collection_name: Optional[str] = Field(default="", max_length=255)
    method: str = Field(default="GET", pattern=r"^(?i)(GET|POST|PUT|PATCH|DELETE|HEAD)$")
    url: str = Field(..., min_length=1, max_length=2048)
    headers: Dict[str, str] = Field(default_factory=dict)
    body: Optional[str] = ""
    body_is_json: bool = False
    timeout_seconds: int = Field(default=15, ge=1, le=60)
    expected_status: Optional[int] = Field(default=None, ge=100, le=599)
    expected_body_contains: Optional[str] = None
    source: str = Field(default="manual", pattern=r"^(manual|swagger|imported)$")
    source_ref: Optional[str] = Field(default=None, max_length=255)
    is_active: bool = True
    sort_order: int = 0


class ApplicationApiCheckUpdate(BaseModel):
    """Partial-update payload — only fields the user actually changed."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    collection_name: Optional[str] = Field(default=None, max_length=255)
    method: Optional[str] = Field(default=None, pattern=r"^(?i)(GET|POST|PUT|PATCH|DELETE|HEAD)$")
    url: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    headers: Optional[Dict[str, str]] = None
    body: Optional[str] = None
    body_is_json: Optional[bool] = None
    timeout_seconds: Optional[int] = Field(default=None, ge=1, le=60)
    expected_status: Optional[int] = Field(default=None, ge=100, le=599)
    expected_body_contains: Optional[str] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class ApplicationApiCheckRunResponse(BaseModel):
    """One persisted execution of an API check."""

    id: int
    api_check_id: int
    ran_at: datetime
    ran_by: Optional[int] = None
    ok: bool
    status_code: Optional[int] = None
    duration_ms: int
    response_headers: Dict[str, str] = Field(default_factory=dict)
    response_body: Optional[str] = ""
    error: Optional[str] = None
    request_url: Optional[str] = None
    request_method: Optional[str] = None

    model_config = {"from_attributes": True}


class ApplicationApiCheckRunResult(BaseModel):
    """Response from POST /api/api-checks/{id}/run — the freshly executed
    run summary, returned to the caller so the UI can update without an
    extra GET. ``assertion_*`` carry the optional expected_status /
    expected_body_contains verdict so the UI can show pass/fail labels."""

    run: ApplicationApiCheckRunResponse
    assertion_passed: Optional[bool] = None
    assertion_reason: Optional[str] = None


class AppDuplicateRequest(BaseModel):
    """Request body for duplicating an Application into a (possibly different) product.

    ``recovery_scenario_ids`` mirrors ``JobDuplicateRequest.scenario_ids``:
    ``None`` copies all recovery scenarios; an explicit list filters to that
    subset (empty list = copy nothing).
    """

    target_product_id: int = Field(..., description="Product that should own the new app (cross-project allowed)")
    cml_application_name: str = Field(..., min_length=1, max_length=255)
    recovery_scenario_ids: Optional[List[int]] = None
