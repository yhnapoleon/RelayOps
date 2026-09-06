"""Job schemas — create, update, response, failure scenarios, verification."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class JobCreate(BaseModel):
    """Request body for creating a job under a product."""

    mmp_project_id: Optional[str] = ""
    mmp_model_id: Optional[str] = ""
    control_m_job_name: Optional[str] = ""
    control_m_cron: Optional[str] = ""
    # CML v2 binding — user supplies project + job NAMES; the service resolves
    # them to ids on create/update via ControlInterface.
    cml_project_name: Optional[str] = ""
    cml_job_name: Optional[str] = ""
    schedule_cron: Optional[str] = ""
    description: Optional[str] = ""
    dependencies: Optional[Dict[str, Any]] = None
    failure_strategy_summary: Optional[str] = ""
    dependency_notes: Optional[str] = ""
    owner_contact: Optional[str] = ""
    support_group_id: Optional[int] = None
    support_group: Optional[str] = ""
    runbook_required: bool = True
    has_mmp_dependency: Optional[bool] = None
    # Per-job staleness tolerance — see Job.sla_preset / Job.sla_custom_minutes.
    # "strict" | "normal" | "loose" | None (= normal). Custom wins when set.
    sla_preset: Optional[str] = None
    sla_custom_minutes: Optional[int] = Field(default=None, ge=1)


class JobUpdate(BaseModel):
    """Request body for updating a job."""

    mmp_project_id: Optional[str] = None
    mmp_model_id: Optional[str] = None
    control_m_job_name: Optional[str] = None
    control_m_cron: Optional[str] = None
    cml_project_name: Optional[str] = None
    cml_job_name: Optional[str] = None
    schedule_cron: Optional[str] = None
    description: Optional[str] = None
    dependencies: Optional[Dict[str, Any]] = None
    failure_strategy_summary: Optional[str] = None
    dependency_notes: Optional[str] = None
    owner_contact: Optional[str] = None
    support_group_id: Optional[int] = None
    support_group: Optional[str] = None
    runbook_required: Optional[bool] = None
    has_mmp_dependency: Optional[bool] = None
    sla_preset: Optional[str] = None
    sla_custom_minutes: Optional[int] = Field(default=None, ge=1)


class JobResponse(BaseModel):
    """Job response object."""

    id: int
    product_id: int
    mmp_project_id: Optional[str] = ""
    mmp_model_id: Optional[str] = ""
    control_m_job_name: Optional[str] = ""
    control_m_cron: Optional[str] = ""
    # CML v2 binding fields — names are user-input, ids are server-resolved.
    cml_project_name: Optional[str] = ""
    cml_job_name: Optional[str] = ""
    cml_project_id: Optional[str] = None
    cml_job_id: Optional[str] = None
    cml_binding_error: Optional[str] = None
    cml_binding_status: str = "unconfigured"  # resolved | pending | error | unconfigured
    schedule_cron: Optional[str] = ""
    description: Optional[str] = ""
    dependencies: Optional[Dict[str, Any]] = None
    failure_strategy_summary: Optional[str] = ""
    dependency_notes: Optional[str] = ""
    owner_contact: Optional[str] = ""
    support_group_id: Optional[int] = None
    support_group_name: Optional[str] = ""
    support_group: Optional[str] = ""
    runbook_required: bool = True
    has_mmp_dependency: Optional[bool] = None
    sla_preset: Optional[str] = None
    sla_custom_minutes: Optional[int] = None
    is_system: bool = False
    # Last time we polled CML for this job's status (live /cml-status,
    # background CmlChecker, or Check Now). NULL means never polled.
    last_checked_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class JobFailureScenarioResponse(BaseModel):
    """Structured job-failure scenario response."""

    id: int
    job_id: int
    scenario_type: str
    scenario_name: str
    condition_description: Optional[str] = ""
    detection_source: Optional[str] = ""
    diagnostic_steps: Optional[List[Any]] = None
    action_steps: Optional[List[Any]] = None
    verification_steps: Optional[List[Any]] = None
    escalation_target: Optional[str] = ""
    fallback_owner_type: Optional[str] = None
    threshold_operator: Optional[str] = ""
    threshold_value: Optional[float] = None
    threshold_feature_list: Optional[List[str]] = None
    email_template: Optional[Dict[str, Any]] = None
    is_not_applicable: bool = False
    not_applicable_signoff_by: Optional[int] = None
    not_applicable_signoff_at: Optional[datetime] = None
    is_active: bool = True
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class JobFailureScenarioCreate(BaseModel):
    """Request body for creating a structured job failure scenario."""

    scenario_type: str = Field(..., min_length=1, max_length=50)
    scenario_name: str = Field(default="", max_length=255)
    condition_description: str = ""
    detection_source: str = ""
    diagnostic_steps: List[str] = Field(default_factory=list)
    action_steps: List[str] = Field(default_factory=list)
    verification_steps: List[str] = Field(default_factory=list)
    escalation_target: str = ""
    fallback_owner_type: Optional[str] = None
    threshold_operator: Optional[str] = ""
    threshold_value: Optional[float] = None
    threshold_feature_list: List[str] = Field(default_factory=list)
    email_template: Optional[Dict[str, Any]] = None
    is_not_applicable: bool = False
    is_active: bool = True


class JobFailureScenarioUpdate(BaseModel):
    """Request body for updating a structured job failure scenario."""

    scenario_type: Optional[str] = Field(default=None, min_length=1, max_length=50)
    scenario_name: Optional[str] = Field(default=None, max_length=255)
    condition_description: Optional[str] = None
    detection_source: Optional[str] = None
    diagnostic_steps: Optional[List[str]] = None
    action_steps: Optional[List[str]] = None
    verification_steps: Optional[List[str]] = None
    escalation_target: Optional[str] = None
    fallback_owner_type: Optional[str] = None
    threshold_operator: Optional[str] = None
    threshold_value: Optional[float] = None
    threshold_feature_list: Optional[List[str]] = None
    email_template: Optional[Dict[str, Any]] = None
    is_not_applicable: Optional[bool] = None
    is_active: Optional[bool] = None


class JobDuplicateRequest(BaseModel):
    """Request body for duplicating a Job into a (possibly different) product.

    ``scenario_ids`` controls which failure scenarios come with the copy:
    ``None`` copies all scenarios; an explicit (possibly empty) list copies
    only the scenarios whose source ids appear in it.
    """

    target_product_id: int = Field(..., description="Product that should own the new job (cross-project allowed)")
    control_m_job_name: str = Field(..., min_length=1, max_length=255)
    scenario_ids: Optional[List[int]] = None


class JobVerificationRequest(BaseModel):
    """Request body for running a CML job connectivity verification.

    The CML project is inherited from the owning Ops Project (looked up via
    ``project_id``), so the validate path matches the save path exactly. The
    legacy ``cml_project_name`` field is accepted for back-compat but
    ignored when ``project_id`` is provided.

    ``cml_job_name`` is the binding key; ``control_m_job_name`` is a
    deprecated alias used as fallback when ``cml_job_name`` isn't supplied.
    """

    project_id: Optional[int] = Field(default=None, description="Owning Ops Project id")
    cml_project_name: Optional[str] = Field(default=None, max_length=512)
    cml_job_name: Optional[str] = Field(default=None, max_length=512)
    control_m_job_name: Optional[str] = Field(default=None, max_length=512)
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)


class JobVerificationResponse(BaseModel):
    """Response payload for job connectivity verification checks."""

    ok: bool
    job_found: bool = False
    job_status: Optional[str] = None
    last_run: Optional[str] = None
    cml_project_id: Optional[str] = None
    cml_job_id: Optional[str] = None
    cml_run_id: Optional[str] = None
    # CML job definition snapshot — populated when the job detail
    # (GET /projects/{pid}/jobs/{jid}) is reachable. The schedule is the
    # raw cron expression CML stores; timezone is informational so the UI
    # can warn when CML interprets the cron in a non-UTC timezone.
    cml_schedule: Optional[str] = None
    cml_timezone: Optional[str] = None
    duration_ms: int = 0


class MmpDriftSnapshotResponse(BaseModel):
    """One persisted MMP drift observation for the Job Health timeline.

    Written by MmpChecker on every successful drift read (true OR false);
    INCONCLUSIVE / SKIPPED outcomes do not produce rows, so the timeline
    stays free of phantom samples. ``cml_model_id`` is denormalized so a
    timeline survives Job rebinding.
    """

    id: int
    job_id: int
    cml_model_id: Optional[int] = None
    drifted: bool
    drift_details: Optional[str] = None
    observed_at: datetime

    model_config = {"from_attributes": True}


class MmpVerificationRequest(BaseModel):
    """Request body for MMP connectivity / binding verification.

    Both fields are optional — passing nothing only tests that the
    workspace directory loads (i.e. base_url + bearer reach a live MMP).
    Passing ``mmp_project_id`` additionally checks the project exists.
    Passing both also checks the model exists under that project and
    reports its current drift state.
    """

    mmp_project_id: Optional[str] = Field(default=None, max_length=512)
    mmp_model_id: Optional[str] = Field(default=None, max_length=512)


class MmpVerificationResponse(BaseModel):
    """Response payload for MMP connectivity verification."""

    ok: bool
    duration_ms: int = 0
    # Directory load step
    directory_loaded: bool = False
    total_projects: int = 0
    # Project lookup step (only set when mmp_project_id was passed)
    project_found: Optional[bool] = None
    business_name: Optional[str] = None
    model_count: Optional[int] = None
    # Model lookup step (only set when mmp_model_id was passed)
    model_found: Optional[bool] = None
    cml_model_id: Optional[int] = None
    # Current MMP attention signals (only set when both project + model
    # resolved). Mirror attention_required.{model_drifted, run_pending_approval,
    # run_pending_user_review} so the Validate MMP panel can show all three.
    drifted: Optional[bool] = None
    drift_details: Optional[str] = None
    pending_approval: Optional[bool] = None
    pending_approval_details: Optional[str] = None
    pending_review: Optional[bool] = None
    pending_review_details: Optional[str] = None
    # Single error string, populated for any failure path
    error: Optional[str] = None
