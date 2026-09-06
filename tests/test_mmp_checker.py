"""Unit tests for MmpChecker — the periodic drift-detection branch.

These tests cover:
  * The interval guard (returns early when not yet due)
  * The empty-jobs short-circuit
  * The clear_cache call at cycle start
  * Per-job snapshot writes for create_issue / close_issue
  * Snapshot NOT written for skipped / inconclusive
  * CheckResult.status mapping for each DriftResult.action
  * AnomalyEvent dispatch via on_anomaly callback

The interface is replaced by a stub that returns whatever DriftResult
the test wants. The session is replaced by a small Fake that captures
.add() calls and supports the .query(...).join(...).filter(...).all()
chain MmpChecker uses to fetch Jobs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import List

import pytest

from core.checker.base import AnomalyEvent, CheckStatus
from core.checker.mmp_checker import MmpChecker
from core.integrations.mmp_interface import DriftResult
from core.models.mmp_entities import MmpDriftSnapshot


# ─────────────────────────── stubs / fakes ───────────────────────────


class StubInterface:
    """Returns canned DriftResults keyed by (repo_name, model_name).

    Falls back to a default if a key isn't registered. Records every
    check_drift call so tests can assert on ordering and arguments.
    Also tracks clear_cache() invocations.
    """

    def __init__(self, default: DriftResult | None = None):
        self._responses: dict[tuple[str, str], DriftResult] = {}
        self._default = default or DriftResult(action="skipped", reason="default-stub")
        self.calls: list[tuple[str, str, object]] = []
        self.clear_cache_calls = 0

    def set(self, repo_name: str, model_name: str, result: DriftResult) -> None:
        self._responses[(repo_name, model_name)] = result

    def clear_cache(self) -> None:
        self.clear_cache_calls += 1

    def check_drift(self, repo_name, model_name, job) -> DriftResult:
        self.calls.append((repo_name, model_name, job))
        return self._responses.get((repo_name, model_name), self._default)


class FakeQuery:
    def __init__(self, rows: list):
        self._rows = rows

    def join(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class FakeSession:
    """Minimal SQLAlchemy session double — just enough for MmpChecker.

    .query(Job) returns a chainable FakeQuery whose .all() yields the
    pre-loaded job rows. .add(row) appends to a list we can assert on.
    """

    def __init__(self, jobs: list):
        self._jobs = jobs
        self.added: list = []

    def query(self, *args, **kwargs):
        return FakeQuery(self._jobs)

    def add(self, row) -> None:
        self.added.append(row)


def _make_job(
    *,
    job_id: int = 1,
    product_id: int = 100,
    mmp_project_id: str = "demo@a",
    mmp_model_id: str = "demo-model",
    has_mmp_dependency: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=job_id,
        product_id=product_id,
        mmp_project_id=mmp_project_id,
        mmp_model_id=mmp_model_id,
        has_mmp_dependency=has_mmp_dependency,
    )


def _collect_anomalies() -> tuple[List[AnomalyEvent], callable]:
    bucket: list[AnomalyEvent] = []

    def on_anomaly(ev: AnomalyEvent) -> None:
        bucket.append(ev)

    return bucket, on_anomaly


# ─────────────────────────── interval guard ───────────────────────────


class TestIntervalGuard:
    def test_first_run_proceeds(self):
        iface = StubInterface()
        chk = MmpChecker(iface, check_interval_seconds=900)
        session = FakeSession(jobs=[])
        bucket, on_anomaly = _collect_anomalies()

        chk.run_cycle(session, on_anomaly)

        assert chk._last_run_at is not None  # cycle executed

    def test_skips_when_recently_ran(self):
        iface = StubInterface()
        chk = MmpChecker(iface, check_interval_seconds=900)
        # Simulate a previous cycle 60 seconds ago.
        chk._last_run_at = datetime.utcnow() - timedelta(seconds=60)

        session = FakeSession(jobs=[_make_job()])
        bucket, on_anomaly = _collect_anomalies()
        chk.run_cycle(session, on_anomaly)

        # Guard fired → no clear_cache, no check_drift calls.
        assert iface.clear_cache_calls == 0
        assert iface.calls == []

    def test_runs_when_interval_elapsed(self):
        iface = StubInterface(default=DriftResult(action="skipped"))
        chk = MmpChecker(iface, check_interval_seconds=900)
        chk._last_run_at = datetime.utcnow() - timedelta(seconds=1000)

        session = FakeSession(jobs=[_make_job()])
        bucket, on_anomaly = _collect_anomalies()
        chk.run_cycle(session, on_anomaly)

        assert iface.calls != []

    def test_last_run_at_updated_even_when_no_jobs(self):
        iface = StubInterface()
        chk = MmpChecker(iface, check_interval_seconds=900)

        chk.run_cycle(FakeSession(jobs=[]), lambda ev: None)
        first_ts = chk._last_run_at

        # Without re-bumping last_run_at, the guard would re-fire the cycle.
        assert first_ts is not None

    def test_last_run_at_updated_after_successful_cycle(self):
        iface = StubInterface(default=DriftResult(action="close_issue", drifted=False))
        chk = MmpChecker(iface, check_interval_seconds=900)
        chk.run_cycle(FakeSession(jobs=[_make_job()]), lambda ev: None)
        assert chk._last_run_at is not None


# ─────────────────────────── cycle behaviour ───────────────────────────


class TestRunCycle:
    def test_clear_cache_called_when_jobs_exist(self):
        iface = StubInterface(default=DriftResult(action="skipped"))
        chk = MmpChecker(iface, check_interval_seconds=0)
        chk.run_cycle(FakeSession(jobs=[_make_job()]), lambda ev: None)
        assert iface.clear_cache_calls == 1

    def test_clear_cache_not_called_when_no_jobs(self):
        iface = StubInterface()
        chk = MmpChecker(iface, check_interval_seconds=0)
        chk.run_cycle(FakeSession(jobs=[]), lambda ev: None)
        assert iface.clear_cache_calls == 0

    def test_iterates_all_jobs(self):
        iface = StubInterface(default=DriftResult(action="skipped"))
        chk = MmpChecker(iface, check_interval_seconds=0)
        jobs = [
            _make_job(job_id=1, mmp_project_id="a@x", mmp_model_id="m1"),
            _make_job(job_id=2, mmp_project_id="b@y", mmp_model_id="m2"),
            _make_job(job_id=3, mmp_project_id="c@z", mmp_model_id="m3"),
        ]
        chk.run_cycle(FakeSession(jobs=jobs), lambda ev: None)
        assert len(iface.calls) == 3
        called_jobs = [j.id for _, _, j in iface.calls]
        assert called_jobs == [1, 2, 3]


# ─────────────────────────── snapshot writes ───────────────────────────


class TestSnapshotWrites:
    def test_writes_snapshot_on_create_issue(self):
        iface = StubInterface()
        iface.set(
            "demo@a",
            "demo-model",
            DriftResult(
                action="create_issue",
                drifted=True,
                drift_details="drift!",
                cml_model_id=241,
                reason="drift detected",
            ),
        )
        chk = MmpChecker(iface, check_interval_seconds=0)
        session = FakeSession(jobs=[_make_job(job_id=42)])
        chk.run_cycle(session, lambda ev: None)

        snapshots = [r for r in session.added if isinstance(r, MmpDriftSnapshot)]
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap.job_id == 42
        assert snap.drifted is True
        assert snap.drift_details == "drift!"
        assert snap.cml_model_id == 241

    def test_writes_snapshot_when_drift_folded_into_pending(self):
        """Folding the drift Issue into the pending Issue must NOT drop the
        drift snapshot — the local timeline stays factual (drifted=True)."""
        iface = StubInterface()
        iface.set(
            "demo@a",
            "demo-model",
            DriftResult(
                action="create_issue",
                drifted=True,
                drift_superseded_by_pending=True,
                run_pending_user_review=True,
                drift_details="drift!",
                cml_model_id=241,
                latest_run_id=13509,
            ),
        )
        chk = MmpChecker(iface, check_interval_seconds=0)
        session = FakeSession(jobs=[_make_job(job_id=42)])
        chk.run_cycle(session, lambda ev: None)

        snapshots = [r for r in session.added if isinstance(r, MmpDriftSnapshot)]
        assert len(snapshots) == 1
        assert snapshots[0].drifted is True

    def test_writes_snapshot_on_close_issue(self):
        iface = StubInterface()
        iface.set(
            "demo@a",
            "demo-model",
            DriftResult(
                action="close_issue",
                drifted=False,
                drift_details="all clear",
                cml_model_id=241,
                reason="recovery",
            ),
        )
        chk = MmpChecker(iface, check_interval_seconds=0)
        session = FakeSession(jobs=[_make_job()])
        chk.run_cycle(session, lambda ev: None)

        snapshots = [r for r in session.added if isinstance(r, MmpDriftSnapshot)]
        assert len(snapshots) == 1
        assert snapshots[0].drifted is False

    def test_no_snapshot_on_skipped(self):
        iface = StubInterface(default=DriftResult(action="skipped", reason="empty binding"))
        chk = MmpChecker(iface, check_interval_seconds=0)
        session = FakeSession(jobs=[_make_job()])
        chk.run_cycle(session, lambda ev: None)
        assert [r for r in session.added if isinstance(r, MmpDriftSnapshot)] == []

    def test_no_snapshot_on_inconclusive(self):
        iface = StubInterface(default=DriftResult(action="inconclusive", reason="api down"))
        chk = MmpChecker(iface, check_interval_seconds=0)
        session = FakeSession(jobs=[_make_job()])
        chk.run_cycle(session, lambda ev: None)
        assert [r for r in session.added if isinstance(r, MmpDriftSnapshot)] == []

    def test_check_drift_without_session_skips_snapshot(self):
        """check_drift(job) without session must not raise — used by future
        callers that haven't been migrated to pass session yet."""
        iface = StubInterface()
        iface.set(
            "demo@a",
            "demo-model",
            DriftResult(action="create_issue", drifted=True, cml_model_id=1),
        )
        chk = MmpChecker(iface, check_interval_seconds=0)
        # No session passed.
        result = chk.check_drift(_make_job())
        assert result.status == CheckStatus.ANOMALY


