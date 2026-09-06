"""
Staleness validation for asset health checks.

Provides validate_timestamp() which determines whether a reported "success"
status is genuinely healthy by comparing the last_run timestamp against the
current time and a per-job SLA threshold derived from the job's cron (with
a config-driven fallback for jobs whose cron is empty or unparseable).

Use case: ControlM reports status=Success but the last_run date hasn't
refreshed — this indicates a "miss" (stale/stuck job).

SLA derivation
--------------
Rather than matching the cron string against a small set of templates
(every-N-minutes, daily, weekly, ...) we compute the *next* scheduled
fire time after ``last_run`` and use that gap as the expected interval:

    threshold = (next_fire(cron, last_run) - last_run) * SAFETY_FACTOR

This makes the SLA correct for *any* 5-field cron the user puts in,
including weekday-only schedules ("0 9 * * 1-5" — the Friday->Monday
gap is correctly inferred as 3 days, not 1), multi-value minutes
("0,15,30,45 * * * *"), ranges ("0 9-17 * * *"), and step expressions
("*/15 9-17 * * 1-5"). The parser is intentionally zero-dependency
(pure standard library) so it works in CML sessions that can't pip
install.

Timezone assumption
-------------------
Cron expressions are interpreted in **local time** at a fixed UTC offset
(default UTC+8, i.e. Asia/Singapore / Asia/Hong_Kong). This matches the
business expectation — ``0 8 * * 2-5`` means "8 AM SGT Tue-Fri", not
"8 AM UTC Tue-Fri". All persisted timestamps (``last_run``, ``utcnow()``)
remain in UTC and the conversion is hidden inside ``next_fire_after`` so
nothing else in the system needs to be timezone-aware.

The offset is configurable via the ``RELAYOPS_CRON_TZ_OFFSET_MINUTES`` env var
(default 480 = 8 h). Use minutes (not hours) so half-hour timezones like
India (UTC+5:30 = 330) can be configured too. SGT and HKT don't observe
DST so a fixed offset is correct year-round for the primary deployment.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from core.integrations.normalization import _parse_datetime, NormalizationError

logger = logging.getLogger(__name__)

# When we derive the SLA threshold from a job's cron we multiply the expected
# inter-run interval by this factor so a job that genuinely runs on-time but
# a bit slow doesn't trip the miss alert. 1.5 = tolerate up to 50% lateness.
# Per-job overrides (Strict=1.0, Normal=1.5, Loose=3.0, or a custom flat
# threshold in minutes) flow in through ``validate_timestamp``'s keyword args.
_CRON_SAFETY_FACTOR = 1.5
# Hard floor — even for cron='*/1 * * * *' we won't alert before this many
# minutes of silence. Keeps noise down for sub-minute pollers.
_CRON_MIN_THRESHOLD_MINUTES = 5
# Worst-case search horizon when stepping forward to find the next fire time.
# 4 years is enough to cover even a "Feb 29 only" cron without false negatives.
_CRON_SEARCH_HORIZON_DAYS = 366 * 4
# Early-run tolerance: if last_run falls within this many minutes BEFORE the
# next scheduled fire, the run is treated as fulfilling that scheduled fire
# (anticipated execution). The next SLA window then starts from the *original*
# scheduled time, not from the early run. Example: cron '0 8 * * *', last_run
# at 06:00 — instead of demanding another run by 09:30, the system anchors on
# 08:00 today and only flags after tomorrow 08:00 + grace.
#
# Capped per-job at (natural_interval / 2) so the same constant works for
# hourly jobs (effective window shrinks to 30 min) and daily jobs (full 4 h).
_CRON_EARLY_RUN_WINDOW_MINUTES = 240


def _resolve_cron_tz_offset_minutes() -> int:
    """Return the configured UTC offset (in minutes) for cron evaluation.

    Read at call time (not module load) so tests can monkey-patch the env
    var without re-importing. Falls back to 480 (UTC+8 / SGT) on any parse
    error so a typo in the env var doesn't take the monitoring loop down.
    """
    raw = os.environ.get("RELAYOPS_CRON_TZ_OFFSET_MINUTES")
    if raw is None or raw == "":
        return 480
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "RELAYOPS_CRON_TZ_OFFSET_MINUTES=%r is not an int; falling back to 480 (UTC+8)",
            raw,
        )
        return 480

_DOW_ALIASES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}
_MONTH_ALIASES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


class CronParseError(ValueError):
    """Raised when a cron expression cannot be parsed by the generic parser."""


# --------------------------------------------------------------------------- #
# Generic 5-field cron parser (no external deps)
# --------------------------------------------------------------------------- #


def _to_int(token: str, aliases: Optional[dict], context: str) -> int:
    token = token.strip()
    if aliases and token.lower() in aliases:
        return aliases[token.lower()]
    try:
        return int(token)
    except ValueError as exc:
        raise CronParseError(f"Bad value {token!r} in {context!r}") from exc


def _parse_cron_field(
    expr: str, lo: int, hi: int, *, aliases: Optional[dict] = None,
) -> set:
    """Expand a single cron field into the set of integers it matches.

    Supports: ``*``, single value, ``A,B,C`` list, ``A-B`` range,
    ``*/N`` step, ``A-B/N`` ranged step, ``A/N`` (treated as ``A-hi/N``).
    Aliases (e.g. JAN/MON) are case-insensitive when ``aliases`` is given.
    """
    expr = expr.strip()
    if not expr:
        raise CronParseError("Empty cron field")

    out = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            raise CronParseError(f"Empty sub-expression in {expr!r}")

        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError as exc:
                raise CronParseError(f"Bad step in {part!r}") from exc
            if step <= 0:
                raise CronParseError(f"Non-positive step in {part!r}")
        else:
            base = part

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a_str, b_str = base.split("-", 1)
            start = _to_int(a_str, aliases, part)
            end = _to_int(b_str, aliases, part)
        else:
            start = _to_int(base, aliases, part)
            # "A/N" means "from A through hi, step N" — vixie-cron convention.
            end = hi if step > 1 else start

        if start > end:
            raise CronParseError(f"Range start>end in {part!r}")
        out.update(range(start, end + 1, step))

    out = {v for v in out if lo <= v <= hi}
    if not out:
        raise CronParseError(f"Field {expr!r} matches no values in [{lo},{hi}]")
    return out


def _parse_dow_field(expr: str) -> set:
    """Day-of-week parser. Accepts 0-7 (both 0 and 7 mean Sunday) plus SUN..SAT."""
    raw = _parse_cron_field(expr, 0, 7, aliases=_DOW_ALIASES)
    if 7 in raw:
        raw.discard(7)
        raw.add(0)
    return raw


def _day_matches(
    dom_match: bool, dow_match: bool,
    dom_restricted: bool, dow_restricted: bool,
) -> bool:
    """Vixie-cron day semantics.

    If BOTH day-of-month and day-of-week are restricted (i.e. neither is ``*``),
    the day fires when EITHER matches (OR). Otherwise the restricted side is
    AND'd with the unrestricted ``*`` side (which is always true).
    """
    if dom_restricted and dow_restricted:
        return dom_match or dow_match
    return dom_match and dow_match


def next_fire_after(
    cron: Optional[str],
    after: datetime,
    *,
    tz_offset_minutes: Optional[int] = None,
) -> Optional[datetime]:
    """Return the next time *strictly after* ``after`` that the cron fires.

    ``after`` is treated as **naive UTC** (matching ``datetime.utcnow()``
    and the parsed ``last_run`` timestamps used elsewhere in this module).
    The cron itself is interpreted in the local timezone defined by
    ``tz_offset_minutes`` (default: ``_resolve_cron_tz_offset_minutes()``
    = 480 = UTC+8 = SGT/HKT). The returned datetime is also naive UTC, so
    callers can subtract it from other UTC timestamps directly.

    Concrete example: cron ``0 8 * * 2-5`` with ``after = 2026-05-19
    21:15 UTC`` (Wed 05:15 SGT) returns ``2026-05-20 00:00 UTC``
    (Wed 08:00 SGT), not ``2026-05-20 08:00 UTC``.

    Returns ``None`` when the cron is empty, malformed, has the wrong number
    of fields, or no fire time exists within ``_CRON_SEARCH_HORIZON_DAYS``.
    Resolution is one minute; seconds/microseconds in ``after`` are ignored.
    """
    if not cron:
        return None
    parts = cron.strip().split()
    if len(parts) != 5:
        return None

    try:
        mins = _parse_cron_field(parts[0], 0, 59)
        hours = _parse_cron_field(parts[1], 0, 23)
        doms = _parse_cron_field(parts[2], 1, 31)
        mons = _parse_cron_field(parts[3], 1, 12, aliases=_MONTH_ALIASES)
        dows = _parse_dow_field(parts[4])
    except CronParseError as exc:
        logger.debug("Cron %r unparseable: %s", cron, exc)
        return None

    dom_restricted = parts[2].strip() != "*"
    dow_restricted = parts[4].strip() != "*"

    # Resolve the cron-local timezone once. Shift UTC -> local before the
    # stepping loop and shift local -> UTC on the way out, so the existing
    # day/hour/minute matching code stays in local-time semantics.
    offset = (
        _resolve_cron_tz_offset_minutes()
        if tz_offset_minutes is None
        else int(tz_offset_minutes)
    )
    shift = timedelta(minutes=offset)
    after_local = after + shift

    # Start at the next whole minute > after (in local time).
    t = (after_local + timedelta(minutes=1)).replace(second=0, microsecond=0)
    limit = t + timedelta(days=_CRON_SEARCH_HORIZON_DAYS)

    # Skip-ahead loop: jump month -> day -> hour -> minute instead of stepping
    # one minute at a time. Worst-case iterations for a yearly job ~ 35k.
    while t <= limit:
        if t.month not in mons:
            # Jump to the 1st of the next month at 00:00.
            year, month = t.year, t.month + 1
            if month > 12:
                year += 1
                month = 1
            t = datetime(year, month, 1, 0, 0)
            continue

        cron_dow = (t.weekday() + 1) % 7  # Python Mon=0..Sun=6 -> cron Sun=0..Sat=6
        if not _day_matches(
            t.day in doms, cron_dow in dows, dom_restricted, dow_restricted
        ):
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            continue

        if t.hour not in hours:
            future_hours = [h for h in hours if h > t.hour]
            if future_hours:
                t = t.replace(hour=min(future_hours), minute=0)
            else:
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            continue

        if t.minute not in mins:
            future_mins = [m for m in mins if m > t.minute]
            if future_mins:
                t = t.replace(minute=min(future_mins))
            else:
                t = (t + timedelta(hours=1)).replace(minute=0)
            continue

        # Found a local fire time — return the equivalent UTC instant.
        return t - shift

    logger.debug(
        "Cron %r: no fire time within %d days after %s (offset=%+dmin)",
        cron, _CRON_SEARCH_HORIZON_DAYS, after.isoformat(), offset,
    )
    return None


def estimate_cron_interval_minutes(
    cron: Optional[str], reference: Optional[datetime] = None
) -> Optional[int]:
    """Estimate the expected inter-run interval (minutes) for a 5-field cron.

    Computed as ``next_fire(cron, reference) - reference``. When ``reference``
    is None we use ``datetime.utcnow()`` so legacy callers (that don't pass a
    reference) still get a sensible answer. Returns ``None`` for empty or
    unparseable crons so callers can fall back to a static default.

    Note: the result is reference-dependent for non-uniform schedules. A
    weekday-only cron called on a Friday will return ~3 days; called on a
    Monday it returns ~1 day. That's the correct behavior — the SLA should
    track the *actual* next expected fire, not a hand-picked average.
    """
    ref = reference or datetime.utcnow()
    nxt = next_fire_after(cron, ref)
    if nxt is None:
        return None
    gap = (nxt - ref).total_seconds() / 60.0
    return int(gap) if gap >= 1 else 1


# Default tolerance (minutes) for deciding whether a failed run lines up with
# a scheduled cron fire. A run kicked off by the schedule starts at (≈) the
# fire minute, so we allow this much jitter / queue delay before concluding the
# run was triggered off-schedule (e.g. an ad-hoc run on a day the cron never
# covers). Capped per-call at natural_interval/2 so a wide tolerance can't
# bridge two adjacent fires on a frequent schedule.
_CRON_SCHEDULE_MATCH_TOLERANCE_MINUTES = 120


def _resolve_cron_schedule_tolerance_minutes() -> int:
    """Return the configured schedule-match tolerance (in minutes).

    Read at call time (not module load) so tests / ops can override via the
    ``RELAYOPS_CRON_SCHEDULE_MATCH_TOLERANCE_MINUTES`` env var. Falls back to the
    module default on any parse error.
    """
    raw = os.environ.get("RELAYOPS_CRON_SCHEDULE_MATCH_TOLERANCE_MINUTES")
    if raw is None or raw == "":
        return _CRON_SCHEDULE_MATCH_TOLERANCE_MINUTES
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "RELAYOPS_CRON_SCHEDULE_MATCH_TOLERANCE_MINUTES=%r is not an int; "
            "falling back to %d",
            raw, _CRON_SCHEDULE_MATCH_TOLERANCE_MINUTES,
        )
        return _CRON_SCHEDULE_MATCH_TOLERANCE_MINUTES


def is_within_cron_schedule(
    cron: Optional[str],
    run_time,
    *,
    tolerance_minutes: Optional[int] = None,
    tz_offset_minutes: Optional[int] = None,
) -> bool:
    """Return True if ``run_time`` lines up with a scheduled cron fire.

    A run is "in schedule" when the cron fires at some instant within
    ``tolerance_minutes`` of ``run_time`` — i.e. the run was (most likely)
    kicked off by the schedule rather than triggered ad-hoc on a day/time the
    cron never covers. Example: cron ``0 5 * * 2-5`` (Tue–Fri 05:00) returns
    True for a run at Tue 05:00 but False for a run on Monday or the weekend.

    Used to suppress JOB_FAILED *Issues* for runs that failed outside the
    schedule window — the failure is still recorded as a run snapshot, we just
    don't page anyone for an off-schedule (often manual / ad-hoc) execution.

    Fail-open: when ``cron`` is empty / unparseable or ``run_time`` is missing
    or unparseable, returns True, so an odd schedule never silently suppresses
    a real failure. ``run_time`` may be a naive-UTC ``datetime`` or a timestamp
    string (parsed like ``validate_timestamp``'s ``last_run_date``); the cron is
    evaluated in the configured local timezone.
    """
    if not cron or run_time is None:
        return True

    if isinstance(run_time, datetime):
        run_dt: Optional[datetime] = run_time
    else:
        try:
            run_dt = _parse_datetime(run_time)
        except (NormalizationError, Exception) as exc:
            logger.debug("is_within_cron_schedule: cannot parse run_time %r: %s", run_time, exc)
            return True  # fail open
    if run_dt is None:
        return True

    tolerance = (
        _resolve_cron_schedule_tolerance_minutes()
        if tolerance_minutes is None
        else int(tolerance_minutes)
    )
    if tolerance < 0:
        tolerance = 0

    # Cap the tolerance at half the natural inter-fire interval so a wide
    # tolerance can't span two adjacent scheduled fires on a frequent cron.
    nxt = next_fire_after(cron, run_dt, tz_offset_minutes=tz_offset_minutes)
    if nxt is None:
        return True  # cron unparseable / no upcoming fire -> fail open
    second = next_fire_after(cron, nxt, tz_offset_minutes=tz_offset_minutes)
    if second is not None:
        natural_interval_min = (second - nxt).total_seconds() / 60.0
        tolerance = min(float(tolerance), natural_interval_min / 2.0)

    # Is there a fire in the closed window [run - tol, run + tol]? Probe one
    # minute before the window start so a fire landing exactly on the lower
    # bound still counts (next_fire_after is strictly-after, minute-resolution).
    window_start = run_dt - timedelta(minutes=tolerance + 1)
    fire = next_fire_after(cron, window_start, tz_offset_minutes=tz_offset_minutes)
    if fire is None:
        return False
    return fire <= run_dt + timedelta(minutes=tolerance)


def _threshold_from_cron(
    cron: Optional[str],
    fallback_minutes: int,
    reference: Optional[datetime],
    *,
    safety_factor: float = _CRON_SAFETY_FACTOR,
) -> tuple:
    """Pick the staleness threshold for one job. Returns (threshold, source)."""
    if not cron or reference is None:
        return fallback_minutes, "default"
    nxt = next_fire_after(cron, reference)
    if nxt is None:
        return fallback_minutes, "default"
    gap_minutes = (nxt - reference).total_seconds() / 60.0
    if gap_minutes <= 0:
        return fallback_minutes, "default"
    derived = max(_CRON_MIN_THRESHOLD_MINUTES, int(gap_minutes * safety_factor))
    return derived, "cron"


def _resolve_effective_anchor(
    cron: Optional[str],
    last_run: datetime,
    early_window_minutes: int,
) -> tuple:
    """Decide where the SLA window should anchor for a given last_run.

    If ``last_run`` happened a small amount *before* the next scheduled cron
    fire, the run is treated as an anticipated execution that fulfils that
    scheduled slot — the SLA anchor shifts to the scheduled time so the next
    deadline is computed from there (i.e. "tomorrow 8 AM + grace", not
    "two hours from now").

    The early window is capped at ``natural_interval / 2`` so that the same
    constant works across job cadences:
      * hourly   (interval = 60 min)  → cap = 30 min
      * daily    (interval = 24 h)    → cap = 12 h
      * weekly   (interval = 7 d)     → cap = 3.5 d

    Returns ``(effective_anchor, shifted)`` where ``shifted`` is True iff
    the early-run adjustment was applied.
    """
    if not cron or early_window_minutes <= 0:
        return last_run, False

    next_fire = next_fire_after(cron, last_run)
    if next_fire is None:
        return last_run, False

    gap_to_next = (next_fire - last_run).total_seconds() / 60.0
    if gap_to_next <= 0:
        return last_run, False

    # Cap the early window at half the natural interval so we never confuse
    # consecutive scheduled fires (e.g. for an hourly job, a 4-hour window
    # would otherwise span 4 separate fires).
    second_fire = next_fire_after(cron, next_fire)
    if second_fire is not None:
        natural_interval_min = (second_fire - next_fire).total_seconds() / 60.0
        effective_window = min(early_window_minutes, natural_interval_min / 2.0)
    else:
        effective_window = float(early_window_minutes)

    if gap_to_next <= effective_window:
        return next_fire, True
    return last_run, False


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


@dataclass
class StalenessResult:
    """Result of a staleness validation check.

    Attributes:
        is_stale: True if the last_run is beyond the allowed SLA threshold.
        last_run_dt: Parsed last_run datetime (None if unparseable).
        age_minutes: How many minutes old the last_run is relative to current_time.
        threshold_minutes: The configured SLA threshold.
        reason: Human-readable explanation.
    """

    is_stale: bool
    last_run_dt: Optional[datetime]
    age_minutes: float
    threshold_minutes: int
    reason: str


def validate_timestamp(
    last_run_date: Optional[str],
    current_time: Optional[datetime] = None,
    sla_threshold_minutes: int = 120,
    cron: Optional[str] = None,
    early_run_window_minutes: Optional[int] = None,
    *,
    sla_safety_factor: Optional[float] = None,
    sla_override_minutes: Optional[int] = None,
) -> StalenessResult:
    """Validate whether a job's last_run timestamp is within the SLA threshold.

    This should be called when a checker reports status == "success" or
    "completed". If the last_run is older than the derived threshold, the
    job is considered stale (a "miss").

    Anticipated-execution handling: when ``last_run`` falls a little before
    the next scheduled cron fire (e.g. an 8 AM daily job that was triggered
    manually at 6 AM), the SLA window anchors on the *scheduled* fire time
    rather than the early run. The next deadline therefore becomes
    "tomorrow 08:00 + grace" instead of "this morning 09:30". The tolerance
    is ``early_run_window_minutes`` (default: ``_CRON_EARLY_RUN_WINDOW_MINUTES``)
    and is per-job capped at ``natural_interval / 2`` to keep hourly jobs
    safe from cross-firing misclassification.

    Per-job overrides
    -----------------
    ``sla_safety_factor`` overrides the global ``_CRON_SAFETY_FACTOR`` for
    this call (Strict=1.0, Normal=1.5, Loose=3.0 are the canonical choices).
    ``sla_override_minutes`` short-circuits the cron-derived threshold and
    uses a flat per-job value (in minutes) — useful when the user wants to
    pin a specific tolerance regardless of the job's schedule cadence.

    Timezone: cron expressions are interpreted in SGT/HKT (UTC+8) by default —
    see the module docstring and ``_resolve_cron_tz_offset_minutes``. All
    timestamps in / out of this function remain in UTC.

    Args:
        last_run_date: The last run timestamp string from the external system.
            Supports ISO 8601, Unix timestamps, and common datetime formats.
        current_time: Reference time for comparison. Defaults to utcnow().
        sla_threshold_minutes: Fallback threshold (in minutes) used when
            ``cron`` is empty or doesn't match a recognized pattern.
        cron: Optional 5-field cron expression for the job. When provided
            and parseable, the threshold is derived from the gap between
            the SLA anchor and the next scheduled fire, multiplied by the
            safety factor — so a weekday job whose last run was Friday
            doesn't get flagged at 1 day even though next fire is Monday.
        early_run_window_minutes: How many minutes before a scheduled fire
            still count as an anticipated execution for that slot.
            ``None`` uses the module default.
        sla_safety_factor: Per-job override for the cron multiplier
            (preset values: Strict 1.0, Normal 1.5, Loose 3.0). ``None``
            uses the global default.
        sla_override_minutes: Per-job custom flat threshold (minutes). When
            set, this value wins over both the cron-derived threshold and
            ``sla_threshold_minutes``.

    Returns:
        A StalenessResult indicating whether the timestamp is stale.
        ``threshold_minutes`` is measured from the SLA *anchor* (which may
        be ``last_run`` itself, or the next scheduled fire if early-run
        adjustment kicked in); ``age_minutes`` is also measured from that
        anchor, so the comparison ``age > threshold`` keeps its meaning.
    """
    now = current_time or datetime.utcnow()
    early_window = (
        _CRON_EARLY_RUN_WINDOW_MINUTES
        if early_run_window_minutes is None
        else early_run_window_minutes
    )
    safety_factor = (
        _CRON_SAFETY_FACTOR if sla_safety_factor is None else float(sla_safety_factor)
    )

    # Parse last_run first so the threshold can be anchored to it (gives
    # weekday-only / monthly crons the correct gap).
    last_run_dt: Optional[datetime] = None
    parse_error: Optional[str] = None
    if last_run_date:
        try:
            last_run_dt = _parse_datetime(last_run_date)
        except (NormalizationError, Exception) as exc:
            logger.warning("Cannot parse last_run_date '%s': %s", last_run_date, exc)
            parse_error = str(exc)

    # Decide the SLA anchor: usually last_run, but shifted to the next
    # scheduled fire when last_run looks like an anticipated execution.
    if last_run_dt is not None:
        anchor_dt, anchor_shifted = _resolve_effective_anchor(
            cron, last_run_dt, early_window
        )
    else:
        anchor_dt, anchor_shifted = None, False

    if sla_override_minutes is not None:
        threshold, threshold_source = int(sla_override_minutes), "override"
    else:
        threshold, threshold_source = _threshold_from_cron(
            cron, sla_threshold_minutes, anchor_dt, safety_factor=safety_factor,
        )
    if threshold_source == "override":
        threshold_label = f"{threshold} minutes (per-job custom override)"
    elif threshold_source == "cron":
        threshold_label = (
            f"{threshold} minutes (derived from cron {cron!r} "
            f"× safety_factor {safety_factor})"
        )
    else:
        threshold_label = f"{threshold} minutes (default — cron unparseable or absent)"

    if not last_run_date:
        return StalenessResult(
            is_stale=True,
            last_run_dt=None,
            age_minutes=0,
            threshold_minutes=threshold,
            reason="No last_run timestamp provided; cannot verify freshness",
        )

    if last_run_dt is None or anchor_dt is None:
        return StalenessResult(
            is_stale=True,
            last_run_dt=None,
            age_minutes=0,
            threshold_minutes=threshold,
            reason=(
                f"Cannot parse last_run timestamp: {last_run_date}"
                + (f" ({parse_error})" if parse_error else "")
            ),
        )

    age_minutes = (now - anchor_dt).total_seconds() / 60.0

    # When the anchor was shifted to a future scheduled fire (early run),
    # age may be negative — the scheduled time simply hasn't arrived yet.
    if age_minutes < 0:
        return StalenessResult(
            is_stale=False,
            last_run_dt=last_run_dt,
            age_minutes=age_minutes,
            threshold_minutes=threshold,
            reason=(
                f"Last run at {last_run_dt.isoformat()} is an early execution for "
                f"scheduled fire {anchor_dt.isoformat()}; next deadline is "
                f"{(anchor_dt + timedelta(minutes=threshold)).isoformat()}"
            ),
        )

    anchor_label = (
        f"effective scheduled anchor {anchor_dt.isoformat()} "
        f"(last_run {last_run_dt.isoformat()} treated as early execution)"
        if anchor_shifted
        else f"last run at {last_run_dt.isoformat()}"
    )

    if age_minutes > threshold:
        return StalenessResult(
            is_stale=True,
            last_run_dt=last_run_dt,
            age_minutes=age_minutes,
            threshold_minutes=threshold,
            reason=(
                f"{anchor_label} is {age_minutes:.0f} minutes old, "
                f"exceeding {threshold_label}"
            ),
        )

    return StalenessResult(
        is_stale=False,
        last_run_dt=last_run_dt,
        age_minutes=age_minutes,
        threshold_minutes=threshold,
        reason=(
            f"{anchor_label} is {age_minutes:.0f} minutes old, "
            f"within {threshold_label}"
        ),
    )
