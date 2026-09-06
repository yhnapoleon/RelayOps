"""Scenario post-processing for onboarding drafts.

Two pipeline steps, both payload-mutating:

``normalize_scenario_types``
    Documents invent their own scenario labels ("CML no resource", "API must
    be up"…). The platform's enum is fixed, so: any non-enum scenario_type is
    moved into ``scenario_name`` (the original table label is a *name*, not a
    category), then the scenario is re-classified into the enum — one batched
    light-model LLM call, with a keyword heuristic as the no-LLM fallback and
    ``other`` as the floor. Deterministic validation stays the gate: whatever
    this step outputs is still checked against the enum afterwards.

``inject_email_actions``
    Every scenario gets an owner-notification email: the canonical
    ``{to, cc, subject, body}`` template (same shape the scenario entities
    store) pre-filled from the draft's own facts, plus a "send email" action
    step when the runbook doesn't already have one. Scenario-specific
    contacts (escalation_target, extracted from the document's failure-modes
    tables) become ``to``; the owner goes to ``cc`` so the default
    escalation chain is never lost. Pure code — no LLM.
"""

from __future__ import annotations

import re
from typing import List, Optional, Set, Tuple

from core.logging import get_logger
from core.models.constants import ApplicationRecoveryScenarioType, JobFailureScenarioType
from core.agent.schemas import OnboardingDraftPayload

logger = get_logger(__name__)


# ── type normalization ────────────────────────────────────────────────

# Keyword → enum fallback used when the classify LLM call is unavailable or
# returns garbage. Checked in order; first hit wins.
_JOB_HEURISTICS: Tuple[Tuple[str, str], ...] = (
    # Must precede every other drift rule — "no significant drift" also
    # matches r"drift", so the negated phrasing has to win first.
    (
        r"no\s*(significant|material|meaningful)?\s*drift"
        r"|drift\s*(is\s*)?(not\s*significant|within\s*tolerance)"
        r"|无(显著|明显)?(漂移|drift)",
        "mmp_no_significant_drift",
    ),
    (r"perf(ormance)?\s*drift", "mmp_perf_drift"),
    (r"feature\s*(drift|missing)", "mmp_feature_drift"),
    (r"missing\s*data|data\s*quality", "mmp_data_quality_drift"),
    (r"fairness", "mmp_fairness_risk"),
    (r"drift", "mmp_drift_detected"),
    (r"pending\s*approval", "mmp_run_pending_approval"),
    (r"no\s*resource|not\s*trigger|未触发|漏跑", "not_triggered"),
    (r"ingest|upstream|上游|transformation|prep\b|pipeline|dependency|依赖", "dependency_failed"),
    (r"fail|失败|error|报错", "triggered_but_failed"),
    (r"logic|逻辑", "logic_issue"),
    (r"external|外部", "external_system_issue"),
)
_APP_HEURISTICS: Tuple[Tuple[str, str], ...] = (
    (r"health\s*check|healthcheck|must\s+be\s+up|api.*(down|up)", "healthcheck_failed"),
    (r"offline|down|宕机|不可用", "offline"),
    (r"restart|重启", "restart_required"),
    (r"ray\s*actor", "ray_actor_missing"),
    (r"deploy", "deployment_issue"),
)


def _heuristic_type(kind: str, text: str) -> str:
    rules = _JOB_HEURISTICS if kind == "job" else _APP_HEURISTICS
    low = text.lower()
    for pattern, enum_value in rules:
        if re.search(pattern, low):
            return enum_value
    return "other"


def _classify_with_llm(items: List[dict]) -> Optional[List[str]]:
    """One light-tier call classifying all unknown scenarios at once.
    Returns a type per item (already enum-validated) or None on any failure.
    """
    try:
        from pydantic import BaseModel, Field

        from core.agent.llm import get_chat_model
        from core.agent.prompts import SCENARIO_CLASSIFY_SYSTEM
        from core.agent.structured import invoke_structured

        class _Classified(BaseModel):
            scenario_types: List[str] = Field(default_factory=list)

        lines = []
        for i, item in enumerate(items):
            lines.append(
                f"{i}. kind={item['kind']} | original name: {item['label']!r} | "
                f"trigger condition: {item['condition'][:300]!r}"
            )
        # Use the main (strong) model — scenario typing feeds the runbook, so
        # accuracy matters more than the token saving of the light tier.
        chat = get_chat_model(temperature=0)
        result = invoke_structured(chat, _Classified, [
            ("system", SCENARIO_CLASSIFY_SYSTEM),
            ("human", "\n".join(lines)),
        ])
        if len(result.scenario_types) != len(items):
            return None
        out = []
        for item, chosen in zip(items, result.scenario_types):
            allowed = (JobFailureScenarioType.ALL if item["kind"] == "job"
                       else ApplicationRecoveryScenarioType.ALL)
            out.append(chosen if chosen in allowed else "")
        return out
    except Exception:  # noqa: BLE001 — classification must never fail the draft
        logger.opt(exception=True).warning("onboarding: scenario classify LLM call failed")
        return None


