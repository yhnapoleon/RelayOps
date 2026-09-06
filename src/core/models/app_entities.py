"""Application domain models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from core.models.database import Base
from core.models.constants import ApplicationRecoveryScenarioType, FallbackOwnerType


class Application(Base):
    """
    Application model — belongs to a Product. Represents a web application.

    Attributes:
        id: Auto-increment primary key.
        product_id: FK to products.id.
        application_url: Application URL.
        health_check_url: Health check endpoint URL.
        description: Application description.
        restart_supported: Whether restart is a supported remediation path.
        restart_summary: Short summary of restart method.
        owner_contact: Owner or SME contact for escalations.
        support_group: Group identifier/name for operational support.
        created_at: Creation timestamp.
        updated_at: Last update timestamp.
    """

    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    application_url = Column(String(512), nullable=True, default="")
    health_check_url = Column(String(512), nullable=True, default="")
    # CML v2 binding: user-input identifiers (resolved at first use).
    cml_project_name = Column(String(255), nullable=True, default="")
    cml_application_name = Column(String(255), nullable=True, default="")
    cml_subdomain = Column(String(255), nullable=True, default="")
    # Probe routing: fastapi | runtime | ray | generic. Drives which §5 contract
    # the AppInterface enforces against the serving URL.
    cml_app_type = Column(String(20), nullable=True, default="generic")
    # Cached CML ids and serving URL (built from subdomain, may be overridden).
    cml_project_id = Column(String(64), nullable=True)
    cml_application_id = Column(String(64), nullable=True)
    cml_serving_url = Column(String(512), nullable=True, default="")
    # Last resolver error — NULL means binding is healthy or never attempted.
    cml_binding_error = Column(Text, nullable=True)
    # Last AppChecker outcome — written by AppChecker.check_app on every probe.
    # Surfaced on the Product UI so users can see App health without waiting
    # for an Issue to be created.
    last_cml_status = Column(String(50), nullable=True)        # e.g. APPLICATION_RUNNING / APPLICATION_FAILED
    last_relayops_health = Column(String(30), nullable=True)        # healthy | degraded | unhealthy | failed | stopped | starting | stopping | unknown
    last_checked_at = Column(DateTime, nullable=True)
    last_check_error = Column(Text, nullable=True)             # transport / normalization error if any
    description = Column(Text, nullable=True, default="")
    restart_supported = Column(Boolean, nullable=False, default=False)
    restart_summary = Column(Text, nullable=True, default="")
    owner_contact = Column(String(255), nullable=True, default="")
    support_group_id = Column(Integer, nullable=True)
    support_group_name_snapshot = Column(String(255), nullable=True, default="")
    support_group = Column(String(255), nullable=True, default="")
    is_system = Column(Integer, nullable=False, default=0)  # 0=normal, 1=system-managed(read-only)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product = relationship("Product", back_populates="applications")
    recovery_scenarios = relationship(
        "ApplicationRecoveryScenario",
        back_populates="application",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ApplicationRecoveryScenario(Base):
    """Structured application recovery scenario for batch-A data modeling and future runbook execution."""

    __tablename__ = "application_recovery_scenarios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    scenario_type = Column(String(50), nullable=False, default=ApplicationRecoveryScenarioType.OTHER)
    scenario_name = Column(String(255), nullable=False)
    condition_description = Column(Text, nullable=True, default="")
    action_steps = Column(JSON, nullable=True)
    verification_steps = Column(JSON, nullable=True)
    escalation_target = Column(String(255), nullable=True, default="")
    fallback_owner_type = Column(String(50), nullable=True, default=FallbackOwnerType.CASE_BY_CASE)
    # Owner-email template used by the "send email to owner" action — prefills
    # the Outlook draft (mailto) launched from the issue-handling page. Same
    # shape as job_failure_scenarios.email_template:
    # {"to": str, "cc": str, "subject": str, "body": str}. NULL = no template.
    email_template = Column(JSON, nullable=True)
    is_not_applicable = Column(Boolean, nullable=False, default=False)
    not_applicable_signoff_by = Column(Integer, nullable=True)
    not_applicable_signoff_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    application = relationship("Application", back_populates="recovery_scenarios")


class ApplicationApiCheck(Base):
    """Persisted API validation request owned by an Application.

    The legacy ``ApplicationVerificationPanel`` lets users craft ad-hoc HTTP
    probes while creating an app, but those requests die with the form. This
    model promotes one such probe into a saved, reusable check so a team can
    build up a small Postman-style suite per app and re-run it over time.

    Behaviour mirrors the existing ``/api/apps/verification/run`` proxy: the
    server executes the request on the caller's behalf so the browser never
    issues cross-origin / private-network calls.

    Sensitive headers (Authorization / Cookie) are stored as plain text in
    ``headers``; UI is responsible for masking them in list views and audit
    output. Upgrading to envelope encryption is a future-proof change that
    can land without touching this column's shape.
    """

    __tablename__ = "application_api_checks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(
        Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    # Optional folder name for left-rail grouping. Kept as a denormalized
    # string (vs. a Collection table) so an MVP can ship one table and a
    # later Collection upgrade is purely additive.
    collection_name = Column(String(255), nullable=True, default="")
    name = Column(String(255), nullable=False, default="")
    method = Column(String(10), nullable=False, default="GET")
    url = Column(String(2048), nullable=False, default="")
    headers = Column(JSON, nullable=True)             # dict[str, str]
    body = Column(Text, nullable=True, default="")
    body_is_json = Column(Boolean, nullable=False, default=False)
    timeout_seconds = Column(Integer, nullable=False, default=15)
    # Lightweight assertions — out of MVP we only surface them at run time
    # if populated; expansion to a richer rule DSL stays additive.
    expected_status = Column(Integer, nullable=True)
    expected_body_contains = Column(Text, nullable=True)
    # Provenance — manual / swagger / imported. Helps later dedupe and
    # surfaces "this came from your OpenAPI import" in the UI.
    source = Column(String(20), nullable=False, default="manual")
    source_ref = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    # Cached summary of the most recent run so the list view doesn't need
    # to JOIN against the runs table for every row. Updated by /run.
    last_run_at = Column(DateTime, nullable=True)
    last_run_ok = Column(Boolean, nullable=True)
    last_run_status_code = Column(Integer, nullable=True)
    last_run_duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(Integer, nullable=True)
    updated_by = Column(Integer, nullable=True)

    application = relationship("Application")
    runs = relationship(
        "ApplicationApiCheckRun",
        back_populates="api_check",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_app_api_checks_app", "application_id"),
        Index("ix_app_api_checks_app_collection", "application_id", "collection_name"),
    )


class ApplicationApiCheckRun(Base):
    """Append-only history of one execution of an ``ApplicationApiCheck``.

    Capped by retention at the service layer (keep latest N per check) so
    the table doesn't grow without bound. Response body is truncated to the
    same limit the legacy verification proxy applies.
    """

    __tablename__ = "application_api_check_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_check_id = Column(
        Integer, ForeignKey("application_api_checks.id", ondelete="CASCADE"), nullable=False
    )
    ran_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    ran_by = Column(Integer, nullable=True)
    ok = Column(Boolean, nullable=False, default=False)
    status_code = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=False, default=0)
    response_headers = Column(JSON, nullable=True)
    response_body = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    # Snapshot of the request we actually fired — lets you see what
    # changed when an old run drifts from the current saved values.
    request_url = Column(String(2048), nullable=True)
    request_method = Column(String(10), nullable=True)

    api_check = relationship("ApplicationApiCheck", back_populates="runs")

    __table_args__ = (
        Index("ix_app_api_check_runs_check_ts", "api_check_id", "ran_at"),
    )


class ApplicationHealthCheck(Base):
    """One row per AppChecker probe — feeds the App Health time-series chart.

    Written by AppChecker._persist_app_health alongside the Application
    row's last_* fields. Unlike those columns (which are overwritten every
    tick), rows here are append-only so the UI can render a time window.
    """

    __tablename__ = "application_health_checks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(
        Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    checked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    relayops_health = Column(String(30), nullable=False, default="unknown")
    cml_status = Column(String(50), nullable=True)
    error = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_app_health_app_ts", "application_id", "checked_at"),
    )


class VerificationReport(Base):
    """One snapshot of a 'Run all verification' across every product.

    Generated by ``POST /api/verification/reports/generate``. The endpoint
    fires every active ``ApplicationApiCheck`` server-side (reusing the
    same executor as the per-app run endpoint), appends an
    ``ApplicationApiCheckRun`` per check, and then folds the outcomes
    into a single VerificationReport row. The detail breakdown is kept
    as a denormalised JSON column rather than a join table because:

    * The frontend always reads the full detail blob at once (one
      report -> one Detail dialog), so a 1-N join buys nothing.
    * Reports are immutable once written — no per-row updates are ever
      needed, so denormalisation has zero downside.
    * Project / product / check names are captured at generation time
      so that renaming or deleting an asset later doesn't rewrite
      history (audit-grade snapshot).

    Each entry in ``details`` has roughly the shape::

        {
          "project_id": int | null,
          "project_name": str,
          "product_id": int,
          "product_name": str,
          "application_id": int,
          "check_id": int,
          "check_name": str,
          "method": str,
          "url": str,
          "status": "pass" | "fail",
          "status_code": int | null,
          "duration_ms": int,
          "error": str | null,
          "assertion_reason": str | null,
          "run_id": int | null,
        }
    """

    __tablename__ = "verification_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    generated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    generated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    total_count = Column(Integer, nullable=False, default=0)
    pass_count = Column(Integer, nullable=False, default=0)
    fail_count = Column(Integer, nullable=False, default=0)
    duration_ms = Column(Integer, nullable=False, default=0)
    notes = Column(Text, nullable=True, default="")
    details = Column(JSON, nullable=False, default=list)

    __table_args__ = (
        Index("ix_verification_reports_generated_at", "generated_at"),
    )
