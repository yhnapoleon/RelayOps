"""Reusable scenario-template models.

A ScenarioTemplate snapshots the authoring fields of a Job failure scenario
or an App recovery scenario (including the owner-email template) so a team
can save a frequently-used scenario once, name it, and one-click refill it
into a new scenario while creating assets.

Templates are intentionally NOT tied to a specific Job/App — they're a
workspace-wide library keyed only by ``asset_kind`` (job | app) and
``scenario_type``. The raw authoring fields are stored verbatim in the
``payload`` JSON blob (the same shape the frontend scenario form uses), so
applying a template is a pure client-side form fill with no server-side
field mapping to keep in sync as the scenario form evolves.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, JSON, String, Text

from core.models.database import Base


class ScenarioTemplate(Base):
    """A named, reusable Job/App scenario template.

    Attributes:
        id: Auto-increment primary key.
        asset_kind: "job" or "app" — which scenario form this template fills.
        name: Human-friendly template name (unique-ish per asset_kind; not
            enforced at the DB level so two teams can pick the same name).
        scenario_type: The scenario_type the template was authored for
            (e.g. "not_triggered", "offline"). Used to surface the most
            relevant templates first in the picker.
        description: Optional free-text note describing when to use it.
        payload: The scenario authoring fields, verbatim from the frontend
            form state (condition_description, diagnostic/action/verification
            steps, escalation_target, fallback_owner_type, threshold_*, and
            the email_template). Shape mirrors JobScenarioFormState /
            AppScenarioFormState minus the per-instance ``id``.
        created_by: user_id of the author (nullable for back-compat).
        is_active: Soft-hide flag so a template can be retired without losing
            history.
        created_at / updated_at: Timestamps.
    """

    __tablename__ = "scenario_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_kind = Column(String(10), nullable=False, default="job")  # job | app
    name = Column(String(255), nullable=False, default="")
    scenario_type = Column(String(50), nullable=True, default="")
    description = Column(Text, nullable=True, default="")
    payload = Column(JSON, nullable=False, default=dict)
    created_by = Column(Integer, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_scenario_templates_kind", "asset_kind"),
        Index("ix_scenario_templates_kind_type", "asset_kind", "scenario_type"),
    )