# ─────────────────────────── action → CheckStatus mapping ───────────────────────────


class TestStatusMapping:
    def test_create_issue_maps_to_anomaly_with_event(self):
        iface = StubInterface()
        iface.set(
            "demo@a",
            "demo-model",
            DriftResult(action="create_issue", drifted=True, cml_model_id=241),
        )
        chk = MmpChecker(iface, check_interval_seconds=0)
        result = chk.check_drift(_make_job(), session=FakeSession(jobs=[]))
        assert result.status == CheckStatus.ANOMALY
        assert result.anomaly_event is not None
        assert result.anomaly_event.job_id == 1
        assert result.anomaly_event.metadata["cml_model_id"] == 241

    def test_close_issue_maps_to_healthy(self):
        iface = StubInterface()
        iface.set("demo@a", "demo-model", DriftResult(action="close_issue", drifted=False))
        chk = MmpChecker(iface, check_interval_seconds=0)
        result = chk.check_drift(_make_job(), session=FakeSession(jobs=[]))
        assert result.status == CheckStatus.HEALTHY
        assert result.anomaly_event is None

    def test_drift_folded_into_pending_maps_to_healthy(self):
        """A run that is drifted AND pending-review folds the standalone drift
        Issue into the pending Issue: no drift AnomalyEvent, but action stays
        create_issue so the snapshot is still written elsewhere."""
        iface = StubInterface()
        iface.set(
            "demo@a",
            "demo-model",
            DriftResult(
                action="create_issue",
                drifted=True,
                drift_superseded_by_pending=True,
                run_pending_user_review=True,
                latest_run_id=13509,
                cml_model_id=241,
            ),
        )
        chk = MmpChecker(iface, check_interval_seconds=0)
        result = chk.check_drift(_make_job(), session=FakeSession(jobs=[]))
        assert result.status == CheckStatus.HEALTHY
        assert result.anomaly_event is None
        assert "13509" in result.reason

    def test_skipped_maps_to_skipped(self):
        iface = StubInterface(default=DriftResult(action="skipped", reason="empty"))
        chk = MmpChecker(iface, check_interval_seconds=0)
        result = chk.check_drift(_make_job(), session=FakeSession(jobs=[]))
        assert result.status == CheckStatus.SKIPPED

    def test_inconclusive_maps_to_inconclusive(self):
        iface = StubInterface(default=DriftResult(action="inconclusive", reason="api down"))
        chk = MmpChecker(iface, check_interval_seconds=0)
        result = chk.check_drift(_make_job(), session=FakeSession(jobs=[]))
        assert result.status == CheckStatus.INCONCLUSIVE

    def test_unknown_action_maps_to_inconclusive(self):
        iface = StubInterface(default=DriftResult(action="weird", reason="?"))
        chk = MmpChecker(iface, check_interval_seconds=0)
        result = chk.check_drift(_make_job(), session=FakeSession(jobs=[]))
        assert result.status == CheckStatus.INCONCLUSIVE

    def test_interface_exception_maps_to_inconclusive(self):
        class ExplodingInterface:
            def clear_cache(self):
                pass

            def check_drift(self, **kwargs):
                raise RuntimeError("kaboom")

        chk = MmpChecker(ExplodingInterface(), check_interval_seconds=0)
        result = chk.check_drift(_make_job(), session=FakeSession(jobs=[]))
        assert result.status == CheckStatus.INCONCLUSIVE
        assert "kaboom" in result.reason