def normalize_scenario_types(payload: OnboardingDraftPayload) -> None:
    """Mutates: non-enum scenario_type → scenario_name + re-classification."""
    pending: List[dict] = []   # {kind, label, condition, scenario}

    for prod in payload.products:
        for job in prod.jobs:
            for sc in job.scenarios:
                t = sc.scenario_type.strip()
                if t and t not in JobFailureScenarioType.ALL:
                    if not sc.scenario_name.strip():
                        sc.scenario_name = t
                    pending.append({"kind": "job", "label": t,
                                    "condition": sc.condition_description, "scenario": sc})
        for app in prod.apps:
            for sc in app.scenarios:
                t = sc.scenario_type.strip()
                if t and t not in ApplicationRecoveryScenarioType.ALL:
                    if not sc.scenario_name.strip():
                        sc.scenario_name = t
                    pending.append({"kind": "app", "label": t,
                                    "condition": sc.condition_description, "scenario": sc})
    if not pending:
        return

    llm_types = _classify_with_llm(pending)
    for i, item in enumerate(pending):
        chosen = (llm_types[i] if llm_types else "") or _heuristic_type(
            item["kind"], f"{item['label']} {item['condition']}")
        item["scenario"].scenario_type = chosen
        payload.warnings.append(
            f"Scenario 「{item['label']}」 is not in the preset categories; "
            f"auto-classified as {chosen} (the original name is kept in the "
            "scenario name — please review)"
        )


# ── owner-email injection ─────────────────────────────────────────────

_EMAIL_STEP_RE = re.compile(r"email|邮件|mail\b", re.IGNORECASE)


def _split_contacts(value: str) -> List[str]:
    return [p for p in re.split(r"[;,/、；]+", value or "") if p.strip()]


def _resolve_recipients(scenario_contact: str, owner_contact: str) -> Tuple[str, str]:
    """(to, cc): scenario's own contacts win the To line; the owner rides
    along in Cc so the default escalation chain is never lost."""
    sc = (scenario_contact or "").strip()
    owner = (owner_contact or "").strip()
    if sc:
        cc = owner if owner and _norm(owner) not in {_norm(p) for p in _split_contacts(sc)} else ""
        return sc, cc
    return owner, ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


# Canonical owner-email body. MUST stay in lockstep with the platform's
# buildDefaultEmailBody / serializeEmailBody / TYPE_OF_REQUEST_BY_SCENARIO_TYPE
# in src/frontend/src/App.tsx, so an onboarding-seeded scenario reads
# identically to one created by hand in the app: same intro/outro, same
# Order Date / Application / … rows, the same {{tokens}} the send-time
# substitution fills, and the same per-scenario-type "Type of Request". The
# rows are a structured Control-M request — the format the handover docs and
# the data-centre team actually use — not a free-text summary.
_EMAIL_BODY_INTRO = ("Please kindly perform the following job requests. "
                     "Approval email is attached above.")
_EMAIL_BODY_OUTRO = ("Thanks,", "{{sender_name}}")
_EMAIL_LABEL_COLUMN = 19
_NBSP = " "   # pad with NBSP (App.tsx does) so the value column survives Outlook

_JOB_EMAIL_ROWS: Tuple[Tuple[str, str], ...] = (
    ("Order Date", "{{date}}"),
    ("Application", ""),
    ("Group", ""),
    ("Table", ""),
    ("Job", "{{job_name}}"),
    ("Type of Request", "Re-Run"),
    ("Financial Impact", "No Impact"),
)
_APP_EMAIL_ROWS: Tuple[Tuple[str, str], ...] = (
    ("Order Date", "{{date}}"),
    ("Application", "{{app_name}}"),
    ("Type of Request", "Investigation"),
    ("Financial Impact", "No Impact"),
)
_TYPE_OF_REQUEST_BY_SCENARIO_TYPE = {
    "not_triggered": "Trigger",
    "triggered_but_failed": "Re-Run",
    "dependency_failed": "Re-Run",
    "mmp_drift_detected": "Investigation",
    "mmp_fairness_risk": "Investigation",
    "mmp_run_pending_approval": "Approval Request",
    "mmp_unapproved_exp_run": "Approval Request",
    "mmp_perf_drift": "Investigation",
    "mmp_feature_drift": "Investigation",
    "mmp_data_quality_drift": "Investigation",
    "mmp_no_significant_drift": "Investigation",
    "pass_threshold": "Investigation",
    "fail_threshold": "Investigation",
    "logic_issue": "Code Fix",
    "external_system_issue": "Investigation",
    "offline": "Restart",
    "healthcheck_failed": "Investigation",
    "ray_actor_missing": "Restart",
    "deployment_issue": "Investigation",
}


