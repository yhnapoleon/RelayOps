"""Deterministic SLA / cron recommendation.

Scenario: a responder says "this stale alert is a false positive — the job
running at this cadence is normal". We then suggest adjusting the job's cron /
staleness threshold. **Every number here is computed from real data; the LLM
only narrates the result, it never invents a figure.**

Inputs are all real:
  * observed cadence — median gap between adjacent *successful* JobExecution
    timestamps (recent ``lookback``);
  * current cron — Job.schedule_cron / control_m_cron;
  * safety factor — cml_checker._safety_factor_for (Strict 1.0 / Normal 1.5 /
    Loose 3.0; unset/Normal → the global 1.5);
  * cron validity — croniter (guarded import; same dep the checker already uses).

Hard "don't make it up" rules:
  * fewer than ``min_samples`` real runs → no recommended_* at all, just a warning;
  * recommended_threshold = ceil(observed × factor), floored at the triggering
    alert's age when provided (so the normal run isn't re-flagged);
  * a recommended cron is only offered when the observed cadence clearly
    disagrees with the cron's expectation AND maps to a conservative, valid cron;
    otherwise None + a "review manually" warning.

Read-only — it never writes a row. The change is committed (into the project
version flow) only after the user confirms.
"""
from __future__ import annotations

import math
from typing import List, Optional

from core.agent.write_schemas import SlaRecommendation
from core.checker.cml_checker import _safety_factor_for
from core.integrations.staleness import estimate_cron_interval_minutes

# Mirrors staleness._CRON_SAFETY_FACTOR (the default for unset / "normal").
_GLOBAL_SAFETY_FACTOR = 1.5
# Observed vs cron-expected mismatch beyond this fraction → consider a cron change.
_CRON_MISMATCH_TOLERANCE = 0.25
# Normalised success statuses written by the checker ("completed") and tests/CML.
_SUCCESS_STATUSES = ("completed", "success")

# Guarded import — croniter is already a platform dep; degrade if absent.
try:
    from croniter import croniter as _croniter  # type: ignore
except ImportError:  # pragma: no cover - exercised only on a stripped image
    _croniter = None


def _cron_is_valid(cron: Optional[str]) -> bool:
    if not cron or _croniter is None:
        return False
    try:
        return bool(_croniter.is_valid(cron))
    except Exception:
        return False


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _preset_for_factor(factor: float) -> Optional[str]:
    return {1.0: "strict", 1.5: "normal", 3.0: "loose"}.get(round(factor, 3))


def _conservative_cron(interval_minutes: float) -> Optional[str]:
    """Map an observed interval to a *conservative* standard cron, or None when
    it doesn't map cleanly (±10%). We never fabricate an arbitrary cron — an
    unclean cadence stays None so the caller asks for manual review."""
    def close(a: float, b: float) -> bool:
        return abs(a - b) <= 0.10 * b

    if close(interval_minutes, 60):
        return "0 * * * *"  # hourly
    if close(interval_minutes, 1440):
        return "0 0 * * *"  # daily at 00:00
    # every N hours (2..23)
    hours = interval_minutes / 60.0
    n = round(hours)
    if 2 <= n <= 23 and close(interval_minutes, n * 60):
        return f"0 */{n} * * *"
    return None


def recommend_sla(
    session,
    job_id: int,
    *,
    min_samples: int = 5,
    lookback: int = 20,
    this_alert_age_minutes: Optional[float] = None,
) -> SlaRecommendation:
    """Compute a deterministic SLA/cron recommendation for a job. Read-only."""
    from core.models.entities import Job, JobExecution

    job = session.get(Job, job_id)
    if job is None:
        return SlaRecommendation(job_id=job_id, warnings=["job not found"])

    cron = (job.schedule_cron or job.control_m_cron or "").strip()
    factor = _safety_factor_for(job.sla_preset)
    if factor is None:
        factor = _GLOBAL_SAFETY_FACTOR

    expected = estimate_cron_interval_minutes(cron) if cron else None
    current_threshold = int(math.ceil(expected * factor)) if expected else 0

    runs = (
        session.query(JobExecution)
        .filter(JobExecution.job_id == job_id, JobExecution.status.in_(_SUCCESS_STATUSES))
        .order_by(JobExecution.timestamp.desc())
        .limit(lookback)
        .all()
    )
    timestamps = [r.timestamp for r in runs if r.timestamp is not None]
    intervals: List[float] = []
    for newer, older in zip(timestamps, timestamps[1:]):
        gap = (newer - older).total_seconds() / 60.0
        if gap > 0:  # loop invariant: only positive adjacent-success gaps
            intervals.append(gap)

    rec = SlaRecommendation(
        job_id=job_id,
        current_cron=cron,
        current_threshold_minutes=current_threshold,
        observed_interval_minutes=_median(intervals),
        sample_run_count=len(intervals),
        croniter_valid=_cron_is_valid(cron),
        method="median(adjacent successful run gaps) × safety_factor, ceil; "
               "floored at alert age; cron validated by croniter",
    )

    if rec.sample_run_count < min_samples:
        rec.warnings.append("真实执行样本不足，不给具体阈值/cron 建议")
        return rec

    # ── threshold: cover real cadence × safety factor, never below this alert's age
    proposed = int(math.ceil(rec.observed_interval_minutes * factor))
    if this_alert_age_minutes is not None:
        proposed = max(proposed, int(math.ceil(this_alert_age_minutes)))
    if proposed != rec.current_threshold_minutes:
        rec.recommended_threshold_minutes = proposed
        rec.recommended_sla_preset = _preset_for_factor(factor)

    # ── cron: only when real cadence clearly disagrees AND maps to a safe cron
    if expected and rec.croniter_valid:
        drift = abs(expected - rec.observed_interval_minutes) / expected
        if drift > _CRON_MISMATCH_TOLERANCE:
            candidate = _conservative_cron(rec.observed_interval_minutes)
            if candidate and _cron_is_valid(candidate):
                rec.recommended_cron = candidate
            else:
                rec.warnings.append("真实节奏无法用保守 cron 安全表达，建议人工确认")

    return rec
