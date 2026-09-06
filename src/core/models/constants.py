"""Enum and constant classes for core business entities."""


class ProjectRole:
    """Per-project membership roles (``ProjectMember.role``) — distinct from
    the global ``UserRole`` login identity.

    - ``business_owner``: the single project owner (matches
      ``Project.owner_id``). Full control, including member management and
      ownership transfer. Set via the owner-transfer flow, never assigned
      directly through the Members UI.
    - ``product_member``: a project *editor*. Can view AND modify the
      project and all its assets (products, jobs, apps, scenarios, project
      settings) and submit handovers — everything the owner can do EXCEPT
      manage members, transfer ownership, or delete the project.
    - ``relayops_member``: a view-only collaborator. Sees the project but cannot
      mutate its assets.
    """

    BUSINESS_OWNER = "business_owner"
    PRODUCT_MEMBER = "product_member"
    RELAYOPS_MEMBER = "relayops_member"

    ALL = [BUSINESS_OWNER, PRODUCT_MEMBER, RELAYOPS_MEMBER]
    # Roles a user can pick when adding a member (owner is set via transfer).
    ASSIGNABLE = [RELAYOPS_MEMBER, PRODUCT_MEMBER]
    # Roles that carry edit/mutate rights on the project and its assets.
    EDITOR_ROLES = [BUSINESS_OWNER, PRODUCT_MEMBER]


class ProductLifecycleStatus:
    """Product-level lifecycle status for the currently effective handover state."""

    DRAFTING = "drafting"
    ACTIVE = "active"
    ARCHIVED = "archived"

    ALL = [DRAFTING, ACTIVE, ARCHIVED]


class ProductVersionStatus:
    """Approval status for a specific product version."""

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"

    ALL = [DRAFT, PENDING_REVIEW, APPROVED, REJECTED, SUPERSEDED]

    TRANSITIONS = {
        DRAFT: [PENDING_REVIEW],
        PENDING_REVIEW: [APPROVED, REJECTED],
        APPROVED: [SUPERSEDED],
        REJECTED: [DRAFT, PENDING_REVIEW],
        SUPERSEDED: [],
    }

    @classmethod
    def can_transition(cls, from_status: str, to_status: str) -> bool:
        """Check if a version status transition is valid."""
        return to_status in cls.TRANSITIONS.get(from_status, [])


class ProductStatus:
    """
    Lightweight product status constants.

    After version control moved to the project level, this field only gates
    monitoring: the checkers poll assets whose owning product is in
    DRAFT / PENDING_REVIEW / ACTIVE, and skip ARCHIVED. The submit/approve
    lifecycle now lives on the project (see ProjectVersion / ProjectLifecycleStatus).
    """

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    ACTIVE = "active"
    ARCHIVED = "archived"

    ALL = [DRAFT, PENDING_REVIEW, ACTIVE, ARCHIVED]

    # Valid transitions: from_status -> [allowed_to_statuses]
    TRANSITIONS = {
        DRAFT: [PENDING_REVIEW],
        PENDING_REVIEW: [DRAFT, ACTIVE],  # reject -> draft, approve -> active
        ACTIVE: [ARCHIVED],
        ARCHIVED: [],  # terminal state
    }

    @classmethod
    def can_transition(cls, from_status: str, to_status: str) -> bool:
        """Check if a status transition is valid."""
        return to_status in cls.TRANSITIONS.get(from_status, [])


class ProjectLifecycleStatus:
    """Project-level lifecycle status for the currently effective handover state."""

    DRAFTING = "drafting"
    ACTIVE = "active"
    ARCHIVED = "archived"

    ALL = [DRAFTING, ACTIVE, ARCHIVED]


class ProjectStatus:
    """Project workflow status — drives the submit/approve handover lifecycle."""

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    ACTIVE = "active"
    ARCHIVED = "archived"

    ALL = [DRAFT, PENDING_REVIEW, ACTIVE, ARCHIVED]


class ProjectVersionStatus:
    """Approval status for a specific project version."""

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"

    ALL = [DRAFT, PENDING_REVIEW, APPROVED, REJECTED, SUPERSEDED]

    TRANSITIONS = {
        DRAFT: [PENDING_REVIEW],
        PENDING_REVIEW: [APPROVED, REJECTED],
        APPROVED: [SUPERSEDED],
        REJECTED: [DRAFT, PENDING_REVIEW],
        SUPERSEDED: [],
    }

    @classmethod
    def can_transition(cls, from_status: str, to_status: str) -> bool:
        """Check if a version status transition is valid."""
        return to_status in cls.TRANSITIONS.get(from_status, [])


