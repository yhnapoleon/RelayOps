"""Out-of-schedule failures are recorded but don't raise an Issue.

Manager requirement: a run that fails *outside* the job's cron window — e.g.
a Monday or weekend run for a ``0 5 * * 2-5`` (Tue–Fri 05:00) job — must still
be recorded as a run snapshot but must NOT create a JOB_FAILED Issue. Only
failures of runs the schedule actually asked for page the on-duty Ops.

Covers the shared ``is_within_cron_schedule`` helper plus the CmlChecker
periodic-loop branch that consumes it. All datetimes are naive UTC and the
cron is evaluated with ``tz_offset_minutes=0`` so reasoning stays in UTC.
"""
from datetime import datetime

from core.checker.base import AnomalyType, CheckStatus
from core.checker.cml_checker import CmlChecker
from core.integrations.staleness import is_within_cron_schedule

# Tue–Fri 05:00. (2026-06-22 Mon, 23 Tue, 24 Wed, 26 Fri, 27 Sat, 28 Sun.)
WEEKDAY_CRON = "0 5 * * 2-5"


# --------------------------------------------------------------------------- #
# is_within_cron_schedule
# --------------------------------------------------------------------------- #


def _within(run_dt: datetime, cron: str = WEEKDAY_CRON) -> bool:
    return is_within_cron_schedule(cron, run_dt, tz_offset_minutes=0)


def test_run_on_scheduled_day_is_in_window():
    # Tuesday 05:00 — exactly a scheduled fire.
    assert _within(datetime(2026, 6, 23, 5, 0)) is True
    # Friday 05:03 — small jitter, still the scheduled slot.
    assert _within(datetime(2026, 6, 26, 5, 3)) is True


def test_run_on_unscheduled_day_is_out_of_window():
    # Monday — cron never fires; failure should be suppressed.
    assert _within(datetime(2026, 6, 22, 5, 0)) is False
    # Weekend.
    assert _within(datetime(2026, 6, 27, 5, 0)) is False
    assert _within(datetime(2026, 6, 28, 5, 0)) is False


def test_run_far_from_fire_on_scheduled_day_is_out_of_window():
    # Tuesday but at 20:00 — hours away from the 05:00 fire (> default 120m).
    assert _within(datetime(2026, 6, 23, 20, 0)) is False


def test_fail_open_when_cron_missing_or_unparseable():
    dt = datetime(2026, 6, 22, 5, 0)  # a Monday
    assert is_within_cron_schedule(None, dt, tz_offset_minutes=0) is True
    assert is_within_cron_schedule("", dt, tz_offset_minutes=0) is True
    assert is_within_cron_schedule("not a cron", dt, tz_offset_minutes=0) is True
    # Missing run time -> can't evaluate -> in-window (issue still raised).
    assert is_within_cron_schedule(WEEKDAY_CRON, None, tz_offset_minutes=0) is True


def test_accepts_timestamp_string():
    assert is_within_cron_schedule(
        WEEKDAY_CRON, "2026-06-23T05:00:00Z", tz_offset_minutes=0
    ) is True
    assert is_within_cron_schedule(
        WEEKDAY_CRON, "2026-06-22T05:00:00Z", tz_offset_minutes=0
    ) is False


def test_tolerance_capped_for_frequent_cron():
    # Every 30 min: a run 5 min late still maps to its fire; the wide default
    # 120m tolerance is capped at interval/2 (15m) so it can't bridge fires.
    cron = "*/30 * * * *"
    assert is_within_cron_schedule(cron, datetime(2026, 6, 22, 10, 5), tz_offset_minutes=0) is True


# --------------------------------------------------------------------------- #
# CmlChecker._evaluate_run integration
# --------------------------------------------------------------------------- #


class _FakeJob:
    id = 7
    product_id = 3
    cml_project_id = "proj"
    cml_job_id = "job-x"
    cml_job_name = "Nightly Recommend"
    schedule_cron = WEEKDAY_CRON
    control_m_cron = None
    sla_preset = None
    sla_custom_minutes = None


def _failed_run(created_at: str) -> dict:
    return {
        "id": "run-123",
        "status": "ENGINE_FAILED",
        "created_at": created_at,
        "running_at": created_at,
        "finished_at": created_at,
        "failure_reason": "boom",
    }


def _evaluate(run: dict):
    checker = CmlChecker(control_interface=None)
    # tz default is UTC+8; pass UTC-equivalent timestamps so the cron lines up.
    return checker._evaluate_run(_FakeJob(), run)


def test_failed_run_in_window_raises_anomaly(monkeypatch):
    # Force UTC cron evaluation so the 05:00 timestamps match the cron.
    monkeypatch.setenv("RELAYOPS_CRON_TZ_OFFSET_MINUTES", "0")
    result = _evaluate(_failed_run("2026-06-23T05:00:00Z"))  # Tuesday
    assert result.status == CheckStatus.ANOMALY
    assert result.anomaly_event is not None
    assert result.anomaly_event.anomaly_type == AnomalyType.JOB_FAILED


def test_failed_run_out_of_window_is_recorded_without_anomaly(monkeypatch):
    monkeypatch.setenv("RELAYOPS_CRON_TZ_OFFSET_MINUTES", "0")
    result = _evaluate(_failed_run("2026-06-22T05:00:00Z"))  # Monday
    assert result.status == CheckStatus.HEALTHY
    assert result.anomaly_event is None
    assert "outside the cron schedule" in result.reason
