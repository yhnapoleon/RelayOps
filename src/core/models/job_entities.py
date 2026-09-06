"""Job domain models."""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from core.models.database import Base
from core.models.constants import FallbackOwnerType, JobFailureScenarioType


class Job(Base):
    """
    Job model — belongs to a Product. Represents a scheduled batch job.

    Attributes:
        id: Auto-increment primary key.
        product_id: FK to products.id.
        mmp_project_id: MMP project identifier.
        mmp_model_id: MMP model identifier.
        control_m_job_name: Control-M job name.
        control_m_cron: Control-M cron expression.
        schedule_cron: Expected schedule cron expression.
        description: Job description.
        dependencies: JSON field for dependency definitions.
        failure_strategy_summary: Top-level Ops handling summary.
        dependency_notes: Human-readable explanation of upstream dependencies.
        owner_contact: Owner or SME contact for escalations.
        support_group: Group identifier/name for operational support.
        runbook_required: Whether this job must be fully handover-ready.
        created_at: Creation timestamp.
        updated_at: Last update timestamp.
    """

    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    mmp_project_id = Column(String(255), nullable=True, default="")
    mmp_model_id = Column(String(255), nullable=True, default="")
    control_m_job_name = Column(String(255), nullable=True, default="")
    control_m_cron = Column(String(255), nullable=True, default="")
    # CML v2 binding: user-input identifiers (resolved to ids at first use, see core.services.job_service).
    cml_project_name = Column(String(255), nullable=True, default="")
    cml_job_name = Column(String(255), nullable=True, default="")
    # Cached CML resource ids; nullable so unresolved/invalid bindings degrade gracefully.
    cml_project_id = Column(String(64), nullable=True)
    cml_job_id = Column(String(64), nullable=True)
    # Last resolver error message — populated when CML was reachable but the
    # name didn't resolve. NULL means binding is healthy or never attempted.
    cml_binding_error = Column(Text, nullable=True)
    schedule_cron = Column(String(255), nullable=True, default="")
    description = Column(Text, nullable=True, default="")
    dependencies = Column(JSON, nullable=True)
    failure_strategy_summary = Column(Text, nullable=True, default="")
    dependency_notes = Column(Text, nullable=True, default="")
    owner_contact = Column(String(255), nullable=True, default="")
    support_group_id = Column(Integer, nullable=True)
    support_group_name_snapshot = Column(String(255), nullable=True, default="")
    support_group = Column(String(255), nullable=True, default="")
    runbook_required = Column(Boolean, nullable=False, default=True)
    has_mmp_dependency = Column(Boolean, nullable=True, default=None)
    # Per-job staleness tolerance knob (used by core.integrations.staleness via
    # core.checker.cml_checker._safety_factor_for):
    #   sla_preset      : "strict" (1.0) | "normal" (1.5, default) | "loose" (3.0)
    #                     NULL is treated as "normal" so legacy rows behave the
    #                     same as before this column existed.
    #   sla_custom_minutes : when set, overrides the cron-derived threshold with
    #                        a flat per-job value (in minutes). Takes precedence
    #                        over sla_preset. NULL means "use preset".
    # Surfaced in the Job create/edit form ("Strictness" + "Custom hours...").
    sla_preset = Column(String(16), nullable=True, default=None)
    sla_custom_minutes = Column(Integer, nullable=True, default=None)
    is_system = Column(Integer, nullable=False, default=0)  # 0=normal, 1=system-managed(read-only)
    # Last time we polled CML for this job's status (either the live
    # /cml-status endpoint or the background CmlChecker). Surfaced on the
    # asset card so users can tell how fresh the displayed status is.
    # NULL when the job has never been polled (e.g. binding unresolved).
    last_checked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product = relationship("Product", back_populates="jobs")
    failure_scenarios = relationship(
        "JobFailureScenario",
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class JobFailureScenario(Base):
    """Structured job-failure scenario for batch-A data modeling and future runbook execution."""

    __tablename__ = "job_failure_scenarios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    scenario_type = Column(String(50), nullable=False, default=JobFailureScenarioType.OTHER)
    scenario_name = Column(String(255), nullable=False)
    condition_description = Column(Text, nullable=True, default="")
    detection_source = Column(String(255), nullable=True, default="")
    diagnostic_steps = Column(JSON, nullable=True)
    action_steps = Column(JSON, nullable=True)
    verification_steps = Column(JSON, nullable=True)
    escalation_target = Column(String(255), nullable=True, default="")
    # Owner-email template used by the "send email to owner" action — prefills
    # the Outlook draft (mailto) launched from the issue-handling page.
    # Shape: {"to": str, "cc": str, "subject": str, "body": str}. NULL = no
    # template configured; the send action falls back to the job's owner_contact.
    email_template = Column(JSON, nullable=True)
    fallback_owner_type = Column(String(50), nullable=True, default=FallbackOwnerType.CASE_BY_CASE)
    threshold_operator = Column(String(8), nullable=True, default="")
    threshold_value = Column(Float, nullable=True)
    threshold_feature_list = Column(JSON, nullable=True)
    is_not_applicable = Column(Boolean, nullable=False, default=False)
    not_applicable_signoff_by = Column(Integer, nullable=True)
    not_applicable_signoff_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    job = relationship("Job", back_populates="failure_scenarios")


class JobExecution(Base):
    """
    Job execution snapshot — persisted by the monitoring Controller each
    time CmlChecker polls CML Platform and observes a new (`last_run`,
    `status`) pair for a Job.

    There is no inbound heartbeat path. Rows are written by
    CmlChecker.run_cycle on dedup by (job_id, timestamp) so each unique
    upstream execution is recorded exactly once, regardless of how many
    polls observe it.

    Consumed by the analytics router for failure-rate / failure-streak
    metrics.

    Attributes:
        id: Auto-increment primary key.
        job_id: FK to jobs.id — which Job this snapshot belongs to.
        status: Normalized execution status (success / failed / running / waiting).
        timestamp: The upstream `last_run` reported by CML.
        metadata_json: Optional metadata (e.g. {"source": "cml_polling"}).
        received_at: When RelayOps recorded this snapshot.
    """

    __tablename__ = "job_executions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(50), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    # CML v2 run id — used as the dedup key when polling /api/v2/.../runs.
    # Nullable for back-compat with legacy rows persisted before Phase 2.
    cml_run_id = Column(String(64), nullable=True, index=True)
    metadata_json = Column(JSON, nullable=True)
    received_at = Column(DateTime, default=datetime.utcnow)

    job = relationship("Job", foreign_keys=[job_id])
