"""S4.4 — ui_capability knowledge layer: drift alarms + explain semantics.

The drift tests are the alarm: a narrative key that no longer resolves to a real
tab / schema field goes red. No LLM, no DB — all pure functions.
"""
import pytest

from core.agent import ui_capability as ui
from core.agent.ui_capability import (
    FIELD_NARRATIVES,
    FORM_SCHEMAS,
    TAB_NARRATIVES,
    TAB_VISIBILITY,
    explain_field,
    explain_tab,
    tabs_for_role,
)
from core.models.constants import (
    ApplicationRecoveryScenarioType,
    JobFailureScenarioType,
)
from core.models.user import UserRole


# ── drift alarms (S4.4) ──────────────────────────────────────────────────


def test_tab_narratives_cover_exactly_known_tabs():
    # Every recorded tab narrative must be a real (gated) tab, and vice versa.
    assert set(TAB_NARRATIVES) == set(TAB_VISIBILITY)


def test_field_narratives_reference_real_schema_fields():
    for form_key, fields in FIELD_NARRATIVES.items():
        assert form_key in FORM_SCHEMAS, f"unknown form_key {form_key!r}"
        model_fields = FORM_SCHEMAS[form_key].model_fields
        for field_path in fields:
            assert field_path in model_fields, f"{form_key}.{field_path} not on schema"


def test_enum_facts_match_constants():
    assert explain_field("app", "cml_app_type")["enum"] == list(("fastapi", "runtime", "ray", "generic"))
    assert explain_field("job_scenario", "scenario_type")["enum"] == list(JobFailureScenarioType.ALL)
    assert explain_field("app_scenario", "scenario_type")["enum"] == list(ApplicationRecoveryScenarioType.ALL)


# ── tabs_for_role ────────────────────────────────────────────────────────


def test_admin_sees_admin_panel_relayops_member_does_not():
    admin = tabs_for_role(UserRole.ADMIN)
    relayops = tabs_for_role(UserRole.RELAYOPS_MEMBER)
    assert "relayops-admin-users" in admin and "relayops-admin-handover" in admin
    # relayops_member is elevated at the service layer but the admin panel stays
    # admin-only in the UI — the guide must reflect what they actually see.
    assert "relayops-admin-users" not in relayops and "relayops-admin-handover" not in relayops
    assert "actions" in relayops and "verification" in relayops


def test_regular_user_sees_only_basic_tabs():
    # My Actions + its workbench and the Open Issues board are now stable
    # fixtures for every authenticated user (content is role/membership-scoped).
    # The Ops/admin-only surfaces (schedules, admin panel, verification, …) stay
    # hidden.
    regular = tabs_for_role(UserRole.REGULAR_USER)
    assert set(regular) == {
        "dashboard", "projects", "actions", "workbench", "open-issues",
        "analytics", "ai-assistant",
    }


def test_legacy_business_owner_normalizes_to_regular():
    assert tabs_for_role("business_owner") == tabs_for_role(UserRole.REGULAR_USER)


# ── explain_tab (fact + narrative) ───────────────────────────────────────


def test_explain_tab_blocks_invisible_tab():
    out = explain_tab(UserRole.REGULAR_USER, "relayops-admin-users")
    assert "error" in out


def test_explain_tab_unknown_tab_no_record():
    out = explain_tab(UserRole.ADMIN, "nonexistent-tab")
    assert "error" in out and ui._NO_RECORD in out["error"]


def test_explain_tab_returns_steps_for_visible():
    out = explain_tab(UserRole.ADMIN, "projects")
    assert out["known"] is True and out["steps"] and out["title"]


def test_explain_tab_tolerates_underscore_key():
    # The model often retypes the offered 'ai-assistant' key as 'ai_assistant'.
    out = explain_tab(UserRole.ADMIN, "ai_assistant")
    assert out.get("known") is True and "error" not in out


def test_offered_tab_keys_resolve_hyphen_or_underscore():
    # Seam guard (RELIABILITY_PLAN §6.3): guide_offer_tabs hands the model these
    # keys; explain_tab must accept each one back in either spelling.
    for role in (UserRole.ADMIN, UserRole.RELAYOPS_MEMBER, UserRole.REGULAR_USER):
        for tab in tabs_for_role(role):
            assert "error" not in explain_tab(role, tab)
            assert "error" not in explain_tab(role, tab.replace("-", "_"))


def test_explain_tab_resolves_display_name():
    # RELIABILITY_PLAN §12 A — the model passes the human label it saw on the
    # card (or its own paraphrase), not the internal key. explain_tab must
    # resolve the display name to the right tab.
    assert explain_tab(UserRole.ADMIN, "My Project")["known"] is True
    assert explain_tab(UserRole.ADMIN, "My Projects")["known"] is True
    assert explain_tab(UserRole.ADMIN, "Admin Panel · User Management")["known"] is True
    assert explain_tab(UserRole.ADMIN, "Admin Panel · Schedules")["known"] is True
    assert explain_tab(UserRole.ADMIN, "AI Assistant")["known"] is True


def test_explain_tab_still_rejects_nonexistent_page():
    # Resolution must not over-match: a tab the platform doesn't have stays error.
    out = explain_tab(UserRole.ADMIN, "Reports and Billing Center")
    assert "error" in out


# ── explain_field ────────────────────────────────────────────────────────


def test_explain_field_required_and_linkage():
    name = explain_field("project", "name")
    assert name["required"] is True
    url = explain_field("app", "application_url")
    assert "either" in url["linkage"]


def test_explain_field_unknown_field_no_record():
    out = explain_field("job", "totally_made_up")
    assert "error" in out and ui._NO_RECORD in out["error"]


def test_explain_field_unknown_form_no_record():
    out = explain_field("widget", "name")
    assert "error" in out
