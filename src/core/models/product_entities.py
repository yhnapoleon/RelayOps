"""Product domain models."""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from core.models.database import Base
from core.models.constants import ProductLifecycleStatus, ProductStatus, ProductVersionStatus


class Product(Base):
    """
    Product model — belongs to a Project.

    Attributes:
        id: Auto-increment primary key.
        project_id: FK to projects.id.
        name: Product name.
        status: Legacy workflow status kept for backward compatibility.
        lifecycle_status: Product-level lifecycle status detached from review state.
        created_at: Creation timestamp.
        updated_at: Last update timestamp.
    """

    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    status = Column(String(50), nullable=False, default=ProductStatus.DRAFT)
    lifecycle_status = Column(String(50), nullable=False, default=ProductLifecycleStatus.DRAFTING)
    current_draft_version_id = Column(Integer, ForeignKey("product_versions.id"), nullable=True)
    current_approved_version_id = Column(Integer, ForeignKey("product_versions.id"), nullable=True)
    latest_version_number = Column(String(50), nullable=False, default="1")
    is_system = Column(Integer, nullable=False, default=0)  # 0=normal, 1=system-managed(read-only)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="products")
    # passive_deletes=True on these three: every child table has
    # ondelete=CASCADE at the DB level, so letting SQLAlchemy *also*
    # emit its own DELETE for the children causes "tuple concurrently
    # deleted" on YugabyteDB (the row is already gone by the time the
    # ORM DELETE arrives). Trust the DB cascade.
    versions = relationship(
        "ProductVersion",
        back_populates="product",
        cascade="all, delete-orphan",
        foreign_keys="ProductVersion.product_id",
        passive_deletes=True,
    )
    current_draft_version = relationship(
        "ProductVersion",
        foreign_keys=[current_draft_version_id],
        post_update=True,
    )
    current_approved_version = relationship(
        "ProductVersion",
        foreign_keys=[current_approved_version_id],
        post_update=True,
    )
    jobs = relationship(
        "Job",
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    applications = relationship(
        "Application",
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ProductVersion(Base):
    """Immutable-ish review unit for handover approval and future export/audit replay."""

    __tablename__ = "product_versions"
    __table_args__ = (
        UniqueConstraint("product_id", "version_number", name="uq_product_versions_product_version"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    version_number = Column(String(50), nullable=False)
    version_status = Column(String(50), nullable=False, default=ProductVersionStatus.DRAFT)
    change_summary = Column(Text, nullable=True, default="")
    snapshot_json = Column(JSON, nullable=True)
    completeness_summary_json = Column(JSON, nullable=True)
    submitted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    derived_from_version_id = Column(Integer, ForeignKey("product_versions.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product = relationship("Product", back_populates="versions", foreign_keys=[product_id])
    derived_from_version = relationship("ProductVersion", remote_side=[id], foreign_keys=[derived_from_version_id])
