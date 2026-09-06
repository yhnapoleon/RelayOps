"""CML Checker — drives Ops's job monitoring loop against the CML v2 contract.

Per cycle:
  1. Load all active Jobs that have both ``cml_project_id`` and ``cml_job_id``
     set (the service layer is responsible for resolving names -> ids; an
     unresolved Job is silently skipped here so a single bad binding doesn't
     block the whole tick).
  2. For each Job, fetch a fixed window of the most recent runs
     (``max_pages`` × ``page_size``) via
     ``ControlInterface.list_job_runs_recent`` and dedup against the set
     of ``cml_run_id`` already in ``JobExecution`` for this Job. This lets
     historical gaps self-heal: a run missed by an earlier tick (whether
     from Controller downtime, a commit-time rollback, or a too-narrow
     polling window) is re-fetched every subsequent tick and inserted
     once until it falls outside the window. The first time a Job is seen
     (no prior rows) only the latest page is fetched to avoid bootstrapping
     years of history.
  3. The most recent run determines the anomaly decision. A job is only
     "healthy" when its latest run has COMPLETED SUCCESSFULLY within its SLA
     window — anything else that has overrun its expected runtime is a miss:
        * ENGINE_FAILED / ENGINE_TIMEOUT       -> JOB_FAILED
        * ENGINE_SUCCEEDED with stale finish   -> JOB_STALE
        * ENGINE_STOPPED                       -> JOB_STALE (treated as a miss)
        * ENGINE_RUNNING / STARTING that has been in-flight past its SLA
          threshold (i.e. should already have finished) -> JOB_STALE (timeout)
        * ENGINE_RUNNING / STARTING still within its SLA window -> no anomaly
        * ENGINE_SCHEDULING / SKIPPED / UNKNOWN -> no anomaly (informational)

Each JOB_FAILED / JOB_STALE anomaly carries the run's ``cml_run_id`` as a
dedup key so the issue layer raises exactly one Issue per distinct event
(per failed/stale run) rather than suppressing new events behind a still-open
earlier Issue.

API errors (timeout, 401, 403) degrade to INCONCLUSIVE so we never raise a
false JOB_FAILED purely because CML was unreachable.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from sqlalchemy import and_

from core.checker.base import AnomalyEvent, AnomalyType, CheckResult, CheckStatus
from core.integrations.control_interface import CmlApiError
from core.integrations.normalization import _normalize_controlm_status
from core.integrations.staleness import is_within_cron_schedule, validate_timestamp
from core.logging import get_logger
from core.models.entities import Job, JobExecution, Product, ProductStatus

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from core.integrations.control_interface import ControlInterface

logger = get_logger(__name__)


def _parse_iso(value) -> Optional[datetime]:
    """Best-effort parse of CML's ISO-8601 timestamps into a naive datetime.

    CML returns Go zero-value timestamps (``0001-01-01T00:00:00Z``) for run
    fields that haven't been populated yet (a queued run that hasn't started
    has no ``running_at`` / ``finished_at``, and on some workspaces even
    ``created_at`` comes back zero). Those parse to ``datetime(1, 1, 1, ...)``
    which YugabyteDB later displays as ``1901-01-01`` in the UI. Reject any
    year before 1970 here so the persistence path treats them as "no usable
    timestamp" and falls back / skips.
    """
    if value in (None, ""):
        return None
    parsed: Optional[datetime] = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.utcfromtimestamp(value)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    if parsed is None or parsed.year < 1970:
        return None
    return parsed


def _run_finished_at(run: dict) -> Optional[str]:
    """Pick the most informative timestamp for staleness checks.

    For terminal runs we prefer ``finished_at``; if absent, fall back to
    ``running_at`` and finally ``created_at`` so jobs never look more stale
    than they actually are.
    """
    return run.get("finished_at") or run.get("running_at") or run.get("created_at")


def _run_started_at(run: dict) -> Optional[str]:
    """Pick the timestamp marking when an in-flight run began.

    Used by the running-too-long timeout check: a run that hasn't finished has
    no ``finished_at``, so we measure elapsed runtime from ``running_at`` (when
    execution actually started) and fall back to ``created_at`` (enqueue time).
    """
    return run.get("running_at") or run.get("created_at")


def _run_triggered_at(run: dict) -> Optional[str]:
    """Pick the timestamp closest to when the schedule would have fired.

    Used to decide whether a run lines up with the job's cron. The scheduler
    creates the run at (≈) the fire minute, so prefer ``created_at``; fall back
    to ``running_at`` then ``finished_at`` when CML omitted the earlier fields.
    """
    return run.get("created_at") or run.get("running_at") or run.get("finished_at")


# Strictness presets the user picks in the create/edit Job form. Maps to the
# safety factor that scales the cron-derived interval into a staleness
# threshold (Strict = no tolerance, Loose = up to 3× the interval).
_SLA_PRESET_FACTORS: dict = {
    "strict": 1.0,
    "normal": 1.5,
    "loose": 3.0,
}


def _safety_factor_for(preset) -> Optional[float]:
    """Map a Job.sla_preset string to its numeric safety factor.

    Returns ``None`` (defer to the global default) when the preset is unset,
    "normal", or unrecognised — so an unknown value never blows up the check.
    """
    if preset is None:
        return None
    key = str(preset).strip().lower()
    if not key or key == "normal":
        return None  # equivalent to the global _CRON_SAFETY_FACTOR
    return _SLA_PRESET_FACTORS.get(key)


class CmlChecker:
    """CML branch — owns the v2 polling, run-snapshot persistence, and JOB_* anomalies."""

    def __init__(
        self,
        control_interface: "ControlInterface",
        *,
        staleness_threshold_minutes: int = 120,
    ) -> None:
        self._control_interface = control_interface
        self._staleness_threshold_minutes = staleness_threshold_minutes

    # ------------------------------------------------------------------ #
    # "Check now" — single-job manual probe used by API routers
    # ------------------------------------------------------------------ #

    def check_job(self, job: "Job") -> CheckResult:
        if not job.cml_project_id or not job.cml_job_id:
            return CheckResult(
                status=CheckStatus.SKIPPED,
                reason=(
                    f"Job {job.id} has no resolved CML binding "
                    f"(cml_project_id={job.cml_project_id!r}, "
                    f"cml_job_id={job.cml_job_id!r})"
                ),
            )

        try:
            runs = self._control_interface.list_job_runs(
                job.cml_project_id, job.cml_job_id, limit=1
            )
        except CmlApiError as exc:
            logger.warning(
                "CmlChecker: CML API error checking job {} (id={}): {}",
                job.cml_job_id, job.id, exc,
            )
            return CheckResult(
                status=CheckStatus.INCONCLUSIVE,
                reason=f"CML API error for job {job.cml_job_id}: {exc}",
            )
        except Exception as exc:
            logger.warning(
                "CmlChecker: unexpected error checking job {} (id={}): {}",
                job.cml_job_id, job.id, exc,
            )
            return CheckResult(
                status=CheckStatus.INCONCLUSIVE,
                reason=f"Unexpected error for job {job.cml_job_id}: {exc}",
            )

        if not runs:
            return CheckResult(
                status=CheckStatus.HEALTHY,
                reason=f"Job {job.cml_job_id} has no run history yet",
            )
        return self._evaluate_run(job, runs[0])

    # ------------------------------------------------------------------ #
    # Periodic loop — owned by Controller.tick()
    # ------------------------------------------------------------------ #

    def run_cycle(self, session: "Session", on_anomaly: Callable[[AnomalyEvent], None]) -> None:
        # Monitor pre-approval products too (DRAFT / PENDING_REVIEW) so owners
        # see asset health before submitting for review. Only ARCHIVED is
        # excluded — those are decommissioned, anomalies on them are noise.
        jobs = (
            session.query(Job)
            .join(Product, Job.product_id == Product.id)
            .filter(
                and_(
                    Product.status.in_(
                        [
                            ProductStatus.DRAFT,
                            ProductStatus.PENDING_REVIEW,
                            ProductStatus.ACTIVE,
                        ]
                    ),
                    Job.cml_project_id.isnot(None),
                    Job.cml_project_id != "",
                    Job.cml_job_id.isnot(None),
                    Job.cml_job_id != "",
                )
            )
            .all()
        )
        if not jobs:
            logger.debug("CmlChecker: no active jobs with resolved CML binding")
            return

        # Drop legacy rows whose timestamp parsed to year < 1970 — those came
        # from CML returning a zero-value timestamp (e.g. "0001-01-01T00:00:00Z"
        # for queued/scheduling runs) before _parse_iso started rejecting them.
        # YugabyteDB displays year=1 as "1901-01-01" in the UI; this sweep keeps
        # the Recent Runs list clean. Idempotent: a no-op once the table is
        # clean, so safe to run every tick.
        deleted = (
            session.query(JobExecution)
            .filter(JobExecution.timestamp < datetime(1970, 1, 1))
            .delete(synchronize_session=False)
        )
        if deleted:
            logger.info("CmlChecker: pruned {} legacy zero-timestamp execution row(s)", deleted)

        snapshots_added = 0
        cycle_started_at = datetime.utcnow()
        for job in jobs:
            # Pre-load every cml_run_id Ops already has for this Job. The
            # fetcher always pulls a fixed window of recent runs (no
            # watermark), so dedup happens in-memory before we ever call
            # _persist_snapshot — that's what lets historical gaps inside
            # the window self-heal (each tick re-fetches them; the ones
            # still missing get inserted, the rest are skipped here).
            known_run_ids: set = set(
                row[0]
                for row in session.query(JobExecution.cml_run_id)
                .filter(
                    JobExecution.job_id == job.id,
                    JobExecution.cml_run_id.isnot(None),
                )
                .all()
            )

            try:
                runs = self._control_interface.list_job_runs_recent(
                    job.cml_project_id,
                    job.cml_job_id,
                    bootstrap=not known_run_ids,
                )
            except CmlApiError as exc:
                # The poll attempt still counts as a "check" — surface its
                # timestamp so the UI can show staleness of the check itself.
                job.last_checked_at = cycle_started_at
                logger.warning(
                    "CmlChecker: CML API error for job {} (id={}, project={}): {}",
                    job.cml_job_id, job.id, job.cml_project_id, exc,
                )
                continue
            except Exception as exc:
                job.last_checked_at = cycle_started_at
                logger.warning(
                    "CmlChecker: unexpected error for job {} (id={}): {}",
                    job.cml_job_id, job.id, exc,
                )
                continue

            job.last_checked_at = cycle_started_at

            for run in runs:
                run_id = run.get("id")
                if run_id and run_id in known_run_ids:
                    continue
                if self._persist_snapshot(session, job, run):
                    snapshots_added += 1
                    if run_id:
                        known_run_ids.add(run_id)

            # Latest run drives the anomaly decision.
            if runs:
                result = self._evaluate_run(job, runs[0])
                if result.status == CheckStatus.ANOMALY and result.anomaly_event:
                    on_anomaly(result.anomaly_event)

        if snapshots_added:
            logger.debug("CmlChecker: queued {} new execution snapshot(s)", snapshots_added)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _persist_snapshot(self, session, job: "Job", run: dict) -> bool:
        """Insert a JobExecution row if (job_id, cml_run_id) is new.

        Returns True on insert, False on dedup hit / unparseable timestamp.
        """
        run_id = run.get("id")
        if not run_id:
            return False

        already = (
            session.query(JobExecution.id)
            .filter(JobExecution.job_id == job.id, JobExecution.cml_run_id == run_id)
            .first()
        )
        if already is not None:
            return False

        ts = _parse_iso(_run_finished_at(run))
        if ts is None:
            # CML returned only zero-value timestamps — typical for a freshly
            # queued run that hasn't even been assigned a created_at yet. Skip
            # this run; next tick will re-fetch it and (hopefully) find usable
            # timestamps, at which point the (job_id, cml_run_id) dedup will
            # let it land. Better than persisting a row that displays as
            # "1901-01-01" forever in the UI.
            return False

        try:
            normalized = _normalize_controlm_status(run.get("status"))
        except Exception:
            normalized = "unknown"

        try:
            session.add(JobExecution(
                job_id=job.id,
                status=normalized,
                timestamp=ts,
                cml_run_id=run_id,
                metadata_json={
                    "source": "cml_v2_polling",
                    "cml_status": run.get("status"),
                    "cml_run_id": run_id,
                    "failure_reason": run.get("failure_reason"),
                    "arguments": run.get("arguments"),
                    "runtime_identifier": run.get("runtime_identifier"),
                },
                received_at=datetime.utcnow(),
            ))
            return True
        except Exception:
            logger.opt(exception=True).warning(
                "CmlChecker: failed to persist snapshot for job {} run {}",
                job.id, run_id,
            )
            return False

    # ------------------------------------------------------------------ #
    # Run-status -> CheckResult
    # ------------------------------------------------------------------ #

    def _evaluate_run(self, job: "Job", run: dict) -> CheckResult:
        cml_status = str(run.get("status") or "")
        try:
            normalized = _normalize_controlm_status(cml_status)
        except Exception:
            normalized = "unknown"

        run_id = run.get("id")
        finished_iso = _run_finished_at(run)

        if normalized == "failed":
            # A run that failed *outside* the job's cron window (e.g. an ad-hoc
            # run on a day/time the schedule never covers — Monday for a
            # "0 5 * * 2-5" Tue–Fri job) is recorded as a run snapshot but does
            # NOT raise an Issue: the schedule didn't ask for that execution, so
            # its failure shouldn't page the on-duty Ops. is_within_cron_schedule
            # fails open (cron empty/unparseable or no usable trigger time ->
            # treat as in-window) so we never silently swallow a real failure.
            cron = job.schedule_cron or job.control_m_cron or None
            triggered_at = _parse_iso(_run_triggered_at(run))
            if not is_within_cron_schedule(cron, triggered_at):
                return CheckResult(
                    status=CheckStatus.HEALTHY,
                    reason=(
                        f"Job {job.cml_job_id} run {run_id} FAILED but its trigger "
                        f"time {triggered_at.isoformat() if triggered_at else 'unknown'} "
                        f"is outside the cron schedule {cron!r}; recorded without "
                        f"raising an issue"
                    ),
                )
            return CheckResult(
                status=CheckStatus.ANOMALY,
                anomaly_event=AnomalyEvent(
                    anomaly_type=AnomalyType.JOB_FAILED,
                    title=f"CML Job failed: {job.cml_job_name or job.cml_job_id}",
                    description=(
                        f"CML reported status={cml_status} for the latest run.\n"
                        f"Job Name: {job.cml_job_name}\n"
                        f"Ops Job ID: {job.id}\n"
                        f"Product ID: {job.product_id}\n"
                        f"CML Project: {job.cml_project_id}\n"
                        f"CML Job: {job.cml_job_id}\n"
                        f"Run ID: {run_id}\n"
                        f"Finished At: {finished_iso or 'unknown'}\n"
                        f"Failure Reason: {run.get('failure_reason') or 'not provided'}"
                    ),
                    product_id=job.product_id,
                    job_id=job.id,
                    dedup_key=str(run_id) if run_id else None,
                    metadata={
                        "cml_status": cml_status,
                        "cml_run_id": run_id,
                        "cml_project_id": job.cml_project_id,
                        "cml_job_id": job.cml_job_id,
                        "finished_at": finished_iso,
                        "failure_reason": run.get("failure_reason"),
                    },
                ),
                reason=f"CML status is {cml_status} for run {run_id}",
            )

        if normalized == "completed":
            staleness = validate_timestamp(
                last_run_date=finished_iso,
                sla_threshold_minutes=self._staleness_threshold_minutes,
                cron=(job.schedule_cron or job.control_m_cron or None),
                sla_safety_factor=_safety_factor_for(getattr(job, "sla_preset", None)),
                sla_override_minutes=getattr(job, "sla_custom_minutes", None),
            )
            if staleness.is_stale:
                return CheckResult(
                    status=CheckStatus.ANOMALY,
                    anomaly_event=AnomalyEvent(
                        anomaly_type=AnomalyType.JOB_STALE,
                        title=f"CML Job stale: {job.cml_job_name or job.cml_job_id}",
                        description=(
                            f"Latest run is ENGINE_SUCCEEDED but finished_at is stale.\n"
                            f"Job Name: {job.cml_job_name}\n"
                            f"Ops Job ID: {job.id}\n"
                            f"Run ID: {run_id}\n"
                            f"Finished At: {finished_iso}\n"
                            f"Age: {staleness.age_minutes:.0f} min\n"
                            f"SLA Threshold: {staleness.threshold_minutes} min "
                            f"(per-job, derived from cron when available; "
                            f"falls back to global "
                            f"{self._staleness_threshold_minutes} min)\n"
                            f"Reason: {staleness.reason}"
                        ),
                        product_id=job.product_id,
                        job_id=job.id,
                        dedup_key=str(run_id) if run_id else None,
                        metadata={
                            "cml_status": cml_status,
                            "cml_run_id": run_id,
                            "finished_at": finished_iso,
                            "age_minutes": staleness.age_minutes,
                            "threshold_minutes": staleness.threshold_minutes,
                            "staleness_reason": staleness.reason,
                        },
                    ),
                    reason=(
                        f"Job {job.cml_job_id} stale: {staleness.reason}"
                    ),
                )
            return CheckResult(
                status=CheckStatus.HEALTHY,
                reason=(
                    f"Job {job.cml_job_id} is {cml_status} and fresh "
                    f"({staleness.age_minutes:.0f} min old)"
                ),
            )

        if normalized == "stopped":
            # ENGINE_STOPPED is a terminal "operator stopped" state — treat
            # as a miss so the Ops on-duty member investigates.
            return CheckResult(
                status=CheckStatus.ANOMALY,
                anomaly_event=AnomalyEvent(
                    anomaly_type=AnomalyType.JOB_STALE,
                    title=f"CML Job stopped: {job.cml_job_name or job.cml_job_id}",
                    description=(
                        f"CML reports the job's latest run was stopped.\n"
                        f"Job Name: {job.cml_job_name}\n"
                        f"Ops Job ID: {job.id}\n"
                        f"Run ID: {run_id}\n"
                        f"Finished At: {finished_iso or 'unknown'}"
                    ),
                    product_id=job.product_id,
                    job_id=job.id,
                    dedup_key=str(run_id) if run_id else None,
                    metadata={
                        "cml_status": cml_status,
                        "cml_run_id": run_id,
                        "finished_at": finished_iso,
                    },
                ),
                reason=f"Latest run for {job.cml_job_id} was stopped",
            )

        if normalized == "running":
            # In-flight run. It is only "healthy" while it is still within its
            # expected runtime; a run that should already have completed (i.e.
            # has been running past its SLA threshold) is a timeout/miss. We
            # reuse the same per-job threshold as the completed-but-stale check,
            # anchored on when the run STARTED rather than when it finished.
            started_iso = _run_started_at(run)
            if started_iso:
                staleness = validate_timestamp(
                    last_run_date=started_iso,
                    sla_threshold_minutes=self._staleness_threshold_minutes,
                    cron=(job.schedule_cron or job.control_m_cron or None),
                    sla_safety_factor=_safety_factor_for(getattr(job, "sla_preset", None)),
                    sla_override_minutes=getattr(job, "sla_custom_minutes", None),
                )
                if staleness.is_stale:
                    return CheckResult(
                        status=CheckStatus.ANOMALY,
                        anomaly_event=AnomalyEvent(
                            anomaly_type=AnomalyType.JOB_STALE,
                            title=f"CML Job timed out: {job.cml_job_name or job.cml_job_id}",
                            description=(
                                f"Latest run is still {cml_status} but has been "
                                f"in-flight past its expected runtime — it should "
                                f"already have completed.\n"
                                f"Job Name: {job.cml_job_name}\n"
                                f"Ops Job ID: {job.id}\n"
                                f"Run ID: {run_id}\n"
                                f"Started At: {started_iso}\n"
                                f"Running For: {staleness.age_minutes:.0f} min\n"
                                f"SLA Threshold: {staleness.threshold_minutes} min "
                                f"(per-job, derived from cron when available; "
                                f"falls back to global "
                                f"{self._staleness_threshold_minutes} min)\n"
                                f"Reason: {staleness.reason}"
                            ),
                            product_id=job.product_id,
                            job_id=job.id,
                            dedup_key=str(run_id) if run_id else None,
                            metadata={
                                "cml_status": cml_status,
                                "cml_run_id": run_id,
                                "started_at": started_iso,
                                "age_minutes": staleness.age_minutes,
                                "threshold_minutes": staleness.threshold_minutes,
                                "staleness_reason": staleness.reason,
                                "timeout": True,
                            },
                        ),
                        reason=(
                            f"Job {job.cml_job_id} running too long: {staleness.reason}"
                        ),
                    )

        # waiting / scheduling / skipped / unknown (and running-but-fresh)
        # -> no anomaly.
        return CheckResult(
            status=CheckStatus.HEALTHY,
            reason=f"Job {job.cml_job_id} status is {cml_status}; no action",
        )
