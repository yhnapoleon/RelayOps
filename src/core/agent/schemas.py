"""Draft payload schemas for the onboarding agent.

These mirror the platform's create payloads (ProjectCreate / JobCreate /
AppCreate / *ScenarioCreate) but every field is optional — extraction must be
able to say "the document didn't give this" instead of inventing values. Each
draft node carries a ``source_quote`` so the reviewer can check a value
against the original document.

The payload is stored as JSON on ``OnboardingDraft.payload`` and re-parsed
through these models on every save, so the JSON shape is always schema-valid.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class EmailTemplateDraft(BaseModel):
    """Owner-notification email — canonical 4-field shape shared with
    job/app scenario entities (see product_service._EMAIL_TEMPLATE_FIELDS)."""

    to: str = ""
    cc: str = ""
    subject: str = ""
    body: str = ""

    @property
    def is_empty(self) -> bool:
        return not any((self.to.strip(), self.cc.strip(),
                        self.subject.strip(), self.body.strip()))


class JobScenarioDraft(BaseModel):
    """Maps to JobFailureScenarioCreate."""

    scenario_type: str = ""
    scenario_name: str = ""
    condition_description: str = ""
    diagnostic_steps: List[str] = Field(default_factory=list)
    action_steps: List[str] = Field(default_factory=list)
    verification_steps: List[str] = Field(default_factory=list)
    escalation_target: str = ""
    email_template: EmailTemplateDraft = Field(default_factory=EmailTemplateDraft)
    source_quote: str = ""


class AppScenarioDraft(BaseModel):
    """Maps to ApplicationRecoveryScenarioCreate."""

    scenario_type: str = ""
    scenario_name: str = ""
    condition_description: str = ""
    action_steps: List[str] = Field(default_factory=list)
    verification_steps: List[str] = Field(default_factory=list)
    escalation_target: str = ""
    email_template: EmailTemplateDraft = Field(default_factory=EmailTemplateDraft)
    source_quote: str = ""


class JobDraft(BaseModel):
    """Maps to JobCreate. Jobs have no name column — they are identified by
    their CML / Control-M names, so at least one of those should be present."""

    cml_project_name: str = ""
    cml_job_name: str = ""
    control_m_job_name: str = ""
    control_m_cron: str = ""
    schedule_cron: str = ""
    mmp_project_id: str = ""
    mmp_model_id: str = ""
    owner_contact: str = ""
    description: str = ""
    dependency_notes: str = ""
    scenarios: List[JobScenarioDraft] = Field(default_factory=list)
    source_quote: str = ""
    # Control-M rerun-request identifiers lifted from the handover doc's
    # "Example template / Sent to … for execution" block (Application =
    # APPL_CML_*, Group = GRP_*, Table = TBL_*, CHG/TSK Number = CHG…). They are
    # a per-job extraction conduit: the enrichment step folds them into each of
    # this job's scenario email-template body rows and then clears them, so the
    # email body — not these fields — is where the values live afterwards.
    control_m_application: str = ""
    control_m_group: str = ""
    control_m_table: str = ""
    change_number: str = ""


class AppDraft(BaseModel):
    """Maps to AppCreate."""

    cml_project_name: str = ""
    cml_application_name: str = ""
    cml_subdomain: str = ""
    cml_app_type: str = "generic"
    application_url: str = ""
    health_check_url: str = ""
    owner_contact: str = ""
    description: str = ""
    scenarios: List[AppScenarioDraft] = Field(default_factory=list)
    source_quote: str = ""
    # Same extraction conduit as JobDraft — an app's recovery-request email
    # rarely carries Group/Table, but a doc may name an explicit Control-M
    # Application id and a CHG/TSK number. Folded into the scenario email bodies
    # then cleared (see core.agent.scenario_enrich.inject_email_actions).
    control_m_application: str = ""
    change_number: str = ""


class ProductDraft(BaseModel):
    """Maps to product_service.create — products group jobs/apps."""

    name: str = ""
    jobs: List[JobDraft] = Field(default_factory=list)
    apps: List[AppDraft] = Field(default_factory=list)


class ProjectDraft(BaseModel):
    """Maps to ProjectCreate."""

    name: str = ""
    description: str = ""
    # "Project Owner" from the handover MetaData (a person's display name, e.g.
    # "Alex Morgan"). Not a platform field — it's the fallback source for a
    # job/app owner_contact the document didn't spell out (→ name@example.com).
    owner_name: str = ""
    cml_project_name: str = ""
    mmp_project_id: str = ""
    prod_stat_url: str = ""


class OnboardingDraftPayload(BaseModel):
    """Everything the preview table edits and the submit pipeline consumes."""

    project: ProjectDraft = Field(default_factory=ProjectDraft)
    products: List[ProductDraft] = Field(default_factory=list)
    # Field-level "the document didn't give this" notes from extraction.
    warnings: List[str] = Field(default_factory=list)


# ── Validation / clarification report ────────────────────────────────


class FieldIssue(BaseModel):
    field_path: str = ""
    message: str = ""


class ClarificationOption(BaseModel):
    """One pre-verified candidate answer (e.g. a CML project the Ops service
    identity can actually see). ``value`` is what gets written back;
    ``detail`` is display-only context (owner / match quality / model count)."""

    value: str = ""
    detail: str = ""


class Clarification(BaseModel):
    """A question the agent proactively asks the reviewer. ``field_path``
    points into the payload (e.g. ``products[0].apps[1].application_url``)
    so the answer can be written back deterministically — no LLM involved.

    ``kind`` tells the preview UI which input to render: ``text`` (plain
    input), ``cml_project`` (type-to-search combobox against the live CML
    picker endpoint), ``mmp_project`` (MMP directory dropdown). ``options``
    are server-verified candidates the enrichment step matched — the reviewer
    confirms, the agent never silently auto-binds."""

    id: str = ""
    field_path: str = ""
    question: str = ""
    reason: str = ""
    kind: str = "text"
    options: List[ClarificationOption] = Field(default_factory=list)


class ValidationReport(BaseModel):
    errors: List[FieldIssue] = Field(default_factory=list)      # block submit
    warnings: List[FieldIssue] = Field(default_factory=list)    # advisory
    clarifications: List[Clarification] = Field(default_factory=list)

    @property
    def ok_to_submit(self) -> bool:
        return not self.errors


class ClarificationAnswer(BaseModel):
    """One answered question, sent back by the preview page."""

    id: str = ""
    field_path: str = ""
    answer: str = ""


# ── "Add assets to an existing project" diff ─────────────────────────
#
# A draft can target an existing Ops project instead of creating a new one
# (``OnboardingDraft.target_project_id``). The diff lives *beside* the payload
# rather than inside it — the payload's JSON Schema is handed to the LLM for
# structured extraction, so bookkeeping fields in there would be noise the
# model is invited to hallucinate. Keying by ``field_path`` matches how the
# validation report and clarifications already address payload nodes.

CHANGE_NEW = "new"
CHANGE_UPDATE = "update"
CHANGE_UNCHANGED = "unchanged"


class NodeDiff(BaseModel):
    """How one draft node compares to the platform row it maps onto.

    ``existing_id`` is the platform row id (None = this node will be created).
    ``previous`` holds the value stored today for exactly the fields this draft
    would change — list fields joined with newlines so the map stays flat.
    """

    existing_id: Optional[int] = None
    change: str = CHANGE_NEW
    previous: Dict[str, str] = Field(default_factory=dict)


class ProjectDiff(BaseModel):
    """Diff annotations for a whole update draft, keyed by field_path
    (``project``, ``products[0]``, ``products[0].jobs[1]``,
    ``products[0].apps[0].scenarios[2]`` …).

    Always *derived*: recomputed from a live project snapshot on every
    extraction, save and refine — and once more at update time — so a reviewer
    edit or an LLM re-order can never leave a node pointing at the wrong row.
    """

    project_id: int = 0
    project_name: str = ""
    nodes: Dict[str, NodeDiff] = Field(default_factory=dict)
