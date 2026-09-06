"""Identity-aware UI knowledge for the guide branch.

Two layers, deliberately separated so the narrative copy can never drift past
what the code actually exposes:

* **Fact layer** (derived from code) — ``tabs_for_role`` mirrors the frontend's
  per-role tab gating; ``explain_field`` derives a field's required/enum/linkage
  facts from the onboarding create schema + ``validate.py``. These are the
  ground truth the assistant must not contradict.
* **Narrative layer** (hand-written) — ``TAB_NARRATIVES`` / ``FIELD_NARRATIVES``
  carry the step-by-step prose (loaded from the shipped knowledge base).

Anti-hallucination boundary: when a tab/field has no recorded narrative the
explain helpers answer "该功能我没有记录" rather than inventing UI behaviour.
The drift test (``tests/test_ui_capability.py``) keeps the two layers honest —
every narrative key must resolve to a real tab / a real schema field.

No new dependency; reuses the onboarding ``field_path`` vocabulary so a guide
explanation and an onboarding clarification name the same field the same way.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional

from core.models.user import UserRole, normalize_role
from core.agent.schemas import (
    AppDraft,
    AppScenarioDraft,
    JobDraft,
    JobScenarioDraft,
    ProductDraft,
    ProjectDraft,
)
from core.agent.validate import _APP_TYPES
from core.models.constants import (
    ApplicationRecoveryScenarioType,
    JobFailureScenarioType,
)

# ── Fact layer: tab visibility per role ──────────────────────────────────
#
# Mirrors src/frontend/src/App.tsx `availableTabs` (the single source the UI
# uses). Encoded as {tab_key: frozenset(roles)} so a role check is a membership
# test. Roles are the *normalized* global roles (business_owner → regular_user).
# Keep this in lockstep with App.tsx; the narrative drift test guards the keys.

_ALL_ROLES = frozenset({UserRole.ADMIN, UserRole.RELAYOPS_MEMBER, UserRole.REGULAR_USER})
_RELAYOPS_ADMIN = frozenset({UserRole.ADMIN, UserRole.RELAYOPS_MEMBER})
_ADMIN = frozenset({UserRole.ADMIN})
_Ops = frozenset({UserRole.RELAYOPS_MEMBER})

TAB_VISIBILITY: Dict[str, frozenset] = {
    "dashboard": _ALL_ROLES,
    "projects": _ALL_ROLES,
    # My Actions + its workbench are a stable fixture for every authenticated
    # user (the nav no longer flips on role); content is whatever is assigned
    # to you. A project-level relayops_member works their claimed issues here too.
    "actions": _ALL_ROLES,
    "workbench": _ALL_ROLES,
    # Ops-only standalone page; admins get the equivalent "Resolved" view
    # inside the admin panel's Issue Management instead.
    "resolved-issues": _Ops,
    # Open Issues board: a stable fixture for every user. Global Ops/admins see
    # the whole platform; other users see their projects' open issues (claim is
    # gated to projects where they hold the Ops role — see open-issues.md).
    "open-issues": _ALL_ROLES,
    "my-schedule": _Ops,
    "relayops-admin-handover": _ADMIN,
    "relayops-admin-issues": _ADMIN,
    # Duty roster: Ops members reach it as a standalone "Schedules" page; admins
    # reach the same view inside the Ops Admin Panel.
    "schedules": _RELAYOPS_ADMIN,
    "relayops-admin-users": _ADMIN,
    "verification": _RELAYOPS_ADMIN,
    "analytics": _ALL_ROLES,
    "ai-assistant": _ALL_ROLES,
}


def tabs_for_role(role: Optional[str]) -> List[str]:
    """Tab keys visible to ``role`` (normalized), in the UI's display order.

    Source of truth = ``TAB_VISIBILITY`` (mirrors App.tsx). Note relayops_member is
    elevated to ~admin at the *service* layer, but UI tabs still gate the admin
    panel to admins only — so a guide answer truthfully reflects what the user
    actually sees, not what the service would allow."""
    norm = normalize_role(role)
    return [tab for tab, roles in TAB_VISIBILITY.items() if norm in roles]


# ── Fact layer: onboarding form fields ───────────────────────────────────
#
# form_key → the draft schema whose fields a guide explanation can address.
# Reusing the onboarding schemas means a guide field name == an onboarding
# clarification field_path == an assist_fill setter target (one vocabulary).

FORM_SCHEMAS = {
    "project": ProjectDraft,
    "product": ProductDraft,
    "job": JobDraft,
    "app": AppDraft,
    "job_scenario": JobScenarioDraft,
    "app_scenario": AppScenarioDraft,
}

# Required leaf fields per form (mirrors validate.validate_payload's hard
# errors). Linkage groups capture "one-of" rules the validator enforces.
_REQUIRED: Dict[str, frozenset] = {
    "project": frozenset({"name"}),
    "product": frozenset({"name"}),
    "job": frozenset(),  # satisfied via the cml/control_m/mmp one-of linkage
    "app": frozenset({"cml_application_name"}),
    "job_scenario": frozenset(),
    "app_scenario": frozenset(),
}

# field_path → allowed enum values (derived from constants / validate, never
# hand-listed) so an explanation can offer the exact valid set.
_ENUMS: Dict[str, List[str]] = {
    "app::cml_app_type": list(_APP_TYPES),
    "job_scenario::scenario_type": list(JobFailureScenarioType.ALL),
    "app_scenario::scenario_type": list(ApplicationRecoveryScenarioType.ALL),
}

# field_path → linkage note (the dynamic/conditional rules in validate.py).
_LINKAGE: Dict[str, str] = {
    "job::cml_job_name": "A Job needs at least one of cml_job_name / control_m_job_name / mmp_model_id.",
    "job::control_m_job_name": "A Job needs at least one of cml_job_name / control_m_job_name / mmp_model_id.",
    "job::mmp_model_id": "A Job needs at least one of cml_job_name / control_m_job_name / mmp_model_id; mmp_project_id alone is not enough.",
    "app::application_url": "The app's access entry: provide either application_url or cml_subdomain (a subdomain can auto-resolve the URL).",
    "app::cml_subdomain": "The app's access entry: provide either cml_subdomain or application_url.",
}


def _schema_has_field(form_key: str, field_path: str) -> bool:
    """True iff ``field_path`` is a declared field on the form's schema model.
    Only leaf attribute names are checked (no index navigation) — the guide
    explains a field kind, not a specific row."""
    model = FORM_SCHEMAS.get(form_key)
    if model is None:
        return False
    return field_path in getattr(model, "model_fields", {})


# ── Narrative layer (derived from the KB — docs/kb/*.md, single source) ───
#
# Historically hand-written here; now derived from the structured KB so the
# guide narrative, the page assistant, and the FTS index all read one source
# Shape is unchanged ({title, summary, steps}) so callers and the
# drift test (TAB_NARRATIVES keys == TAB_VISIBILITY) are unaffected. Derived at
# import from kb_loader (which does NOT import this module — no import cycle).

_STEP_LINE = re.compile(r"^\s*(?:\d+\.|[-*])\s*(.+?)\s*$")


def _derive_tab_narratives() -> Dict[str, Dict]:
    from core.agent import kb_loader

    out: Dict[str, Dict] = {}
    for page in kb_loader.load_pages():
        purpose = page.section("purpose")
        summary = ""
        if purpose and purpose.text:
            # First paragraph of the purpose section = one-line summary.
            summary = re.split(r"\n\s*\n", purpose.text.strip())[0].replace("\n", " ").strip()
        steps: List[str] = []
        for flow in page.sections("flow"):
            for line in flow.text.splitlines():
                m = _STEP_LINE.match(line)
                if m:
                    steps.append(m.group(1).strip())
        out[page.tab] = {"title": page.title, "summary": summary, "steps": steps}
    return out


TAB_NARRATIVES: Dict[str, Dict] = _derive_tab_narratives()

# form_key → {field_path: 讲解词}. Curated; the drift test asserts every
# field_path here is a real schema field.
FIELD_NARRATIVES: Dict[str, Dict[str, str]] = {
    "project": {
        "name": "Project name. Required — it identifies the Ops workspace.",
        "description": "Project description, so scope is clear at handover time.",
        "cml_project_name": "Bound CML project name (used for run monitoring); can be left blank and filled in later.",
        "mmp_project_id": "Bound MMP project id (fill in when model monitoring is involved).",
    },
    "job": {
        "cml_job_name": "CML-side job name, used to monitor runs; provide at least one of control_m/mmp.",
        "control_m_job_name": "Control-M schedule name; leave blank if not documented.",
        "schedule_cron": "Expected schedule cron (monitoring uses it to detect missed runs); fill from the real Control-M schedule.",
        "mmp_model_id": "MMP model id; an mmp_project_id without a model is not a complete setup.",
        "owner_contact": "Owner contact (project POC, the default Ops escalation target).",
    },
    "app": {
        "cml_application_name": "The app's CML application name. Required.",
        "cml_app_type": "Application type; see the enum for values (fastapi/runtime/ray/generic).",
        "application_url": "Access URL; provide either this or cml_subdomain.",
        "cml_subdomain": "CML subdomain; the access URL can be resolved from it.",
        "owner_contact": "Owner contact.",
    },
    "job_scenario": {
        "scenario_type": "Failure scenario type; see the enum for values.",
    },
    "app_scenario": {
        "scenario_type": "Recovery scenario type; see the enum for values.",
    },
}

_NO_RECORD = "I have no record of that feature."


# ── Explain helpers (fact + narrative merged) ────────────────────────────


def _norm_tab(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _resolve_tab_key(raw: str) -> Optional[str]:
    """Resolve a tab key OR the human display name / a paraphrase of it to a real
    tab key. The model often passes the label it saw on the card ("My Projects",
    "Admin Panel · User Management") or its own缩写 ("ai_assistant") instead of
    the internal key — accept all of them. Exact (normalized) key/title match
    first, else the best fuzzy match ≥ 0.7 (so a page the platform doesn't have
    still resolves to None → honest "unknown")."""
    norm = _norm_tab(raw)
    if not norm:
        return None
    for key in TAB_VISIBILITY:  # exact on normalized key or title
        if _norm_tab(key) == norm or _norm_tab(TAB_NARRATIVES.get(key, {}).get("title", "")) == norm:
            return key
    best_key, best_ratio = None, 0.0
    for key in TAB_VISIBILITY:
        for cand in (key, TAB_NARRATIVES.get(key, {}).get("title", "")):
            if cand:
                ratio = SequenceMatcher(None, norm, _norm_tab(cand)).ratio()
                if ratio > best_ratio:
                    best_key, best_ratio = key, ratio
    return best_key if best_ratio >= 0.7 else None


def explain_tab(role: Optional[str], tab_key: str) -> dict:
    """Identity-aware tab explanation: permission check (fact) + step-by-step
    walkthrough (narrative). Returns ``{"error": ...}`` when the role can't see
    the tab or the tab is unknown (no invented UI)."""
    resolved = _resolve_tab_key(tab_key)
    if resolved is None:
        return {"error": f"Unknown page '{tab_key}'. {_NO_RECORD}"}
    tab_key = resolved
    if normalize_role(role) not in TAB_VISIBILITY[tab_key]:
        return {"error": f"Your current role cannot see the '{tab_key}' page, so I can't guide you there."}
    narrative = TAB_NARRATIVES.get(tab_key)
    if narrative is None:
        return {"tab_key": tab_key, "known": False, "message": _NO_RECORD}
    return {
        "tab_key": tab_key,
        "known": True,
        "title": narrative["title"],
        "summary": narrative["summary"],
        "steps": list(narrative.get("steps", [])),
    }


def explain_field(form_key: str, field_path: str) -> dict:
    """Field explanation merging facts (required / enum / linkage, from code)
    with the hand-written narrative. Unknown form/field → "没有记录"."""
    if form_key not in FORM_SCHEMAS:
        return {"error": f"Unknown form '{form_key}'. {_NO_RECORD}"}
    if not _schema_has_field(form_key, field_path):
        return {"error": f"'{form_key}.{field_path}' is not a field I recognize. {_NO_RECORD}"}
    enum_key = f"{form_key}::{field_path}"
    narrative = FIELD_NARRATIVES.get(form_key, {}).get(field_path)
    return {
        "form_key": form_key,
        "field_path": field_path,
        "required": field_path in _REQUIRED.get(form_key, frozenset()),
        "enum": _ENUMS.get(enum_key),
        "linkage": _LINKAGE.get(enum_key, ""),
        "narrative": narrative,
        "known": narrative is not None,
    }


# ── Tool layer (registered onto the guide ReAct) ─────────────────────────


def build_ui_capability_tools(actor) -> list:
    """Wrap the explain helpers as langchain tools bound to ``actor``'s role.
    Pure read-only knowledge — no DB, no writes."""
    from langchain_core.tools import tool

    @tool
    def ui_list_tabs() -> dict:
        """List the RelayOps tabs/pages the CURRENT user can see, with a one
        line summary each. Use this for vague "teach me how to use it" asks so
        you can offer the user concrete pages to drill into."""
        tabs = tabs_for_role(actor.role)
        return {
            "role": normalize_role(actor.role),
            "tabs": [
                {"key": t, "title": TAB_NARRATIVES.get(t, {}).get("title", t),
                 "summary": TAB_NARRATIVES.get(t, {}).get("summary", "")}
                for t in tabs
            ],
        }

    @tool
    def ui_explain_tab(tab_key: str) -> dict:
        """Explain one tab/page step by step for the CURRENT user. ``tab_key`` is
        a page key from ui_list_tabs (e.g. 'projects', 'verification'). Returns
        an error if the user can't see it or it isn't recorded — relay that as-is,
        do not invent steps."""
        return explain_tab(actor.role, tab_key)

    @tool
    def ui_explain_field(form_key: str, field_path: str) -> dict:
        """Explain an onboarding form field: whether it's required, its allowed
        enum values, linkage rules, and a plain-language note. ``form_key`` is
        one of project/product/job/app/job_scenario/app_scenario; ``field_path``
        is the field name (e.g. 'cml_app_type'). Unknown fields return an error —
        relay it, do not make up a field."""
        return explain_field(form_key, field_path)

    return [ui_list_tabs, ui_explain_tab, ui_explain_field]
