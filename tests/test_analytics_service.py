"""analytics_service — period resolution, health classification, issue stats.

The math moved out of the router (AGENT_INTELLIGENCE_PLAN.md §3.0); these
tests pin the service surface the agent tools build on.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.exceptions import ValidationError
from core.models.constants import AUTO_CLOSE_RESOLUTION
from core.services import analytics_service
from core.services.analytics_service import (
    AnomalyRules,
    classify_product_health,
    compute_issue_stats,
    resolve_period,
)


# ── resolve_period ────────────────────────────────────────────────────


def test_month_period_explicit():
    p = resolve_period(year=2026, month=2)
    assert p.granularity == "month"
    assert p.start == datetime(2026, 2, 1)
    assert p.end == datetime(2026, 3, 1)
    assert p.label == "February 2026"


def test_december_rolls_into_next_year():
    p = resolve_period(year=2025, month=12)
    assert p.end == datetime(2026, 1, 1)


def test_week_period_snaps_to_monday():
    p = resolve_period(week_start="2026-06-11")  # a Thursday
    assert p.granularity == "week"
    assert p.start == datetime(2026, 6, 8)
    assert p.end == datetime(2026, 6, 15)
    assert p.week_start == "2026-06-08"


def test_invalid_week_start_raises_domain_error():
    with pytest.raises(ValidationError):
        resolve_period(week_start="not-a-date")


def test_invalid_month_raises_domain_error():
    with pytest.raises(ValidationError):
        resolve_period(year=2026, month=13)


# ── classify_product_health ───────────────────────────────────────────

RULES = AnomalyRules()
PERIOD_END = datetime(2026, 6, 1)


def _classify(**overrides):
    kwargs = dict(
        failure_rate_percent=0,
        job_runs_total=0,
        open_issue_count=0,
        max_repeat_failure_streak=0,
        last_failed_at=None,
        period_end=PERIOD_END,
        rules=RULES,
    )
    kwargs.update(overrides)
    return classify_product_health(**kwargs)


def test_healthy_product():
    severity, is_anomaly, score, reasons = _classify()
    assert (severity, is_anomaly, score, reasons) == ("healthy", False, 0, [])


def test_rule_a_high_failure_rate():
    severity, is_anomaly, score, reasons = _classify(
        failure_rate_percent=50, job_runs_total=30)
    assert is_anomaly and len(reasons) == 1
    assert "failure_rate" in reasons[0]
    assert severity in ("risk", "anomaly")


def test_rule_b_streak():
    severity, is_anomaly, _, reasons = _classify(max_repeat_failure_streak=5)
    assert is_anomaly and "repeat_failure_streak" in reasons[0]


def test_two_rules_escalate_to_anomaly():
    severity, _, _, reasons = _classify(
        failure_rate_percent=50, job_runs_total=30, max_repeat_failure_streak=6)
    assert len(reasons) == 2 and severity == "anomaly"


def test_severities_are_known_vocabulary():
    from core.agent.knowledge import HEALTH_SEVERITIES

    for kwargs in (
        {},
        {"failure_rate_percent": 30, "job_runs_total": 10},
        {"max_repeat_failure_streak": 5},
        {"failure_rate_percent": 90, "job_runs_total": 50, "max_repeat_failure_streak": 9},
    ):
        severity, _, _, _ = _classify(**kwargs)
        assert severity in HEALTH_SEVERITIES


# ── compute_issue_stats ───────────────────────────────────────────────


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Maps Product/Project queries to canned rows for the breakdown maps."""

    def __init__(self, products=(), projects=()):
        self._products = list(products)
        self._projects = list(projects)

    def query(self, entity):
        name = getattr(entity, "__name__", str(entity))
        if name == "Product":
            return _FakeQuery(self._products)
        if name == "Project":
            return _FakeQuery(self._projects)
        return _FakeQuery([])


def _issue(**overrides):
    base = dict(
        type="job_failed", status="open", product_id=None,
        created_at=datetime(2026, 6, 1), resolved_at=None,
        sla_deadline=None, resolution_description=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_stats_counts_types_and_statuses():
    issues = [
        _issue(),
        _issue(type="app_offline", status="in_progress"),
        _issue(status="resolved", resolved_at=datetime(2026, 6, 1, 0, 30),
               resolution_description="fixed by rerun"),
    ]
    stats = compute_issue_stats(_FakeSession(), issues)
    assert stats["total"] == 3
    assert stats["open_count"] == 1
    assert stats["in_progress_count"] == 1
    assert stats["resolved_count"] == 1
    assert {e["type"]: e["count"] for e in stats["by_type"]} == {"job_failed": 2, "app_offline": 1}


def test_sla_and_mttr_math():
    created = datetime(2026, 6, 1)
    issues = [
        _issue(status="resolved", created_at=created,
               resolved_at=created + timedelta(minutes=30),
               sla_deadline=created + timedelta(hours=1),
               resolution_description="ok"),       # compliant, 30 min
        _issue(status="resolved", created_at=created,
               resolved_at=created + timedelta(minutes=90),
               sla_deadline=created + timedelta(hours=1),
               resolution_description="late"),     # breached, 90 min
    ]
    stats = compute_issue_stats(_FakeSession(), issues)
    assert stats["sla_total_count"] == 2
    assert stats["sla_compliant_count"] == 1
    assert stats["sla_compliance_rate"] == 50
    assert stats["avg_resolution_minutes"] == 60
    assert stats["max_resolution_minutes"] == 90


def test_auto_closed_not_a_manual_intervention():
    issues = [
        _issue(status="closed", resolution_description=AUTO_CLOSE_RESOLUTION),
        _issue(status="resolved", resolved_at=datetime(2026, 6, 1, 1),
               resolution_description="manually rerun"),
    ]
    stats = compute_issue_stats(_FakeSession(), issues)
    assert stats["manual_interventions"] == 1


def test_router_aliases_still_point_at_service():
    from api.routers import analytics as router

    assert router._execution_is_failure is analytics_service.execution_is_failure
    assert router._max_failed_streak is analytics_service.max_failed_streak
    assert router._compute_issue_stats is analytics_service.compute_issue_stats