# ─────────────────────────── anomaly dispatch ───────────────────────────


class TestAnomalyDispatch:
    def test_create_issue_calls_on_anomaly(self):
        iface = StubInterface()
        iface.set(
            "demo@a",
            "demo-model",
            DriftResult(action="create_issue", drifted=True, cml_model_id=241),
        )
        chk = MmpChecker(iface, check_interval_seconds=0)
        bucket, on_anomaly = _collect_anomalies()
        chk.run_cycle(FakeSession(jobs=[_make_job()]), on_anomaly)
        assert len(bucket) == 1
        assert bucket[0].anomaly_type.name == "MMP_DRIFT"

    def test_drifted_and_pending_run_raises_only_pending_ticket(self):
        """RelayOps test drifted and pending run raises only pending ticket."""
        iface = StubInterface()
        iface.set(
            "demo@a",
            "demo-model",
            DriftResult(
                action="create_issue",
                drifted=True,
                drift_superseded_by_pending=True,
                run_pending_user_review=True,
                run_pending_user_review_details="Run pending user review.",
                latest_run_id=13509,
                cml_model_id=241,
            ),
        )
        chk = MmpChecker(iface, check_interval_seconds=0)
        bucket, on_anomaly = _collect_anomalies()
        chk.run_cycle(FakeSession(jobs=[_make_job()]), on_anomaly)
        assert [ev.anomaly_type.name for ev in bucket] == ["MMP_PENDING_REVIEW"]

    def test_close_issue_does_not_call_on_anomaly(self):
        iface = StubInterface()
        iface.set("demo@a", "demo-model", DriftResult(action="close_issue", drifted=False))
        chk = MmpChecker(iface, check_interval_seconds=0)
        bucket, on_anomaly = _collect_anomalies()
        chk.run_cycle(FakeSession(jobs=[_make_job()]), on_anomaly)
        assert bucket == []

    def test_skipped_does_not_call_on_anomaly(self):
        iface = StubInterface(default=DriftResult(action="skipped"))
        chk = MmpChecker(iface, check_interval_seconds=0)
        bucket, on_anomaly = _collect_anomalies()
        chk.run_cycle(FakeSession(jobs=[_make_job()]), on_anomaly)
        assert bucket == []