class IssueType:
    """Issue type constants.

    The four MMP_* entries mirror the four ``attention_required`` flags
    MMP surfaces per model. MMP_DRIFT is the umbrella drift signal (the
    boolean ``model_drifted.status``); the run-level drift sub-flags
    (pmetric / fmean / fmissing) are folded into the MMP_DRIFT issue's
    description rather than getting their own issue types — they almost
    always co-occur, so splitting them would produce duplicate noise.
    """

    # Handover workflow
    HANDOVER_REVIEW = "handover_review"
    SCOPE_CHANGE_REVIEW = "scope_change_review"

    # Automated alerts (Task 4)
    JOB_NOT_TRIGGERED = "job_not_triggered"
    JOB_FAILED = "job_failed"
    JOB_STALE = "job_stale"  # Status is success but last_run is beyond SLA threshold (miss)
    APP_OFFLINE = "app_offline"
    MMP_DRIFT = "mmp_drift"
    MMP_FAIRNESS_RISK = "mmp_fairness_risk"
    MMP_RUN_PENDING_APPROVAL = "mmp_run_pending_approval"
    # MMP_PENDING_REVIEW mirrors attention_required.run_pending_user_review —
    # a production run awaiting user review. Like MMP_RUN_PENDING_APPROVAL it is
    # a model-governance signal handled directly on the MMP platform (no Ops
    # runbook / workbench), so the My Actions card offers a deep link + a
    # one-click resolve instead of the execution workspace.
    MMP_PENDING_REVIEW = "mmp_pending_review"
    MMP_UNAPPROVED_EXP_RUN = "mmp_unapproved_exp_run"

    ALL = [
        HANDOVER_REVIEW,
        SCOPE_CHANGE_REVIEW,
        JOB_NOT_TRIGGERED,
        JOB_FAILED,
        JOB_STALE,
        APP_OFFLINE,
        MMP_DRIFT,
        MMP_FAIRNESS_RISK,
        MMP_RUN_PENDING_APPROVAL,
        MMP_PENDING_REVIEW,
        MMP_UNAPPROVED_EXP_RUN,
    ]


