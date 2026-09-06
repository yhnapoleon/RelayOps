"""KB fact layer — the ground truth the narrative KB must not contradict.

Curated (by reading the three sources: frontend App.tsx / *View components, the
API routers + api.ts, and the rendered UI). Kept separate from the narrative
Markdown so a drift test can assert every page the KB describes is a real tab
and every button it documents is a real UI control — the same fact-vs-narrative
discipline as ``ui_capability.py`` / ``knowledge.CAPABILITY_MATRIX``.

``KNOWN_BUTTONS`` is filled out per tab during KB authoring (plan Phase 2). An
empty set means "buttons not yet inventoried" — the drift test skips the button
check for that tab rather than failing, so the pipeline can land before every
page is fully documented.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List

from core.agent.ui_capability import TAB_VISIBILITY, tabs_for_role  # re-exported


def known_tabs() -> List[str]:
    """Every real UI tab key (single source = ui_capability.TAB_VISIBILITY)."""
    return list(TAB_VISIBILITY)


# tab_key -> the UI control labels a KB ``## buttons`` section may name (verbatim
# from App.tsx / the *View components). Labels are matched case-insensitively
# against the KB's bolded labels by the drift test. An empty set = "not yet
# inventoried" → the button check is skipped for that tab.
KNOWN_BUTTONS: Dict[str, FrozenSet[str]] = {
    "dashboard": frozenset({
        "Total Projects", "Open Issues", "Resolved Issues", "Total Issues",
    }),
    "projects": frozenset({
        "New Project", "Create Project", "Create Product", "Add Job", "Add App",
        "Scenarios", "Add / Manage", "API Checks", "Transfer Business Owner",
        "Submit for Review", "Save Changes",
    }),
    "actions": frozenset({
        "View Details", "Open Workbench", "Start Working", "Contact Owner",
        "Open in MMP", "Details",
    }),
    "workbench": frozenset({
        "Back to Actions", "Open in MMP", "Open Project", "Open Product",
        "Change scenario", "Start Working", "Continue Working", "Back to runbook",
        "Record Step", "Escalate", "Return to Product Owner", "Contact Owner",
        "Resolve Issue", "Mark False Positive",
    }),
    "resolved-issues": frozenset({"Dashboard", "Details"}),
    "open-issues": frozenset({"Assign to me", "Assign selected to me"}),
    "my-schedule": frozenset(),
    "relayops-admin-handover": frozenset({"Approve", "Reject"}),
    "relayops-admin-issues": frozenset({
        "Outstanding Issues", "Resolved", "All Issue History", "View Timeline",
        "Details",
    }),
    "schedules": frozenset({"Assign Duty", "Calendar", "List", "Edit", "Delete"}),
    "relayops-admin-users": frozenset({"Apply"}),
    "verification": frozenset({
        "Reload", "Run all verification", "Run verification", "Run",
        "Generate Report", "View Reports", "View Report",
    }),
    "analytics": frozenset(),  # export/rule button labels not verbatim-inventoried yet
    "ai-assistant": frozenset({"Guide", "Write mode", "Onboarding"}),
}


def known_buttons(tab: str) -> FrozenSet[str]:
    return KNOWN_BUTTONS.get(tab, frozenset())


__all__ = ["known_tabs", "known_buttons", "KNOWN_BUTTONS", "tabs_for_role", "TAB_VISIBILITY"]
