"""Unit tests for the false-positive → success-point behaviour.

A failed CML run whose alert an on-duty Ops member dismisses as a false
positive must count as a *success* in every health chart, not a failure.
The plumbing has two pure-ish pieces this suite pins down:

  * ``false_positive_execution_keys`` — maps false-positive issues back to
    the ``(job_id, cml_run_id)`` of the run they dismissed.
  * ``_execution_is_failure`` / ``_max_failed_streak`` — the failure
    predicate the analytics router feeds into failure-rate, streak, and
    trend metrics, now honouring that dismissal set.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.routers.analytics import _execution_is_failure, _max_failed_streak
from core.models.entities import IssueStatus
from core.services.issue_service import false_positive_execution_keys


def _exec(job_id: int, cml_run_id: str | None, status: str):
    return SimpleNamespace(job_id=job_id, cml_run_id=cml_run_id, status=status)


# ─────────────────── false_positive_execution_keys ───────────────────


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.last_entities = None

    def query(self, *entities):
        self.last_entities = entities
        return _FakeQuery(self._rows)


def test_false_positive_keys_empty_job_ids_skips_query():
    session = _FakeSession(rows=[SimpleNamespace(job_id=1, dedup_key="r1")])
    assert false_positive_execution_keys(session, []) == set()
    # No query should have been issued for an empty / all-None job list.
    assert session.last_entities is None
    assert false_positive_execution_keys(session, [None]) == set()


def test_false_positive_keys_returns_job_run_tuples():
    rows = [
        SimpleNamespace(job_id=1, dedup_key="run-1"),
        SimpleNamespace(job_id=1, dedup_key="run-2"),
        SimpleNamespace(job_id=2, dedup_key="run-9"),
    ]
    session = _FakeSession(rows=rows)
    keys = false_positive_execution_keys(session, [1, 2])
    assert keys == {(1, "run-1"), (1, "run-2"), (2, "run-9")}


def test_false_positive_keys_filters_on_status_constant():
    # Guards against drift between the constant and the query intent.
    assert IssueStatus.FALSE_POSITIVE == "false_positive"


# ───────────────────────── _execution_is_failure ─────────────────────


def test_failed_run_counts_as_failure_when_not_dismissed():
    execution = _exec(job_id=1, cml_run_id="run-1", status="failed")
    assert _execution_is_failure(execution, set()) is True


def test_failed_run_counts_as_success_when_dismissed():
    execution = _exec(job_id=1, cml_run_id="run-1", status="failed")
    keys = {(1, "run-1")}
    assert _execution_is_failure(execution, keys) is False


def test_dismissal_is_scoped_to_the_exact_job_and_run():
    execution = _exec(job_id=1, cml_run_id="run-1", status="failed")
    # Same run id but a different job must not be dismissed.
    assert _execution_is_failure(execution, {(2, "run-1")}) is True
    # Same job but a different run id must not be dismissed.
    assert _execution_is_failure(execution, {(1, "run-2")}) is True


def test_completed_run_is_never_a_failure():
    execution = _exec(job_id=1, cml_run_id="run-1", status="completed")
    assert _execution_is_failure(execution, set()) is False


# ───────────────────────── _max_failed_streak ────────────────────────


def test_streak_ignores_dismissed_run_in_the_middle():
    executions = [
        _exec(1, "a", "failed"),
        _exec(1, "b", "failed"),
        _exec(1, "c", "failed"),
    ]
    # Without dismissal: a 3-run streak.
    assert _max_failed_streak(executions, set()) == 3
    # Dismissing the middle run breaks the streak into 1 + 1.
    assert _max_failed_streak(executions, {(1, "b")}) == 1


def test_streak_zero_when_all_dismissed():
    executions = [_exec(1, "a", "failed"), _exec(1, "b", "failed")]
    assert _max_failed_streak(executions, {(1, "a"), (1, "b")}) == 0
