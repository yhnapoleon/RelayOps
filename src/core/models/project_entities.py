"""Project domain models."""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from core.models.database import Base
from core.models.constants import ProjectLifecycleStatus, ProjectStatus, ProjectVersionStatus


class Project(Base):
    """
    Project model — top-level grouping owned by a Business Owner.

    Version control and the submit-draft / handover lifecycle live here (a
    project usually maps to a single repo). Each project owns a chain of
    ProjectVersion snapshots that freeze the entire project scope — all
    products plus their jobs/apps/scenarios — for review and rollback.

    Attributes:
        id: Auto-increment primary key.
        name: Project name.
        description: Brief description.
        owner_id: FK to users.id (the Business Owner).
        created_at: Creation timestamp.
        updated_at: Last update timestamp.
    """

    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True, default="")
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    owner_group_id = Column(Integer, nullable=True)
    owner_group_name_snapshot = Column(String(255), nullable=True, default="")
    is_system = Column(Integer, nullable=False, default=0)  # 0=normal, 1=system-managed(read-only)
    # CML v2 binding lives at the project level: every Job/Application under
    # this Ops Project monitors assets inside the named CML project. Inheriting
    # from the project mirrors the CML v2 invocation flow:
    # locate project first, then look up jobs/apps within it.
    cml_project_name = Column(String(255), nullable=True, default="")
    cml_project_id = Column(String(64), nullable=True)
    cml_binding_error = Column(Text, nullable=True)
    # Optional MMP project binding — stores the CML directory's
    # ``project_repo_name`` (e.g. "demo-inventory-forecast@regional-demand").
    # Independent of cml_project_name above; a Ops Project may carry one, the
    # other, both, or neither. When set, the Job-level MMP picker under this
    # project defaults to filtering on this binding (with a UI override to see
    # the full workspace directory).
    mmp_project_id = Column(String(255), nullable=True, default="")
    # User-supplied link to a production statistics / monitoring dashboard.
    # A project usually maps to one repo, so a single prod-stat URL covers all
    # of its assets. Display-only — not probed by any checker.
    prod_stat_url = Column(String(512), nullable=True, default="")
    # Version-control / handover lifecycle (moved up from product level).
    status = Column(String(50), nullable=False, default=ProjectStatus.DRAFT)
    lifecycle_status = Column(String(50), nullable=False, default=ProjectLifecycleStatus.DRAFTING)
    current_draft_version_id = Column(Integer, ForeignKey("project_versions.id"), nullable=True)
    current_approved_version_id = Column(Integer, ForeignKey("project_versions.id"), nullable=True)
    latest_version_number = Column(String(50), nullable=False, default="1")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    products = relationship(
        "Product",
        back_populates="project",
        cascade="all, delete-orphan",
        # See product_entities.py — DB ondelete=CASCADE already removes
        # children; pairing ORM cascade with passive_deletes avoids the
        # YugabyteDB "tuple concurrently deleted" race.
        passive_deletes=True,
    )
    versions = relationship(
        "ProjectVersion",
        back_populates="project",
        cascade="all, delete-orphan",
        foreign_keys="ProjectVersion.project_id",
        passive_deletes=True,
    )
    current_draft_version = relationship(
        "ProjectVersion",
        foreign_keys=[current_draft_version_id],
        post_update=True,
    )
    current_approved_version = relationship(
        "ProjectVersion",
        foreign_keys=[current_approved_version_id],
        post_update=True,
    )


class ProjectVersion(Base):
    """Immutable-ish review unit for project-level handover approval, export, and audit replay."""

    __tablename__ = "project_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "version_number", name="uq_project_versions_project_version"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    version_number = Column(String(50), nullable=False)
    version_status = Column(String(50), nullable=False, default=ProjectVersionStatus.DRAFT)
    change_summary = Column(Text, nullable=True, default="")
    snapshot_json = Column(JSON, nullable=True)
    completeness_summary_json = Column(JSON, nullable=True)
    submitted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    derived_from_version_id = Column(Integer, ForeignKey("project_versions.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="versions", foreign_keys=[project_id])
    derived_from_version = relationship("ProjectVersion", remote_side=[id], foreign_keys=[derived_from_version_id])


class ProjectMember(Base):
    """
    Project membership — a user's role *within a single project*.

    Historically the user's role lived on ``User.role`` (one global value)
    and the project owner was filtered out of the members list. That
    conflated two distinct concepts: a person's login identity (admin /
    relayops_member / regular user, used for global tab visibility & top-level
    RBAC) and their role inside a particular project (business owner of
    the project / relayops member working on it). This row carries the
    project-scoped role; ``User.role`` is now strictly the global
    identity and is never overwritten by project membership operations.

    Invariant: every project has exactly one row here with
    ``role='business_owner'`` whose ``user_id`` matches
    ``Project.owner_id``. The "creator is a member" row is inserted by
    ``project_service.create_project``; a backfill in
    ``system_seed._ensure_owner_member_rows`` covers legacy data.

    Attributes:
        id: Auto-increment primary key.
        project_id: FK to projects.id.
        user_id: FK to users.id (the member being added).
        role: Per-project role — 'business_owner' (the owner, one per
            project) or 'relayops_member' (everyone else). Default
            'relayops_member' so adds are safe when the column is omitted.
        added_by: FK to users.id (who added this member).
        created_at: When the membership was created.
    """

    __tablename__ = "project_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False, default="relayops_member")
    support_group_id = Column(Integer, nullable=True)
    support_group_name_snapshot = Column(String(255), nullable=True, default="")
    added_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProjectSupportGroup(Base):
    """Binding table between projects and support groups."""

    __tablename__ = "project_support_groups"
    __table_args__ = (
        UniqueConstraint("project_id", "support_group_id", name="uq_project_support_groups_binding"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    support_group_id = Column(Integer, nullable=False)
    support_group_name_snapshot = Column(String(255), nullable=True, default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