class IssueStatus:
    """Issue status constants."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    FALSE_POSITIVE = "false_positive"

    ALL = [OPEN, IN_PROGRESS, RESOLVED, CLOSED, FALSE_POSITIVE]


# Resolution text monitoring writes when it auto-closes an alert whose
# underlying problem went away. Analytics excludes these from "manual
# interventions"; the agent layer uses it to split auto vs manual closes.
AUTO_CLOSE_RESOLUTION = "Auto-closed: problem resolved"


class IssueActionType:
    """Structured action events emitted from the Phase 3 execution workspace."""

    START_WORKING = "start_working"
    RECORD_STEP = "record_step"
    VERIFICATION_PASSED = "verification_passed"
    ESCALATE = "escalate"
    RETURN_TO_OWNER = "return_to_owner"
    RESOLVE = "resolve"
    FALSE_POSITIVE = "false_positive"

    ALL = [
        START_WORKING,
        RECORD_STEP,
        VERIFICATION_PASSED,
        ESCALATE,
        RETURN_TO_OWNER,
        RESOLVE,
        FALSE_POSITIVE,
    ]


class JobFailureScenarioType:
    """Structured job-failure scenarios for future runbook execution.

    MMP scenario surface, post Phase-8-rework:

    * Required (auto-detected from ``attention_required.*.status``):
        - ``MMP_DRIFT_DETECTED``       → model_drifted
        - ``MMP_FAIRNESS_RISK``        → has_fairness_risk
        - ``MMP_RUN_PENDING_APPROVAL`` → run_pending_approval
        - ``MMP_UNAPPROVED_EXP_RUN``   → has_unapproved_exp_run
    * Optional (no auto-detection — Ops can configure runbooks for the
      drift sub-types so they can react manually when the drift Issue
      description shows pmetric/fmean/fmissing flags):
        - ``MMP_PERF_DRIFT``         → run.pmetric_drifted
        - ``MMP_FEATURE_DRIFT``      → run.fmean_drifted
        - ``MMP_DATA_QUALITY_DRIFT`` → run.fmissing_drifted
    * Optional outcome runbook (no auto-detection):
        - ``MMP_NO_SIGNIFICANT_DRIFT`` → the drift Issue fired, but on
          inspection the reported deviation is within the model's agreed
          tolerance. Documents the "confirm, record the evidence, close as
          benign" path so operators stop improvising it per incident.

    ``PASS_THRESHOLD`` / ``FAIL_THRESHOLD`` are pre-Phase-8 legacy from a
    metric-threshold model MMP no longer exposes. Kept in the enum so
    existing rows don't error on load; the frontend hides them from new
    scenario selectors.
    """

    NOT_TRIGGERED = "not_triggered"
    TRIGGERED_BUT_FAILED = "triggered_but_failed"
    DEPENDENCY_FAILED = "dependency_failed"
    LOGIC_ISSUE = "logic_issue"
    EXTERNAL_SYSTEM_ISSUE = "external_system_issue"
    # MMP — auto-detected (required for the MMP template)
    MMP_DRIFT_DETECTED = "mmp_drift_detected"
    MMP_FAIRNESS_RISK = "mmp_fairness_risk"
    MMP_RUN_PENDING_APPROVAL = "mmp_run_pending_approval"
    MMP_UNAPPROVED_EXP_RUN = "mmp_unapproved_exp_run"
    # MMP — manual-only sub-drift scenarios (no auto-detection today)
    MMP_PERF_DRIFT = "mmp_perf_drift"
    MMP_FEATURE_DRIFT = "mmp_feature_drift"
    MMP_DATA_QUALITY_DRIFT = "mmp_data_quality_drift"
    # MMP — manual-only benign outcome for a drift Issue
    MMP_NO_SIGNIFICANT_DRIFT = "mmp_no_significant_drift"
    # Deprecated by Phase 8 — kept for back-compat with existing data.
    PASS_THRESHOLD = "pass_threshold"
    FAIL_THRESHOLD = "fail_threshold"
    OTHER = "other"

    ALL = [
        NOT_TRIGGERED,
        TRIGGERED_BUT_FAILED,
        DEPENDENCY_FAILED,
        LOGIC_ISSUE,
        EXTERNAL_SYSTEM_ISSUE,
        MMP_DRIFT_DETECTED,
        MMP_FAIRNESS_RISK,
        MMP_RUN_PENDING_APPROVAL,
        MMP_UNAPPROVED_EXP_RUN,
        MMP_PERF_DRIFT,
        MMP_FEATURE_DRIFT,
        MMP_DATA_QUALITY_DRIFT,
        MMP_NO_SIGNIFICANT_DRIFT,
        PASS_THRESHOLD,
        FAIL_THRESHOLD,
        OTHER,
    ]


class ApplicationRecoveryScenarioType:
    """Structured application recovery scenarios for future runbook execution."""

    OFFLINE = "offline"
    HEALTHCHECK_FAILED = "healthcheck_failed"
    RESTART_REQUIRED = "restart_required"
    RAY_ACTOR_MISSING = "ray_actor_missing"
    DEPLOYMENT_ISSUE = "deployment_issue"
    OTHER = "other"

    ALL = [
        OFFLINE,
        HEALTHCHECK_FAILED,
        RESTART_REQUIRED,
        RAY_ACTOR_MISSING,
        DEPLOYMENT_ISSUE,
        OTHER,
    ]


class FallbackOwnerType:
    """Escalation fallback target when Ops cannot directly resolve a scenario."""

    PRODUCT_OWNER = "product_owner"
    TECH_OWNER = "tech_owner"
    RELAYOPS_SELF_HANDLE = "relayops_self_handle"
    CASE_BY_CASE = "case_by_case"
    OTHER = "other"

    ALL = [PRODUCT_OWNER, TECH_OWNER, RELAYOPS_SELF_HANDLE, CASE_BY_CASE, OTHER]


class SupportGroupSourceType:
    """Origin of a support group entry."""

    MANUAL = "manual"
    SEEDED = "seeded"
    DIRECTORY = "directory"

    ALL = [MANUAL, SEEDED, DIRECTORY]