class TestRecoveryEvents:
    """build_recovery_events — per-flag recovery plus the approval-supersedes
    timestamp-scoped bulk close."""

    def test_no_approval_close_when_latest_run_not_approved(self):
        from core.checker.mmp_checker import build_recovery_events

        drift = DriftResult(
            action="create_issue",
            drifted=True,
            latest_run_id=200,
            latest_run_approved=False,
        )
        events = build_recovery_events(drift, _make_job())
        # drifted=True → no drift recovery; not approved → no timestamp close.
        assert all(ev.close_created_before is None for ev in events)

    def test_fold_close_when_drift_superseded_by_pending(self):
        """drifted=True but folded into pending → close any already-open drift
        Issue for this job (so drift-then-pending converges to one Issue)."""
        from core.checker.mmp_checker import build_recovery_events
        from core.models.entities import IssueType

        drift = DriftResult(
            action="create_issue",
            drifted=True,
            drift_superseded_by_pending=True,
            run_pending_user_review=True,
            latest_run_id=13509,
        )
        events = build_recovery_events(drift, _make_job(job_id=7, product_id=42))
        drift_closes = [ev for ev in events if ev.issue_type == IssueType.MMP_DRIFT]
        assert len(drift_closes) == 1
        ev = drift_closes[0]
        assert ev.close_created_before is None  # closes regardless of age
        assert ev.job_id == 7
        assert "13509" in ev.reason

    def test_no_fold_close_when_drift_not_superseded(self):
        """Plain drift with no pending flag must NOT be fold-closed — its
        standalone Issue is the right ticket."""
        from core.checker.mmp_checker import build_recovery_events
        from core.models.entities import IssueType

        drift = DriftResult(
            action="create_issue",
            drifted=True,
            drift_superseded_by_pending=False,
            latest_run_id=13600,
        )
        events = build_recovery_events(drift, _make_job())
        assert [ev for ev in events if ev.issue_type == IssueType.MMP_DRIFT] == []

    def test_no_approval_close_when_approval_timestamp_missing(self):
        from core.checker.mmp_checker import build_recovery_events

        # Approved but MMP gave us no parseable approval timestamp → we have
        # nothing to anchor the cutoff on, so the bulk close does not fire.
        drift = DriftResult(
            action="create_issue",
            drifted=True,
            latest_run_id=200,
            latest_run_approved=True,
            latest_run_approved_at=None,
        )
        events = build_recovery_events(drift, _make_job())
        assert all(ev.close_created_before is None for ev in events)

    def test_approval_closes_drift_and_pending_before_timestamp(self):
        from core.checker.base import RecoveryEvent  # noqa: F401
        from core.checker.mmp_checker import build_recovery_events
        from core.models.entities import IssueType

        approved_at = datetime(2026, 5, 12, 11, 21, 58)
        # Model-level drift flag still True (stale aggregate) but the latest
        # production run is approved → drift/pending Issues created before the
        # approval are closed in bulk.
        drift = DriftResult(
            action="create_issue",
            drifted=True,
            latest_run_id=200,
            latest_run_approved=True,
            latest_run_approved_at=approved_at,
        )
        events = build_recovery_events(drift, _make_job(job_id=7, product_id=42))
        scoped = [ev for ev in events if ev.close_created_before is not None]
        assert {ev.issue_type for ev in scoped} == {
            IssueType.MMP_DRIFT,
            IssueType.MMP_RUN_PENDING_APPROVAL,
            IssueType.MMP_PENDING_REVIEW,
        }
        for ev in scoped:
            assert ev.close_created_before == approved_at
            assert ev.job_id == 7
            assert ev.product_id == 42
