"""Transport models for the write-tool flow (propose → confirm).

These are in-memory / wire shapes only — the single persisted artifact is the
``agent_write_proposal`` row (see ``write_proposal_store``); nothing here adds a
business table. A write tool returns a :class:`WriteProposal` (a *diff* the user
must confirm) and never mutates a row itself; the deterministic commit happens
in the confirm endpoint after the user accepts.

"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# kinds the commit dispatcher knows how to apply. S0 ships the issue kinds;
# job_sla (S3) and edit_* / set_member_role (S5) extend it. Adding members to a
# Literal is contract-additive (only-add): older clients still validate the
# kinds they know.
WriteKind = Literal[
    "update_issue_status",
    "resolve_issue",
    "false_positive",
    "record_step",
    "job_sla",
    "edit_project",
    "edit_product",
    "set_member_role",
]


class FieldChange(BaseModel):
    """One field's before→after, with a deterministic rationale (never an
    LLM-invented number — see SLA advisor for the strict case)."""

    field_path: str
    label: str
    old_value: str
    new_value: str
    rationale: str = ""


class WriteProposal(BaseModel):
    """A pending change a write tool produced. ``confirm_token`` is filled in by
    the store when the proposal is persisted; the tool leaves it blank."""

    kind: WriteKind
    entity_type: Literal["issue", "job", "project", "product"]
    entity_id: int
    title: str
    changes: List[FieldChange] = Field(default_factory=list)
    impact_note: str = ""
    requires_resolution: bool = False
    confirm_token: str = ""
    rbac_ok: bool = True
    rbac_reason: str = ""
    # "direct" = a single deterministic service write; "version_flow" = the
    # change enters the project draft/version flow (job cron/SLA), so the UI must
    # tell the user it is not effective immediately.
    commit_path: Literal["direct", "version_flow"] = "direct"


class SlaRecommendation(BaseModel):
    """Output of the deterministic SLA advisor (Phase S3). ``recommended_*`` are
    only set when the computation is safe (enough samples, valid cron); otherwise
    they stay ``None`` and ``warnings`` explains why. Defined here so the schema
    is stable, even though the advisor lands in S3."""

    job_id: int
    current_cron: str = ""
    current_threshold_minutes: int = 0
    recommended_cron: Optional[str] = None
    recommended_threshold_minutes: Optional[int] = None
    recommended_sla_preset: Optional[str] = None
    observed_interval_minutes: float = 0.0
    sample_run_count: int = 0
    croniter_valid: bool = False
    method: str = ""
    warnings: List[str] = Field(default_factory=list)
