"""Issue domain models."""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from core.models.database import Base
from core.models.constants import IssueStatus


class Issue(Base):
    """
    Issue model — tracks problems, reviews, and alerts.

    Used for:
    - Handover review requests (Business Owner → Platform Admin)
    - Scope change reviews
    - Automated alerts (job failures, app offline, etc.)

    Attributes:
        id: Auto-increment primary key.
        type: Issue type (handover_review, job_failed, etc.).
        status: Issue status (open, in_progress, resolved, closed).
        title: Brief description of the issue.
        description: Detailed description.
        product_id: FK to products.id (optional, for product-related issues).
        job_id: FK to jobs.id (optional, for job-related issues).
        app_id: FK to applications.id (optional, for app-related issues).
        created_by: FK to users.id (who created/triggered the issue).
        assignee_id: FK to users.id (who is assigned to handle it).
        resolution_description: How the issue was resolved.
        rejection_reason: Reason for rejection (handover reviews).
        sla_deadline: When the issue should be resolved by.
        resolved_at: When the issue was resolved.
        created_at: Creation timestamp.
        updated_at: Last update timestamp.
    """

    __tablename__ = "issues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False, default=IssueStatus.OPEN)
    title = Column(String(512), nullable=False)
    description = Column(Text, nullable=True, default="")

    # Related entities (optional, depends on issue type).
    # Ops monitoring issues (job_failed, app_offline, mmp_drift, …) stay keyed
    # to the product/job/app. Handover-review issues are project-scoped and
    # carry project_id + project_version_id instead.
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=True)
    product_version_id = Column(Integer, ForeignKey("product_versions.id", ondelete="SET NULL"), nullable=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    project_version_id = Column(Integer, ForeignKey("project_versions.id", ondelete="SET NULL"), nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True)
    app_id = Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=True)

    # People involved
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    support_group_id = Column(Integer, nullable=True)
    support_group_name = Column(String(255), nullable=True, default="")
    owner_group_id = Column(Integer, nullable=True)
    owner_group_name = Column(String(255), nullable=True, default="")
    assigned_via = Column(String(50), nullable=True, default="")

    # Resolution details
    resolution_description = Column(Text, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    selected_scenario_type = Column(String(50), nullable=True)
    selected_scenario_name = Column(String(255), nullable=True)
    action_summary_json = Column(JSON, nullable=True)
    resolution_summary_json = Column(JSON, nullable=True)

    # Optional deep link to an external system where this issue is actioned
    # (e.g. the MMP web-UI model page for pending-approval / pending-review
    # issues). When set, the My Actions card offers a "jump out" button
    # instead of (or alongside) the Ops runbook workspace.
    external_url = Column(Text, nullable=True)

    # Per-event dedup key. For automated monitoring issues this is the
    # underlying occurrence id (e.g. the CML cml_run_id) so we create exactly
    # one issue per distinct failure/miss event instead of one per open issue.
    # NULL for issue types that still dedup on "any open issue of this type".
    dedup_key = Column(String(255), nullable=True, index=True)

    # For MMP issues: the MMP production run id that triggered this alert. A
    # lightweight pointer (not a body snapshot) so the issue detail can trace
    # back to the exact run in the live MMP response, instead of guessing the
    # latest one. NULL for non-MMP issues / issues raised before this existed.
    mmp_run_id = Column(Integer, nullable=True)

    # SLA tracking
    sla_deadline = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    product = relationship("Product", foreign_keys=[product_id])
    product_version = relationship("ProductVersion", foreign_keys=[product_version_id])
    project = relationship("Project", foreign_keys=[project_id])
    project_version = relationship("ProjectVersion", foreign_keys=[project_version_id])
    job = relationship("Job", foreign_keys=[job_id])
    application = relationship("Application", foreign_keys=[app_id])


class UserIssuePreference(Base):
    """
    User issue-type preference used as an auto-assignment tie-breaker.

    Each row means: "when issue counts are tied, this user prefers handling
    this issue type."
    """

    __tablename__ = "user_issue_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "issue_type", name="uq_user_issue_preference_user_type"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    issue_type = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
