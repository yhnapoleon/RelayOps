"""Domain knowledge layer — enum coverage guards + handling-state matrix.

The coverage tests are the drift alarm: add a value to constants.py without a
meaning entry in knowledge.py and they go red.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from core.agent import knowledge
from core.agent.knowledge import (
    AUTO_CLOSE_RESOLUTION,
    HandlingState,
    build_domain_card,
    classify_handling_state,
    domain_card_for_issue,
)
from core.models.constants import (
    ApplicationRecoveryScenarioType,
    IssueActionType,
    IssueStatus,
    IssueType,
    JobFailureScenarioType,
)

NOW = datetime(2026, 6, 11, 12, 0, 0)


def _issue(**overrides):
    base = dict(
        status=IssueStatus.OPEN,
        assignee_id=None,
        action_summary_json=None,
        resolution_description=None,
        sla_deadline=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── enum coverage (drift alarms) ──────────────────────────────────────


def test_issue_type_meanings_cover_all_constants():
    assert set(knowledge.ISSUE_TYPE_MEANINGS) == set(IssueType.ALL)


def test_issue_status_meanings_cover_all_constants():
    assert set(knowledge.ISSUE_STATUS_MEANINGS) == set(IssueStatus.ALL)


def test_action_meanings_cover_all_constants():
    assert set(knowledge.ISSUE_ACTION_MEANINGS) == set(IssueActionType.ALL)


def test_job_scenario_meanings_cover_all_constants():
    assert set(knowledge.JOB_SCENARIO_MEANINGS) == set(JobFailureScenarioType.ALL)


def test_app_scenario_meanings_cover_all_constants():
    assert set(knowledge.APP_SCENARIO_MEANINGS) == set(ApplicationRecoveryScenarioType.ALL)


def test_scenario_hints_only_reference_known_types():
    known = set(JobFailureScenarioType.ALL) | set(ApplicationRecoveryScenarioType.ALL)
    for issue_type, hint_types in knowledge.ISSUE_SCENARIO_HINTS.items():
        assert issue_type in IssueType.ALL
        assert hint_types <= known, f"unknown scenario types for {issue_type}: {hint_types - known}"


def test_handling_state_labels_cover_all_states():
    assert set(HandlingState.LABELS) == set(HandlingState.ALL)


def test_capability_matrix_names_only_real_tools():
    # Drift guard: every relayops_* tool named in the capability
    # matrix must actually exist, so the dual-track "能否对话完成" judgement can't
    # promise a tool that isn't wired in.
    import re

    import pytest

    pytest.importorskip("langchain_core")
    from core.agent.tools import build_langchain_tools
    from core.auth.jwt import CurrentUser
    from core.models.user import UserRole

    real = {t.name for t in build_langchain_tools(
        CurrentUser(username="boss", user_id=1, role=UserRole.ADMIN))}
    named = set()
    for _cap, hint in knowledge.CAPABILITY_MATRIX.values():
        named.update(re.findall(r"relayops_\w+", hint))
    missing = named - real
    assert not missing, f"capability matrix names non-existent tools: {missing}"


def test_capability_matrix_values_use_known_cap_levels():
    levels = {knowledge.CAP_READ, knowledge.CAP_WRITE, knowledge.CAP_UI_ONLY}
    for cap, _hint in knowledge.CAPABILITY_MATRIX.values():
        assert cap in levels


# ── domain card ───────────────────────────────────────────────────────


def test_domain_card_mentions_every_enum_value():
    card = build_domain_card()
    for value in IssueType.ALL + IssueStatus.ALL + IssueActionType.ALL:
        assert f"`{value}`" in card
    assert f"{len(IssueType.ALL)} in total" in card
    assert f"{len(IssueStatus.ALL)} values" in card


def test_compact_card_is_a_prefix_subset():
    compact = build_domain_card(compact=True)
    assert "Issue types" in compact
    assert "recovery scenario" not in compact  # detail sections trimmed
    assert len(compact) < len(build_domain_card())


def test_issue_slice_contains_type_semantics_and_hints():
    text = domain_card_for_issue(IssueType.JOB_FAILED)
    assert "job_failed" in text
    assert "triggered_but_failed" in text
    # status semantics always ride along
    assert "false_positive" in text


# ── handling-state classifier matrix ──────────────────────────────────


def test_open_without_assignee_is_unclaimed():
    out = classify_handling_state(_issue(), now=NOW)
    assert out["state"] == HandlingState.UNCLAIMED
    assert out["label"] == "Unclaimed"


def test_open_with_assignee_no_start_is_claimed_not_started():
    out = classify_handling_state(_issue(assignee_id=7), now=NOW)
    assert out["state"] == HandlingState.CLAIMED_NOT_STARTED


def test_open_after_return_to_owner_wins_over_assignee():
    out = classify_handling_state(
        _issue(assignee_id=7, action_summary_json={
            "returned_to_owner_at": "2026-06-11T10:00:00",
            "returned_to_owner_notes": "需要项目组改逻辑",
        }),
        now=NOW,
    )
    assert out["state"] == HandlingState.RETURNED_TO_OWNER
    assert "项目组" in out["detail"]


def test_in_progress_plain_is_working():
    out = classify_handling_state(
        _issue(status=IssueStatus.IN_PROGRESS, assignee_id=7,
               action_summary_json={"started_at": "2026-06-11T09:00:00"}),
        now=NOW,
    )
    assert out["state"] == HandlingState.IN_PROGRESS


def test_escalation_as_latest_event_means_waiting_external():
    out = classify_handling_state(
        _issue(status=IssueStatus.IN_PROGRESS, assignee_id=7, action_summary_json={
            "started_at": "2026-06-11T09:00:00",
            "steps": [{"title": "查日志", "recorded_at": "2026-06-11T09:10:00"}],
            "escalations": [{"target": "ops-team@example.com", "recorded_at": "2026-06-11T09:30:00"}],
        }),
        now=NOW,
    )
    assert out["state"] == HandlingState.ESCALATED_WAITING
    assert "ops-team@example.com" in out["detail"]


def test_step_after_escalation_returns_to_working():
    out = classify_handling_state(
        _issue(status=IssueStatus.IN_PROGRESS, assignee_id=7, action_summary_json={
            "escalations": [{"target": "x@y", "recorded_at": "2026-06-11T09:30:00"}],
            "steps": [{"title": "对方回复后继续", "recorded_at": "2026-06-11T10:30:00"}],
        }),
        now=NOW,
    )
    assert out["state"] == HandlingState.IN_PROGRESS


def test_resolved_carries_resolution_excerpt():
    out = classify_handling_state(
        _issue(status=IssueStatus.RESOLVED, resolution_description="重跑后成功"),
        now=NOW,
    )
    assert out["state"] == HandlingState.RESOLVED
    assert "重跑" in out["detail"]


def test_closed_auto_vs_manual():
    auto = classify_handling_state(
        _issue(status=IssueStatus.CLOSED, resolution_description=AUTO_CLOSE_RESOLUTION), now=NOW)
    manual = classify_handling_state(
        _issue(status=IssueStatus.CLOSED, resolution_description="手工确认无影响后关闭"), now=NOW)
    assert auto["state"] == HandlingState.CLOSED_AUTO
    assert manual["state"] == HandlingState.CLOSED_MANUAL


def test_false_positive_state():
    out = classify_handling_state(_issue(status=IssueStatus.FALSE_POSITIVE), now=NOW)
    assert out["state"] == HandlingState.FALSE_POSITIVE


def test_sla_flags_at_risk_then_overdue():
    at_risk = classify_handling_state(
        _issue(sla_deadline=NOW + timedelta(minutes=10)), now=NOW)
    overdue = classify_handling_state(
        _issue(sla_deadline=NOW - timedelta(minutes=1)), now=NOW)
    safe = classify_handling_state(
        _issue(sla_deadline=NOW + timedelta(hours=2)), now=NOW)
    assert at_risk["sla_at_risk"] and not at_risk["sla_overdue"]
    assert overdue["sla_overdue"] and not overdue["sla_at_risk"]
    assert not safe["sla_at_risk"] and not safe["sla_overdue"]


def test_sla_flags_not_raised_on_terminal_states():
    out = classify_handling_state(
        _issue(status=IssueStatus.RESOLVED, sla_deadline=NOW - timedelta(hours=1)), now=NOW)
    assert not out["sla_overdue"] and not out["sla_at_risk"]


# ── failure signatures (P2 retrieval labels) ──────────────────────────


def test_signature_extraction_matrix():
    extract = knowledge.extract_failure_signatures
    assert extract("Kerberos ticket expired, kinit failed") == ["kerberos"]
    assert extract("job timed out waiting; 上游依赖失败") == ["dependency", "timeout"]
    assert extract("OOMKilled by scheduler") == ["oom"]
    assert extract("nothing recognizable here") == []
    assert extract("") == [] and extract(None) == []


def test_signature_needles_are_lowercase():
    for label, needles in knowledge.FAILURE_SIGNATURES.items():
        assert label == label.lower()
        for n in needles:
            assert n == n.lower(), f"{label}: needle {n!r} must be lowercase"


def test_malformed_action_summary_degrades_gracefully():
    out = classify_handling_state(
        _issue(status=IssueStatus.IN_PROGRESS, action_summary_json={
            "steps": [{"recorded_at": "not-a-date"}, "garbage"],
            "escalations": [],
        }),
        now=NOW,
    )
    assert out["state"] == HandlingState.IN_PROGRESS