def _serialize_email_body(rows: List[Tuple[str, str]]) -> str:
    """Port of App.tsx ``serializeEmailBody``: intro, blank, NBSP-padded
    ``Label:`` rows, blank, outro."""
    if not rows:
        return "\n".join((_EMAIL_BODY_INTRO, "", "", *_EMAIL_BODY_OUTRO))
    width = max(_EMAIL_LABEL_COLUMN, max(len(label) + 1 + 2 for label, _ in rows))
    lines = [f"{(label + ':').ljust(width, _NBSP)}{value}" for label, value in rows]
    return "\n".join((_EMAIL_BODY_INTRO, "", *lines, "", *_EMAIL_BODY_OUTRO))


# The "CHG/TSK Number" row is optional in the platform editor
# (OPTIONAL_EMAIL_ROW_TEMPLATES in emailTemplate.ts) — use the EXACT same label
# so a doc-seeded row round-trips through parse/serialize and the editor doesn't
# offer a duplicate "+ Add" button.
_CHANGE_NUMBER_LABEL = "CHG/TSK Number"


class _ControlMRequest:
    """The Control-M rerun-request values a handover doc supplies for a job/app,
    folded into the email body's ``Application`` / ``Group`` / ``Table`` rows
    (plus an appended CHG/TSK Number row). Values are used verbatim — they are
    real workspace identifiers (``APPL_CML_DEMO`` etc.), not free text."""

    __slots__ = ("application", "group", "table", "change_number")

    def __init__(self, *, application="", group="", table="", change_number=""):
        self.application = (application or "").strip()
        self.group = (group or "").strip()
        self.table = (table or "").strip()
        self.change_number = (change_number or "").strip()

    @property
    def is_empty(self) -> bool:
        return not (self.application or self.group or self.table or self.change_number)


def _default_email_body(kind: str, scenario_type: str,
                        controlm: Optional[_ControlMRequest] = None) -> str:
    """The canonical structured body, with ``Type of Request`` seeded from the
    scenario type (mirrors App.tsx ``buildDefaultEmailBody``) and — when the
    handover doc supplied them — the Control-M ``Application`` / ``Group`` /
    ``Table`` row values and an appended CHG/TSK Number row."""
    base = _APP_EMAIL_ROWS if kind == "app" else _JOB_EMAIL_ROWS
    preset = _TYPE_OF_REQUEST_BY_SCENARIO_TYPE.get((scenario_type or "").strip())
    cm = controlm or _ControlMRequest()
    row_overrides = {
        "Application": cm.application,
        "Group": cm.group,
        "Table": cm.table,
    }
    rows: List[Tuple[str, str]] = []
    for label, value in base:
        if label == "Type of Request" and preset:
            value = preset
        elif row_overrides.get(label):
            # The doc's real identifier replaces the blank/token placeholder.
            value = row_overrides[label]
        rows.append((label, value))
    if cm.change_number:
        rows.append((_CHANGE_NUMBER_LABEL, cm.change_number))
    return _serialize_email_body(rows)


def _fill_email(sc, *, kind: str, project_name: str, entity_label: str,
                owner_contact: str, controlm: Optional[_ControlMRequest] = None) -> None:
    if not sc.email_template.is_empty:
        return  # extraction/reviewer already authored one — never clobber
    to, cc = _resolve_recipients(sc.escalation_target, owner_contact)
    scenario_label = sc.scenario_name.strip() or sc.scenario_type.strip() or "scenario"
    sc.email_template.to = to
    sc.email_template.cc = cc
    sc.email_template.subject = f"[Ops][{project_name}] {entity_label} - {scenario_label}"
    # Body uses the platform's canonical Control-M-request layout (same as a
    # hand-created scenario), NOT a free-text summary — that's what the docs
    # and the data-centre team expect. The {{job_name}}/{{app_name}}/{{date}}/
    # {{sender_name}} tokens are filled by the platform at send time; the
    # Application/Group/Table/CHG values come from the doc's rerun template.
    sc.email_template.body = _default_email_body(kind, sc.scenario_type, controlm)

    if not any(_EMAIL_STEP_RE.search(step) for step in sc.action_steps):
        target = to or "the owner / scheduling team"
        sc.action_steps.append(
            f"Send a notification email to {target} (use this scenario's "
            "pre-filled owner-email template)")


