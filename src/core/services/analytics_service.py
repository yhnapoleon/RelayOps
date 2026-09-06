"""Analytics computations — the single home for issue/health statistics.

Extracted verbatim from ``api/routers/analytics.py`` (plan:
AGENT_INTELLIGENCE_PLAN.md §3.0) so both the REST layer and the agent tool
layer share one implementation. Nothing here touches FastAPI or RBAC: callers
pass in already-scoped entity lists (the router scopes by role, the agent
tools by ``_accessible_product_ids``) and get plain dicts back.

False-positive convention: a failed execution whose alert was dismissed as a
false positive counts as a *success* in every metric here — that judgement
call must survive into failure rates, streaks and trends.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Optional

from core.config import get_config
from core.exceptions import ValidationError
from core.models.constants import AUTO_CLOSE_RESOLUTION
from core.models.entities import Issue, IssueStatus, JobExecution, Product, Project


# ── period resolution ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Period:
    granularity: str            # "month" | "week"
    start: datetime
    end: datetime
    year: int
    month: int
    week_start: Optional[str]   # ISO date when granularity == "week"
    label: str


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    return start, end


def resolve_period(
    *,
    year: Optional[int] = None,
    month: Optional[int] = None,
    week_start: Optional[str] = None,
) -> Period:
    """Month (default) or ISO-week period. Raises ValidationError on bad input."""
    now = datetime.utcnow()
    if week_start:
        try:
            anchor = datetime.fromisoformat(week_start)
        except ValueError as exc:
            raise ValidationError("Invalid week_start. Use YYYY-MM-DD.") from exc
        start = datetime(anchor.year, anchor.month, anchor.day) - timedelta(days=anchor.weekday())
        return Period(
            granularity="week",
            start=start,
            end=start + timedelta(days=7),
            year=start.year,
            month=start.month,
            week_start=start.date().isoformat(),
            label=f"Week of {start.strftime('%b %d, %Y')}",
        )

    target_year = year or now.year
    target_month = month or now.month
    if not 1 <= target_month <= 12:
        raise ValidationError("month must be between 1 and 12")
    start, end = month_bounds(target_year, target_month)
    return Period(
        granularity="month",
        start=start,
        end=end,
        year=target_year,
        month=target_month,
        week_start=None,
        label=start.strftime("%B %Y"),
    )


# ── failure predicate & streaks (false-positive aware) ────────────────


def is_failed_execution_status(status: Optional[str]) -> bool:
    normalized = (status or "").strip().lower()
    return (
        normalized == "failed"
        or normalized == "error"
        or normalized == "timeout"
        or "fail" in normalized
    )


def execution_is_failure(
    execution: JobExecution,
    false_positive_keys: set[tuple[int, str]],
) -> bool:
    """Whether an execution counts as a failure for analytics/charts.

    A failed run whose alert was dismissed as a false positive is treated as a
    success here (and only here) so every failure-rate / streak / trend metric
    reflects the on-duty member's judgement.
    """
    if (execution.job_id, execution.cml_run_id) in false_positive_keys:
        return False
    return is_failed_execution_status(execution.status)


def max_failed_streak(
    executions: list[JobExecution],
    false_positive_keys: set[tuple[int, str]] = frozenset(),
) -> int:
    max_streak = 0
    current_streak = 0
    for execution in executions:
        if execution_is_failure(execution, false_positive_keys):
            current_streak += 1
            if current_streak > max_streak:
                max_streak = current_streak
        else:
            current_streak = 0
    return max_streak


# ── anomaly rules & health classification ─────────────────────────────


@dataclass(frozen=True)
class AnomalyRules:
    min_runs: int = 20
    min_failure_rate_percent: int = 40
    min_repeat_failure_streak: int = 5
    min_open_issues: int = 3
    recent_failure_hours: int = 24

    def as_dict(self) -> dict:
        return asdict(self)


def get_anomaly_rules(
    *,
    min_runs: Optional[int] = None,
    min_failure_rate_percent: Optional[int] = None,
    min_repeat_failure_streak: Optional[int] = None,
    min_open_issues: Optional[int] = None,
    recent_failure_hours: Optional[int] = None,
) -> AnomalyRules:
    config = get_config()
    return AnomalyRules(
        min_runs=max(1, min_runs if min_runs is not None else config.analytics_anomaly_min_runs),
        min_failure_rate_percent=max(
            1,
            min(100, min_failure_rate_percent if min_failure_rate_percent is not None else config.analytics_anomaly_min_failure_rate_percent),
        ),
        min_repeat_failure_streak=max(
            1,
            min_repeat_failure_streak
            if min_repeat_failure_streak is not None
            else config.analytics_anomaly_min_repeat_failure_streak,
        ),
        min_open_issues=max(
            1,
            min_open_issues if min_open_issues is not None else config.analytics_anomaly_min_open_issues,
        ),
        recent_failure_hours=max(
            1,
            recent_failure_hours if recent_failure_hours is not None else config.analytics_anomaly_recent_failure_hours,
        ),
    )


def classify_product_health(
    *,
    failure_rate_percent: int,
    job_runs_total: int,
    open_issue_count: int,
    max_repeat_failure_streak: int,
    last_failed_at: Optional[datetime],
    period_end: datetime,
    rules: AnomalyRules,
) -> tuple[str, bool, int, list[str]]:
    reasons: list[str] = []
    rule_a = job_runs_total >= rules.min_runs and failure_rate_percent >= rules.min_failure_rate_percent
    rule_b = max_repeat_failure_streak >= rules.min_repeat_failure_streak
    recent_failure_window_start = period_end if period_end < datetime.utcnow() else datetime.utcnow()
    recent_failure_window_start = recent_failure_window_start.replace(microsecond=0)
    recent_failure_window_start = recent_failure_window_start.timestamp() - (rules.recent_failure_hours * 60 * 60)
    recent_failure_cutoff = datetime.utcfromtimestamp(recent_failure_window_start)
    rule_c = (
        open_issue_count >= rules.min_open_issues
        and last_failed_at is not None
        and last_failed_at >= recent_failure_cutoff
    )

    if rule_a:
        reasons.append(f"failure_rate>={rules.min_failure_rate_percent}%_with_{rules.min_runs}+_runs")
    if rule_b:
        reasons.append(f"repeat_failure_streak>={rules.min_repeat_failure_streak}")
    if rule_c:
        reasons.append(
            f"open_issues>={rules.min_open_issues}_and_recent_failure<{rules.recent_failure_hours}h"
        )

    failure_component = min(100, failure_rate_percent)
    streak_component = min(100, max_repeat_failure_streak * 20)
    issue_component = min(100, open_issue_count * 20)
    anomaly_score = int(round((failure_component * 0.5) + (streak_component * 0.25) + (issue_component * 0.25)))
    is_anomaly = len(reasons) > 0

    if len(reasons) >= 2 or anomaly_score >= 70:
        severity = "anomaly"
    elif len(reasons) == 1 or anomaly_score >= 50:
        severity = "risk"
    elif anomaly_score >= 25:
        severity = "watch"
    else:
        severity = "healthy"

    return severity, is_anomaly, anomaly_score, reasons


# ── issue statistics ──────────────────────────────────────────────────


def _build_product_and_project_maps(session, issues: list[Issue]):
    product_ids = sorted({issue.product_id for issue in issues if issue.product_id is not None})
    products = (
        session.query(Product)
        .filter(Product.id.in_(product_ids))
        .all()
        if product_ids else []
    )
    product_map = {product.id: product for product in products}
    project_ids = sorted({product.project_id for product in products})
    projects = (
        session.query(Project)
        .filter(Project.id.in_(project_ids))
        .all()
        if project_ids else []
    )
    project_map = {project.id: project for project in projects}
    return product_map, project_map


def compute_issue_stats(session, issues: list[Issue]) -> dict:
    """Compute analytics statistics and breakdowns from a list of Issue objects."""
    total = len(issues)
    by_type: dict[str, int] = {}
    resolved_times = []
    sla_compliant = 0
    sla_total = 0
    by_product: dict[int, dict] = {}
    by_project: dict[int, dict] = {}
    product_map, project_map = _build_product_and_project_maps(session, issues)

    for issue in issues:
        by_type[issue.type] = by_type.get(issue.type, 0) + 1

        if issue.resolved_at and issue.created_at:
            delta = (issue.resolved_at - issue.created_at).total_seconds() / 60.0
            resolved_times.append(delta)

        if issue.resolved_at and issue.sla_deadline:
            sla_total += 1
            if issue.resolved_at <= issue.sla_deadline:
                sla_compliant += 1

        product = product_map.get(issue.product_id or -1)
        if product is not None:
            product_entry = by_product.setdefault(product.id, {
                "id": product.id,
                "name": product.name or f"Product #{product.id}",
                "count": 0,
            })
            product_entry["count"] += 1

            project = project_map.get(product.project_id)
            if project is not None:
                project_entry = by_project.setdefault(project.id, {
                    "id": project.id,
                    "name": project.name or f"Project #{project.id}",
                    "count": 0,
                })
                project_entry["count"] += 1

    manual_interventions = sum(
        1
        for issue in issues
        if issue.status in (IssueStatus.RESOLVED, IssueStatus.CLOSED, IssueStatus.FALSE_POSITIVE)
        and issue.resolution_description
        and issue.resolution_description != AUTO_CLOSE_RESOLUTION
    )

    avg_resolution = round(sum(resolved_times) / len(resolved_times)) if resolved_times else 0
    max_resolution = round(max(resolved_times)) if resolved_times else 0
    sla_rate = round((sla_compliant / sla_total) * 100) if sla_total > 0 else 0

    return {
        "total": total,
        "by_type": [
            {"type": issue_type, "count": count}
            for issue_type, count in sorted(by_type.items(), key=lambda item: (-item[1], item[0]))
        ],
        "by_product": sorted(by_product.values(), key=lambda item: (-item["count"], item["name"].lower())),
        "by_project": sorted(by_project.values(), key=lambda item: (-item["count"], item["name"].lower())),
        "manual_interventions": manual_interventions,
        "avg_resolution_minutes": avg_resolution,
        "max_resolution_minutes": max_resolution,
        "sla_compliance_rate": sla_rate,
        "sla_compliant_count": sla_compliant,
        "sla_total_count": sla_total,
        "open_count": sum(1 for issue in issues if issue.status == IssueStatus.OPEN),
        "in_progress_count": sum(1 for issue in issues if issue.status == IssueStatus.IN_PROGRESS),
        "resolved_count": sum(
            1
            for issue in issues
            if issue.status in (IssueStatus.RESOLVED, IssueStatus.CLOSED, IssueStatus.FALSE_POSITIVE)
        ),
    }


# ── product health aggregation (plain dicts; caller scopes inputs) ────


def product_health_items(
    session,
    products: list[Product],
    *,
    period_start: datetime,
    period_end: datetime,
    rules: AnomalyRules,
    open_issues: list[Issue],
) -> list[dict]:
    """Per-product health rows (already sorted worst-first).

    ``products`` and ``open_issues`` must be pre-scoped to what the caller is
    allowed to see; this function only aggregates.
    """
    from core.models.entities import Job
    from core.services.issue_service import false_positive_execution_keys

    product_ids = [product.id for product in products]
    if not product_ids:
        return []

    jobs = (
        session.query(Job.id, Job.product_id)
        .filter(Job.product_id.in_(product_ids))
        .all()
    )
    job_ids = [job.id for job in jobs]
    product_job_ids: dict[int, list[int]] = defaultdict(list)
    for job in jobs:
        product_job_ids[job.product_id].append(job.id)

    project_ids = sorted({product.project_id for product in products})
    projects = (
        session.query(Project).filter(Project.id.in_(project_ids)).all()
        if project_ids else []
    )
    project_name_map = {project.id: project.name for project in projects}

    job_execution_rows = (
        session.query(JobExecution)
        .filter(
            JobExecution.job_id.in_(job_ids),
            JobExecution.timestamp >= period_start,
            JobExecution.timestamp < period_end,
        )
        .order_by(JobExecution.timestamp.desc(), JobExecution.id.desc())
        .all()
        if job_ids else []
    )
    executions_by_job: dict[int, list[JobExecution]] = defaultdict(list)
    for row in job_execution_rows:
        executions_by_job[row.job_id].append(row)

    false_positive_keys = false_positive_execution_keys(session, job_ids)

    open_issue_count_by_product: dict[int, int] = defaultdict(int)
    latest_issue_time_by_product: dict[int, datetime] = {}
    for issue in open_issues:
        if issue.product_id is None:
            continue
        open_issue_count_by_product[issue.product_id] += 1
        marker = issue.updated_at or issue.created_at
        if marker is None:
            continue
        current_latest = latest_issue_time_by_product.get(issue.product_id)
        if current_latest is None or marker > current_latest:
            latest_issue_time_by_product[issue.product_id] = marker

    items: list[dict] = []
    for product in products:
        total_runs = 0
        failed_runs = 0
        max_streak = 0
        last_failed_at: Optional[datetime] = None

        for job_id in product_job_ids.get(product.id, []):
            executions = executions_by_job.get(job_id, [])
            total_runs += len(executions)
            failed_for_job = 0
            for execution in executions:
                if execution_is_failure(execution, false_positive_keys):
                    failed_for_job += 1
                    if last_failed_at is None or execution.timestamp > last_failed_at:
                        last_failed_at = execution.timestamp
            failed_runs += failed_for_job
            streak = max_failed_streak(executions, false_positive_keys)
            if streak > max_streak:
                max_streak = streak

        failure_rate = round((failed_runs / total_runs) * 100) if total_runs > 0 else 0
        open_issue_count = open_issue_count_by_product.get(product.id, 0)
        severity, is_anomaly, anomaly_score, reasons = classify_product_health(
            failure_rate_percent=failure_rate,
            job_runs_total=total_runs,
            open_issue_count=open_issue_count,
            max_repeat_failure_streak=max_streak,
            last_failed_at=last_failed_at,
            period_end=period_end,
            rules=rules,
        )
        items.append({
            "product_id": product.id,
            "product_name": product.name,
            "project_id": product.project_id,
            "project_name": project_name_map.get(product.project_id),
            "job_runs_total": total_runs,
            "job_failures": failed_runs,
            "failure_rate_percent": failure_rate,
            "open_issue_count": open_issue_count,
            "max_repeat_failure_streak": max_streak,
            "anomaly_score": anomaly_score,
            "severity": severity,
            "is_anomaly": is_anomaly,
            "anomaly_reasons": reasons,
            "last_failed_at": last_failed_at,
            "last_issue_at": latest_issue_time_by_product.get(product.id),
        })

    return sorted(
        items,
        key=lambda item: (
            -item["anomaly_score"],
            -item["failure_rate_percent"],
            -item["open_issue_count"],
            (item["product_name"] or "").lower(),
        ),
    )


def product_health_drilldown_data(
    session,
    product: Product,
    *,
    period_start: datetime,
    period_end: datetime,
    rules: AnomalyRules,
    open_issues: list[Issue],
) -> dict:
    """Single-product drill-down: summary + per-job rows + daily trend.

    ``open_issues`` must be the caller-scoped open/in-progress issues of this
    product. Issue listings for the period stay with the caller (they need
    caller-specific enrichment).
    """
    from core.models.entities import Job
    from core.services.issue_service import false_positive_execution_keys

    project = session.query(Project).filter(Project.id == product.project_id).first()
    jobs = (
        session.query(Job)
        .filter(Job.product_id == product.id)
        .order_by(Job.id.asc())
        .all()
    )
    job_ids = [job.id for job in jobs]

    job_execution_rows = (
        session.query(JobExecution)
        .filter(
            JobExecution.job_id.in_(job_ids),
            JobExecution.timestamp >= period_start,
            JobExecution.timestamp < period_end,
        )
        .order_by(JobExecution.timestamp.desc(), JobExecution.id.desc())
        .all()
        if job_ids else []
    )
    false_positive_keys = false_positive_execution_keys(session, job_ids)

    executions_by_job: dict[int, list[JobExecution]] = defaultdict(list)
    daily_rollup: dict[str, dict[str, int]] = defaultdict(lambda: {"runs": 0, "fails": 0})
    for execution in job_execution_rows:
        executions_by_job[execution.job_id].append(execution)
        day = execution.timestamp.date().isoformat()
        daily_rollup[day]["runs"] += 1
        if execution_is_failure(execution, false_positive_keys):
            daily_rollup[day]["fails"] += 1

    open_issue_count_by_job: dict[int, int] = defaultdict(int)
    for issue in open_issues:
        if issue.job_id is not None:
            open_issue_count_by_job[issue.job_id] += 1

    job_items: list[dict] = []
    total_runs = 0
    failed_runs = 0
    max_streak = 0
    last_failed_at: Optional[datetime] = None
    for job in jobs:
        executions = executions_by_job.get(job.id, [])
        runs = len(executions)
        failures = 0
        for execution in executions:
            if execution_is_failure(execution, false_positive_keys):
                failures += 1
                if last_failed_at is None or execution.timestamp > last_failed_at:
                    last_failed_at = execution.timestamp
        failure_rate = round((failures / runs) * 100) if runs > 0 else 0
        streak = max_failed_streak(executions, false_positive_keys)
        if streak > max_streak:
            max_streak = streak
        total_runs += runs
        failed_runs += failures
        job_items.append({
            "job_id": job.id,
            "job_name": job.control_m_job_name or f"Job #{job.id}",
            "job_runs_total": runs,
            "job_failures": failures,
            "failure_rate_percent": failure_rate,
            "open_issue_count": open_issue_count_by_job.get(job.id, 0),
            "max_repeat_failure_streak": streak,
            "last_failed_at": next(
                (execution.timestamp for execution in executions if execution_is_failure(execution, false_positive_keys)),
                None,
            ),
        })

    sorted_job_items = sorted(
        job_items,
        key=lambda item: (
            -item["failure_rate_percent"],
            -item["open_issue_count"],
            -item["job_failures"],
            item["job_name"].lower(),
        ),
    )

    failure_rate = round((failed_runs / total_runs) * 100) if total_runs > 0 else 0
    severity, is_anomaly, anomaly_score, reasons = classify_product_health(
        failure_rate_percent=failure_rate,
        job_runs_total=total_runs,
        open_issue_count=len(open_issues),
        max_repeat_failure_streak=max_streak,
        last_failed_at=last_failed_at,
        period_end=period_end,
        rules=rules,
    )
    latest_open_issue_time = max(
        (issue.updated_at or issue.created_at for issue in open_issues if issue.updated_at or issue.created_at),
        default=None,
    )

    summary = {
        "product_id": product.id,
        "product_name": product.name,
        "project_id": product.project_id,
        "project_name": project.name if project else None,
        "job_runs_total": total_runs,
        "job_failures": failed_runs,
        "failure_rate_percent": failure_rate,
        "open_issue_count": len(open_issues),
        "max_repeat_failure_streak": max_streak,
        "anomaly_score": anomaly_score,
        "severity": severity,
        "is_anomaly": is_anomaly,
        "anomaly_reasons": reasons,
        "last_failed_at": last_failed_at,
        "last_issue_at": latest_open_issue_time,
    }

    daily_trend: list[dict] = []
    current = period_start
    while current < period_end:
        key = current.date().isoformat()
        runs = daily_rollup[key]["runs"]
        fails = daily_rollup[key]["fails"]
        rate = round((fails / runs) * 100) if runs > 0 else 0
        daily_trend.append({
            "date": key,
            "job_runs_total": runs,
            "job_failures": fails,
            "failure_rate_percent": rate,
        })
        current += timedelta(days=1)

    return {"summary": summary, "daily_trend": daily_trend, "jobs": sorted_job_items}
