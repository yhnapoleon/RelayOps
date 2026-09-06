"""
MMP Checker branch — wraps MmpInterface to detect model concerns.

Per cycle:
- An internal interval guard returns early when the configured MMP cadence
  hasn't elapsed (MMP is slow; we don't want to hit it on every Controller
  tick).
- Loads MMP-bound Jobs from active / pre-approval Products, asks
  ``MmpInterface.check_drift`` for each one. Multiple Jobs sharing the same
  CML project share a single Step 2 detail call via the interface's
  project-detail cache.
- Writes one ``MmpDriftSnapshot`` row per successful drift read
  (``create_issue`` or ``close_issue``). INCONCLUSIVE / SKIPPED outcomes do
  not write snapshots — Ops's local timeline stays free of phantom rows.

MMP monitoring tracks three signals. One ``check_drift`` HTTP call reads all
of MMP's ``attention_required`` flags; three of them create Issues, the rest
are read but ignored:
- ``model_drifted``           → AnomalyType.MMP_DRIFT
- ``run_pending_approval``    → AnomalyType.MMP_RUN_PENDING_APPROVAL
- ``run_pending_user_review`` → AnomalyType.MMP_PENDING_REVIEW
- ``has_fairness_risk`` / ``has_unapproved_exp_run`` → read but NOT emitted

Every monitored signal that reads healthy again emits a RecoveryEvent (via
``build_recovery_events``) so the Controller can auto-close the matching open
Issue. The drift event description is enriched with the latest production
run's pmetric/fmean/fmissing breakdown, and all three Issue descriptions
embed the current drift/approval/review status block
(``build_mmp_status_lines``).

Action → CheckStatus mapping (drift branch only — the other three signals
ride alongside via ``run_cycle``'s extra-concerns dispatch):
- "create_issue"  → ANOMALY with MMP_DRIFT AnomalyEvent, UNLESS the same
                    latest run is also pending review/approval
                    (``drift_superseded_by_pending``): then the drift Issue is
                    folded into the pending Issue and this maps to HEALTHY (the
                    snapshot is still written; ``drifted`` stays True).
- "close_issue"   → HEALTHY (recovery; existing Issues NOT auto-closed)
- "skipped"       → SKIPPED (data inconsistency: empty binding, unresolved
                    name, model not in project)
- "inconclusive"  → INCONCLUSIVE (MMP API failed; retry next cycle)
- Any exception   → INCONCLUSIVE (no false positives)
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from sqlalchemy import and_

from core.checker.base import (
    AnomalyEvent,
    AnomalyType,
    CheckResult,
    CheckStatus,
    RecoveryEvent,
)
from core.logging import get_logger
from core.models.entities import IssueType, Job, Product, ProductStatus
from core.models.mmp_entities import MmpDriftSnapshot

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from core.integrations.mmp_interface import DriftResult, MmpInterface

logger = get_logger(__name__)


class MmpChecker:
    """MMP branch. Wraps MmpInterface, detects model drift on its own cadence."""

    def __init__(
        self,
        mmp_interface: "MmpInterface",
        check_interval_seconds: int = 900,
    ) -> None:
        self._mmp_interface = mmp_interface
        self._check_interval_seconds = int(check_interval_seconds)
        self._last_run_at: Optional[datetime] = None

    def run_cycle(
        self,
        session: "Session",
        on_anomaly: Callable[[AnomalyEvent], None],
        on_recovery: Optional[Callable[["RecoveryEvent"], None]] = None,
    ) -> None:
        """One full cycle: skip if not yet due, else load Jobs, check drift,
        dispatch anomalies, persist a drift-state row per successful read.

        Monitor pre-approval products too (DRAFT / PENDING_REVIEW) so owners
        see drift signals before submitting for review. Only ARCHIVED is
        excluded — those are decommissioned, anomalies on them are noise.

        ``on_recovery`` (optional) is invoked once per monitored signal that
        reads healthy again (drift / pending-approval / pending-review back to
        normal) so the caller can auto-close the matching open Issue. Left
        ``None`` by unit tests that only assert on anomaly dispatch.
        """
        now = datetime.utcnow()
        if self._last_run_at is not None:
            elapsed = (now - self._last_run_at).total_seconds()
            if elapsed < self._check_interval_seconds:
                logger.debug(
                    "MmpChecker: skipping cycle (last run {:.0f}s ago, interval {}s)",
                    elapsed,
                    self._check_interval_seconds,
                )
                return

        jobs = (
            session.query(Job)
            .join(Product, Job.product_id == Product.id)
            .filter(and_(
                Product.status.in_([
                    ProductStatus.DRAFT,
                    ProductStatus.PENDING_REVIEW,
                    ProductStatus.ACTIVE,
                ]),
                Job.has_mmp_dependency.is_(True),
            ))
            .all()
        )
        if not jobs:
            logger.debug("MmpChecker: no active MMP jobs to check")
            self._last_run_at = now
            return

        # Reset interface caches so this cycle sees one consistent snapshot
        # across all per-job lookups. Multiple Jobs in the same CML project
        # then share the project-detail cache automatically.
        self._mmp_interface.clear_cache()

        for job in jobs:
            result, drift_result = self._check_one(job, session=session)
            if result.status == CheckStatus.ANOMALY and result.anomaly_event:
                on_anomaly(result.anomaly_event)
            # Pending-approval / pending-review ride alongside drift — one HTTP
            # call, up to two extra AnomalyEvents. Only emitted when the drift
            # call actually fetched model detail (action in create_issue /
            # close_issue); SKIPPED / INCONCLUSIVE leave the flags None so
            # nothing fires.
            if drift_result is not None:
                for extra in build_extra_concern_events(drift_result, job):
                    on_anomaly(extra)
                # Auto-close: every monitored signal that reads healthy again
                # emits a RecoveryEvent so the controller can close its Issue.
                if on_recovery is not None:
                    for recovery in build_recovery_events(drift_result, job):
                        on_recovery(recovery)

        # Always advance last_run_at so we honour the configured cadence even
        # when MMP is unreachable (otherwise we'd retry on every Controller
        # tick and burn API budget for nothing).
        self._last_run_at = now

    def check_drift(self, job: "Job", session: Optional["Session"] = None) -> CheckResult:
        """Public single-job check — used by product_service "Check Now".

        Returns only the drift CheckResult (legacy contract). Callers that
        need the other three concerns (fairness, run-pending-approval,
        unapproved-exp-run) should call ``check_concerns`` instead, which
        returns the same drift CheckResult plus the underlying DriftResult
        so they can call ``build_extra_concern_events`` themselves.
        """
        result, _drift = self._check_one(job, session=session)
        return result

    def check_concerns(
        self, job: "Job", session: Optional["Session"] = None
    ) -> tuple[CheckResult, Optional["DriftResult"]]:
        """Public single-job check returning the drift CheckResult AND
        the raw DriftResult, so callers can build the additional Issues
        for fairness/approval/unapproved without re-hitting MMP.
        """
        return self._check_one(job, session=session)

    def _check_one(
        self, job: "Job", session: Optional["Session"] = None
    ) -> tuple[CheckResult, Optional["DriftResult"]]:
        """Run one MMP check; map drift to a CheckResult and return the
        raw DriftResult so the caller can emit extra concern events.

        When ``session`` is provided AND the drift read succeeded (action
        ``create_issue`` or ``close_issue``), a row is appended to
        ``MmpDriftSnapshot`` — the source of Ops's local drift timeline.
        Snapshots are NOT written on SKIPPED / INCONCLUSIVE outcomes so the
        timeline doesn't accumulate phantom entries from outages or stale
        bindings.
        """
        try:
            drift_result = self._mmp_interface.check_drift(
                repo_name=job.mmp_project_id or "",
                model_name=job.mmp_model_id or "",
                job=job,
            )
        except Exception as exc:
            logger.warning(
                "MmpChecker: unexpected error checking drift for job {}: {}",
                job.id,
                exc,
            )
            return (
                CheckResult(
                    status=CheckStatus.INCONCLUSIVE,
                    reason=f"Unexpected error during MMP drift check for job {job.id}: {exc}",
                ),
                None,
            )

        if session is not None and drift_result.action in ("create_issue", "close_issue"):
            session.add(MmpDriftSnapshot(
                job_id=job.id,
                cml_model_id=drift_result.cml_model_id,
                drifted=drift_result.drifted,
                drift_details=drift_result.drift_details,
            ))

        return self._map_drift_result(drift_result, job), drift_result

    def _map_drift_result(self, drift_result: "DriftResult", job: "Job") -> CheckResult:
        """Map a DriftResult to a CheckResult."""
        action = drift_result.action
        mmp_project_id = job.mmp_project_id or ""
        mmp_model_id = job.mmp_model_id or ""

        if action == "create_issue":
            # Fold the standalone drift Issue into the pending-review/-approval
            # Issue when the same latest run is both drifted and pending: the
            # pending Issue already embeds the drift status block, so a separate
            # drift ticket for the same run is a confusing duplicate. We return
            # HEALTHY (no drift AnomalyEvent), but action stays "create_issue"
            # so ``_check_one`` still writes the MmpDriftSnapshot (drifted=True
            # — the timeline stays factual), and ``drifted`` stays True so
            # ``build_recovery_events`` does NOT auto-close a real drift concern.
            # Only fires when a pending Issue actually covers this run; a drifted
            # run with no pending flag still raises its own drift Issue.
            if drift_result.drift_superseded_by_pending:
                return CheckResult(
                    status=CheckStatus.HEALTHY,
                    reason=(
                        f"drift folded into pending-review/-approval issue for "
                        f"latest run #{drift_result.latest_run_id} "
                        f"({mmp_project_id}/{mmp_model_id})"
                    ),
                )

            from core.config import get_config

            cml_model_id = drift_result.cml_model_id
            drift_details = drift_result.drift_details or "N/A"
            sub_flag_lines = build_drift_subflag_lines(drift_result)
            sub_flag_block = f"\n{sub_flag_lines}\n" if sub_flag_lines else ""
            status_block = f"\n{build_mmp_status_lines(drift_result)}\n"
            external_url = get_config().mmp_model_web_url(drift_result.mmp_project_numeric_id)
            metadata = {
                "mmp_project_id": mmp_project_id,
                "mmp_model_id": mmp_model_id,
                "cml_model_id": cml_model_id,
                "mmp_project_numeric_id": drift_result.mmp_project_numeric_id,
                "external_url": external_url or None,
                "drifted": True,
                "drift_details": drift_result.drift_details,
                "latest_run_id": drift_result.latest_run_id,
                "pmetric_drifted": drift_result.latest_run_pmetric_drifted,
                "fmean_drifted": drift_result.latest_run_fmean_drifted,
                "fmissing_drifted": drift_result.latest_run_fmissing_drifted,
            }
            return CheckResult(
                status=CheckStatus.ANOMALY,
                anomaly_event=AnomalyEvent(
                    anomaly_type=AnomalyType.MMP_DRIFT,
                    title=f"MMP Drift detected: {mmp_project_id}/{mmp_model_id}",
                    description=(
                        f"MMP drift detected for model.\n"
                        f"\n"
                        f"MMP Project: {mmp_project_id}\n"
                        f"MMP Model: {mmp_model_id}\n"
                        f"CML Model ID: {cml_model_id if cml_model_id is not None else 'N/A'}\n"
                        f"Ops Job ID: {job.id}\n"
                        f"Product ID: {job.product_id}\n"
                        f"Details: {drift_details}\n"
                        f"Reason: {drift_result.reason}"
                        + (f"\nAction on MMP: {external_url}\n" if external_url else "")
                        + f"{sub_flag_block}"
                        f"{status_block}"
                    ),
                    product_id=job.product_id,
                    job_id=job.id,
                    metadata=metadata,
                ),
                reason=drift_result.reason,
            )

        if action == "close_issue":
            return CheckResult(status=CheckStatus.HEALTHY, reason=drift_result.reason)

        if action == "skipped":
            return CheckResult(status=CheckStatus.SKIPPED, reason=drift_result.reason)

        if action == "inconclusive":
            return CheckResult(status=CheckStatus.INCONCLUSIVE, reason=drift_result.reason)

        logger.warning(
            "MmpChecker: unknown DriftResult action '{}' for job {}",
            action,
            job.id,
        )
        return CheckResult(
            status=CheckStatus.INCONCLUSIVE,
            reason=f"Unknown DriftResult action '{action}' for job {job.id}",
        )


def build_drift_subflag_lines(drift_result: "DriftResult") -> str:
    """Render the latest-run drift sub-flag block for the Issue
    description. Returns "" when MMP gave us no production run to read
    from (the section is hidden rather than printed with "Unknown" rows
    so the runbook author isn't left wondering whether MMP is broken).
    """
    if drift_result.latest_run_id is None:
        return ""
    pieces = []
    if drift_result.latest_run_pmetric_drifted is not None:
        pieces.append(
            f"  - Performance metric drift (pmetric_drifted): "
            f"{'yes' if drift_result.latest_run_pmetric_drifted else 'no'}"
        )
    if drift_result.latest_run_fmean_drifted is not None:
        pieces.append(
            f"  - Feature mean drift (fmean_drifted): "
            f"{'yes' if drift_result.latest_run_fmean_drifted else 'no'}"
        )
    if drift_result.latest_run_fmissing_drifted is not None:
        pieces.append(
            f"  - Feature missing drift (fmissing_drifted): "
            f"{'yes' if drift_result.latest_run_fmissing_drifted else 'no'}"
        )
    if not pieces:
        return ""
    header = f"Latest production run #{drift_result.latest_run_id} sub-flags:"
    return header + "\n" + "\n".join(pieces)


def build_mmp_status_lines(drift_result: "DriftResult") -> str:
    """Render the three monitored MMP signals (drift / pending-approval /
    pending-review) as a compact human-readable block, embedded in every MMP
    Issue description so a responder sees all three at a glance.

    Pending-approval / pending-review lines are omitted when MMP didn't report
    the flag (None) — drift is always shown since it's the legacy contract.
    """
    lines = ["MMP attention signals:"]
    if drift_result.drifted:
        lines.append(f"  - Drift: DRIFTED — {drift_result.drift_details or 'no description'}")
    else:
        lines.append(f"  - Drift: no drift — {drift_result.drift_details or 'no description'}")
    if drift_result.run_pending_approval is not None:
        if drift_result.run_pending_approval:
            lines.append(
                f"  - Pending approval: YES — "
                f"{drift_result.run_pending_approval_details or 'no description'}"
            )
        else:
            lines.append("  - Pending approval: none")
    if drift_result.run_pending_user_review is not None:
        if drift_result.run_pending_user_review:
            lines.append(
                f"  - Pending review: YES — "
                f"{drift_result.run_pending_user_review_details or 'no description'}"
            )
        else:
            lines.append("  - Pending review: none")
    return "\n".join(lines)


# Re-enabled MMP concern signals: each maps a DriftResult flag to its
# AnomalyType + the IssueType used for auto-close. fairness_risk and
# unapproved_exp_run stay disabled (not listed here) — they no longer create
# Issues. Each tuple: (flag_attr, details_attr, AnomalyType, IssueType,
# title_prefix).
_MMP_CONCERN_SIGNALS = [
    (
        "run_pending_approval",
        "run_pending_approval_details",
        AnomalyType.MMP_RUN_PENDING_APPROVAL,
        IssueType.MMP_RUN_PENDING_APPROVAL,
        "MMP run pending approval",
    ),
    (
        "run_pending_user_review",
        "run_pending_user_review_details",
        AnomalyType.MMP_PENDING_REVIEW,
        IssueType.MMP_PENDING_REVIEW,
        "MMP run pending review",
    ),
]


def build_extra_concern_events(drift_result: "DriftResult", job: "Job") -> list[AnomalyEvent]:
    """Build AnomalyEvents for the non-drift MMP concerns whose flag came back
    True. Today that's pending-approval and pending-review (fairness risk and
    unapproved-exp-run remain disabled). Returns ``[]`` when none are set.

    These signals are actioned on the MMP platform, not via a Ops runbook, so
    each event carries the MMP web-UI deep link in ``metadata["external_url"]``
    (when derivable) and embeds all three monitored statuses in its
    description. Used by both the periodic MmpChecker and product_service
    "Check Now" so the dispatch logic lives in one place.
    """
    from core.config import get_config

    events: list[AnomalyEvent] = []
    mmp_project_id = job.mmp_project_id or ""
    mmp_model_id = job.mmp_model_id or ""
    cml_model_id = drift_result.cml_model_id
    external_url = get_config().mmp_model_web_url(drift_result.mmp_project_numeric_id)
    status_block = build_mmp_status_lines(drift_result)

    for flag_attr, details_attr, anomaly_type, _issue_type, title_prefix in _MMP_CONCERN_SIGNALS:
        if not getattr(drift_result, flag_attr):
            continue
        details = getattr(drift_result, details_attr) or "(no description from MMP)"
        # Dedup per pending run, across ALL issue statuses — not just open ones.
        # A pending-approval/-review run can sit awaiting action for hours; a
        # responder may resolve/close the Ops ticket while MMP still reports the
        # run as pending. Without a stable key the next cycle finds no OPEN issue
        # and re-creates one every tick (the "infinite loop" of duplicate
        # tickets). Keying on the latest production run id means a given pending
        # run raises exactly one ticket regardless of its later status; only a
        # genuinely new run re-fires. When MMP gives us no run id we fall back to
        # None (legacy open-issue dedup) since we have nothing stable to key on.
        dedup_key = (
            f"{flag_attr}:{drift_result.latest_run_id}"
            if drift_result.latest_run_id is not None
            else None
        )
        events.append(AnomalyEvent(
            anomaly_type=anomaly_type,
            dedup_key=dedup_key,
            title=f"{title_prefix}: {mmp_project_id}/{mmp_model_id}",
            description=(
                f"{title_prefix} for model.\n"
                f"\n"
                f"MMP Project: {mmp_project_id}\n"
                f"MMP Model: {mmp_model_id}\n"
                f"CML Model ID: {cml_model_id if cml_model_id is not None else 'N/A'}\n"
                f"Ops Job ID: {job.id}\n"
                f"Product ID: {job.product_id}\n"
                f"Details: {details}\n"
                + (f"Action on MMP: {external_url}\n" if external_url else "")
                + f"\n{status_block}"
            ),
            product_id=job.product_id,
            job_id=job.id,
            metadata={
                "mmp_project_id": mmp_project_id,
                "mmp_model_id": mmp_model_id,
                "cml_model_id": cml_model_id,
                "mmp_project_numeric_id": drift_result.mmp_project_numeric_id,
                "external_url": external_url or None,
                "details": getattr(drift_result, details_attr),
                # Triggering production run id, so the issue can trace the exact
                # run in the live MMP body (drift events already carry this).
                "latest_run_id": drift_result.latest_run_id,
            },
        ))
    return events


def build_recovery_events(drift_result: "DriftResult", job: "Job") -> list["RecoveryEvent"]:
    """Build RecoveryEvents for every monitored MMP signal that reads healthy.

    Covers drift (drifted False), pending-approval (False), and pending-review
    (False). A None flag means MMP didn't report it, so no recovery is emitted
    (we can't assert recovery on a signal we never read). The caller closes any
    matching open Issue of the mapped IssueType.

    Also emits a MMP_DRIFT close when ``drift_superseded_by_pending`` — a drift
    that is still True but now folded into the same run's pending Issue. That
    closes a drift Issue raised on an earlier cycle (before the run went
    pending), so drift-then-pending converges to one Issue just like the
    simultaneous case.
    """
    recoveries: list[RecoveryEvent] = []
    if not drift_result.drifted:
        recoveries.append(RecoveryEvent(
            issue_type=IssueType.MMP_DRIFT,
            job_id=job.id,
            product_id=job.product_id,
            reason="MMP reports model_drifted back to normal",
        ))
    elif drift_result.drift_superseded_by_pending:
        # Drift is real (drifted stays True) but the same run's
        # pending-review/-approval Issue now covers it (see check_drift). Close
        # any already-open *standalone* drift Issue so a drift-then-pending run
        # converges to the single pending Issue — matching the simultaneous case
        # where the drift Issue is never created in the first place
        # (_map_drift_result). This is a fold, not a recovery: the drift didn't
        # clear, it was handed off. The pending Issue's status block still shows
        # DRIFTED, so no drift context is lost. If pending later clears without
        # approval while drift persists, a fresh drift Issue is raised again.
        recoveries.append(RecoveryEvent(
            issue_type=IssueType.MMP_DRIFT,
            job_id=job.id,
            product_id=job.product_id,
            reason=(
                f"MMP drift folded into pending-review/-approval issue for "
                f"latest run #{drift_result.latest_run_id}"
            ),
        ))
    for flag_attr, _details_attr, _anomaly_type, issue_type, title_prefix in _MMP_CONCERN_SIGNALS:
        flag = getattr(drift_result, flag_attr)
        if flag is False:  # explicit False only; None = not reported
            recoveries.append(RecoveryEvent(
                issue_type=issue_type,
                job_id=job.id,
                product_id=job.product_id,
                reason=f"MMP reports {title_prefix} cleared",
            ))

    # Approval supersedes earlier concerns. Once the latest production run is
    # approved (deployed or in an approved approval_status), any drift /
    # pending-approval / pending-review Issue raised against an *earlier* run —
    # i.e. created before this run's approval timestamp — is stale. MMP's
    # model-level ``attention_required`` block aggregates over the whole run
    # history, so a flag can still read True even though the latest run is
    # approved; the per-flag recoveries above wouldn't clear those leftovers.
    # This timestamp-scoped close does, leaving any Issue raised AFTER the
    # approval (a genuinely newer concern) untouched. Only fires when MMP gave
    # us an approval timestamp to anchor the cutoff.
    if drift_result.latest_run_approved and drift_result.latest_run_approved_at is not None:
        approved_at = drift_result.latest_run_approved_at
        for issue_type in (
            IssueType.MMP_DRIFT,
            IssueType.MMP_RUN_PENDING_APPROVAL,
            IssueType.MMP_PENDING_REVIEW,
        ):
            recoveries.append(RecoveryEvent(
                issue_type=issue_type,
                job_id=job.id,
                product_id=job.product_id,
                reason=(
                    f"MMP latest production run #{drift_result.latest_run_id} "
                    f"approved at {approved_at:%Y-%m-%d %H:%M:%S} UTC — "
                    "superseding earlier concerns"
                ),
                close_created_before=approved_at,
            ))
    return recoveries