def _owner_email_from_name(name: str) -> str:
    """RelayOps  owner email from name."""
    local = "".join((name or "").split())
    if not local:
        return ""
    try:
        from core.config import get_config

        domain = get_config().email_domain or "example.com"
    except Exception:  # noqa: BLE001 — config trouble shouldn't block the fill
        domain = "example.com"
    return f"{local}@{domain}"


def _in_scope(only_paths: Optional[Set[str]], path: str) -> bool:
    """``only_paths=None`` means the whole payload (ordinary onboarding)."""
    return only_paths is None or path in only_paths


def apply_owner_contact_fallback(
    payload: OnboardingDraftPayload, *, only_paths: Optional[Set[str]] = None
) -> None:
    """Fill any empty job/app ``owner_contact`` with the project owner's
    derived email (``owner_name`` → name@domain). Fill-only — a contact the
    document gave is never overwritten. Adds one warning per fill so the
    reviewer knows it was derived, not quoted.

    ``only_paths`` limits the fill to the given asset paths
    (``products[0].jobs[1]``) — see :func:`inject_email_actions`."""
    owner_email = _owner_email_from_name(payload.project.owner_name)
    if not owner_email:
        return
    owner = payload.project.owner_name.strip()
    for pi, prod in enumerate(payload.products):
        for ji, job in enumerate(prod.jobs):
            if not job.owner_contact.strip() and _in_scope(only_paths, f"products[{pi}].jobs[{ji}]"):
                job.owner_contact = owner_email
                payload.warnings.append(
                    f"Job owner contact not provided in the document; defaulted to "
                    f"the project owner ({owner})'s email {owner_email} — please verify"
                )
        for ai, app in enumerate(prod.apps):
            if not app.owner_contact.strip() and _in_scope(only_paths, f"products[{pi}].apps[{ai}]"):
                app.owner_contact = owner_email
                payload.warnings.append(
                    f"App owner contact not provided in the document; defaulted to "
                    f"the project owner ({owner})'s email {owner_email} — please verify"
                )


def inject_email_actions(
    payload: OnboardingDraftPayload, *, only_paths: Optional[Set[str]] = None
) -> None:
    """Every scenario gets an owner-notification email template + send step.

    ``only_paths`` restricts the fill to the named asset paths
    (``products[0].jobs[1]``). An "add assets to an existing project" draft
    passes the assets its document actually touches: the rest of the payload is
    the project's live state, and back-filling a template — or appending a
    "send the email" action step — onto a runbook the document never mentioned
    would show up as a change the reviewer never asked for."""
    # Fill missing owner contacts from the project owner first, so both the
    # email To/Cc and the offline validator see a resolved owner.
    apply_owner_contact_fallback(payload, only_paths=only_paths)
    project_name = payload.project.name.strip() or payload.project.cml_project_name.strip()
    for pi, prod in enumerate(payload.products):
        for ji, job in enumerate(prod.jobs):
            if not _in_scope(only_paths, f"products[{pi}].jobs[{ji}]"):
                continue
            label = job.cml_job_name or job.control_m_job_name or job.mmp_model_id or f"Job #{ji + 1}"
            cm = _ControlMRequest(
                application=job.control_m_application, group=job.control_m_group,
                table=job.control_m_table, change_number=job.change_number)
            for sc in job.scenarios:
                _fill_email(sc, kind="job", project_name=project_name,
                            entity_label=f"Job {label}", owner_contact=job.owner_contact,
                            controlm=cm)
            # The identifiers now live in each scenario's email body — the single
            # source of truth the reviewer edits. Clear the conduit so it can't
            # drift out of sync with what the body actually says.
            _clear_controlm_job(job)
        for ai, app in enumerate(prod.apps):
            if not _in_scope(only_paths, f"products[{pi}].apps[{ai}]"):
                continue
            label = app.cml_application_name or f"App #{ai + 1}"
            cm = _ControlMRequest(
                application=app.control_m_application, change_number=app.change_number)
            for sc in app.scenarios:
                _fill_email(sc, kind="app", project_name=project_name,
                            entity_label=f"App {label}", owner_contact=app.owner_contact,
                            controlm=cm)
            _clear_controlm_app(app)


def _clear_controlm_job(job) -> None:
    job.control_m_application = ""
    job.control_m_group = ""
    job.control_m_table = ""
    job.change_number = ""


def _clear_controlm_app(app) -> None:
    app.control_m_application = ""
    app.change_number = ""
