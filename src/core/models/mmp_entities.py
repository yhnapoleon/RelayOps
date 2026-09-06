"""MMP domain models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, Text

from core.models.database import Base


class MmpDriftSnapshot(Base):
    """One observation of an MMP-monitored model's drift state.

    Written by MmpChecker on every successful drift read (true OR false).
    Ops builds its own drift timeline by querying this table — we never call
    MMP for run history because the upstream /runs endpoint is broken (500)
    and MMP does not expose a state log.

    Snapshots are NOT written when the cycle fails (INCONCLUSIVE) so the
    timeline stays clean.

    Attributes:
        id: Auto-increment primary key.
        job_id: FK to jobs.id. Cascades on delete.
        cml_model_id: CML's integer model id at the time of observation
            (denormalized — kept so a timeline survives Job rebinding).
        drifted: Value of model.attention_required.model_drifted.status
            from MMP at observation time.
        drift_details: Human-readable description from MMP (the
            ``.description`` sibling of ``.status``). Nullable.
        observed_at: When this snapshot was recorded by Ops.
    """

    __tablename__ = "mmp_drift_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cml_model_id = Column(Integer, nullable=True)
    drifted = Column(Boolean, nullable=False)
    drift_details = Column(Text, nullable=True)
    observed_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_mmp_drift_snapshots_job_time", "job_id", "observed_at"),
    )
