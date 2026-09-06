"""
Base types for the Checker layer.

Defines the unified result types used across all Checker branches (APP, CML, MMP).
These are pure data classes with no database dependencies.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class AnomalyType(str, Enum):
    """Anomaly event types corresponding to IssueType constants."""

    JOB_FAILED = "job_failed"
    JOB_STALE = "job_stale"
    JOB_NOT_TRIGGERED = "job_not_triggered"
    APP_OFFLINE = "app_offline"
    MMP_DRIFT = "mmp_drift"
    MMP_FAIRNESS_RISK = "mmp_fairness_risk"
    MMP_RUN_PENDING_APPROVAL = "mmp_run_pending_approval"
    MMP_PENDING_REVIEW = "mmp_pending_review"
    MMP_UNAPPROVED_EXP_RUN = "mmp_unapproved_exp_run"


@dataclass
class AnomalyEvent:
    """
    Detection result representing an anomaly found by a Checker branch.

    Produced by Checker, consumed by Controller → Assign module.
    """

    anomaly_type: AnomalyType
    title: str
    description: str
    product_id: int
    job_id: Optional[int] = None
    app_id: Optional[int] = None
    metadata: dict = field(default_factory=dict)
    # Stable per-occurrence dedup key (e.g. the CML cml_run_id). When set, the
    # issue layer creates exactly one Issue per (type, entity, dedup_key) so a
    # recurring fault re-alerts on each new event instead of being suppressed
    # by a still-open earlier Issue. None falls back to "any open issue" dedup.
    dedup_key: Optional[str] = None


@dataclass
class RecoveryEvent:
    """A previously-anomalous signal observed back to normal.

    Emitted by a Checker (today only MmpChecker) when a monitored flag reads
    healthy again, so the Controller can auto-close the matching open Issue.
    ``issue_type`` is the IssueType string the recovered signal maps to.
    """

    issue_type: str
    job_id: int
    product_id: Optional[int] = None
    reason: str = ""
    # When set, only Issues created strictly BEFORE this timestamp are
    # auto-closed. Used by the MMP "approval supersedes" path: once the latest
    # production run is approved, drift / pending Issues raised against earlier
    # runs (created before the approval) are stale and closed in bulk, even if
    # MMP's aggregate attention flag still reads True. None = close every open
    # Issue of the type regardless of age (the legacy flag-read-healthy
    # recovery).
    close_created_before: Optional[datetime] = None


class CheckStatus(str, Enum):
    """Possible outcomes of a single check invocation."""

    HEALTHY = "healthy"
    ANOMALY = "anomaly"
    INCONCLUSIVE = "inconclusive"
    SKIPPED = "skipped"


@dataclass
class CheckResult:
    """
    Unified check result returned by every Checker branch.

    Each invocation of a checker (AppChecker / CmlChecker / MmpChecker)
    produces exactly one CheckResult.
    """

    status: CheckStatus
    anomaly_event: Optional[AnomalyEvent] = None
    reason: str = ""
