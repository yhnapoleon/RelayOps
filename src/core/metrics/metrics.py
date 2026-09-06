"""Prometheus metrics for RelayOps.

Two tiers, mirroring the manager's Frontier project:

* **Live runtime metrics** — real ``prometheus_client`` objects held in a
  private ``CollectorRegistry`` and updated in-process as the monitoring
  Controller runs (cycle duration, issues created, last-tick timestamp).
* **Business metrics** — counts queried from the database at scrape time and
  formatted to the Prometheus text exposition format on the fly. Formatting
  fresh on every scrape (instead of holding Gauges) means a label combination
  that disappears — e.g. an issue ``type`` that currently has no rows — leaves
  no stale time series behind.

On CML the backend runs as a single uvicorn process (see
``sdk.serve`` -> ``_serve_for_cdsw_application``, ``workers=1``), so a plain
in-process registry is correct: there is no second worker to aggregate across,
hence no need for ``PROMETHEUS_MULTIPROC_DIR`` / multiprocess mode.
"""

import time
from datetime import datetime, timedelta

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
)
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.logging import get_logger
from core.models.constants import IssueStatus
from core.models.entities import Issue, Job, JobExecution, Product, Project

logger = get_logger(__name__)

# Private registry so Ops's metrics are isolated from the process-global
# default registry (and from any third-party library that registers into it).
registry = CollectorRegistry()

# Register the standard process / platform / GC collectors INTO our private
# registry. Because we expose `generate_latest(registry)` (not the global
# default registry), these wouldn't appear otherwise. They add genuine
# process-level platform health — process_resident_memory_bytes,
# process_cpu_seconds_total, python_gc_* — alongside the business metrics.
ProcessCollector(registry=registry)
PlatformCollector(registry=registry)
GCCollector(registry=registry)

# ── Live runtime metrics (updated in-process by the Controller) ──────────────

controller_runs_total = Counter(
    "relayops_controller_runs_total",
    "Monitoring controller ticks, by result",
    ["result"],  # "success" | "error"
    registry=registry,
)
controller_cycle_seconds = Histogram(
    "relayops_controller_cycle_seconds",
    "Monitoring controller tick duration in seconds",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
    registry=registry,
)
controller_issues_created_total = Counter(
    "relayops_controller_issues_created_total",
    "Issues created by the monitoring controller",
    registry=registry,
)
controller_last_tick_timestamp = Gauge(
    "relayops_controller_last_tick_timestamp",
    "Unix timestamp of the last completed controller tick (for stall alerts)",
    registry=registry,
)

# ── HTTP request metrics (recorded per request by PrometheusMiddleware) ──────
# Labelled by the matched ROUTE TEMPLATE (e.g. /api/projects/{project_id}), not
# the raw path, so /api/projects/1, /2, /3 collapse into one series instead of
# exploding label cardinality.
http_requests_total = Counter(
    "relayops_http_requests_total",
    "HTTP requests by method, route template, and status code",
    ["method", "path", "status"],
    registry=registry,
)
http_request_duration_seconds = Histogram(
    "relayops_http_request_duration_seconds",
    "HTTP request duration in seconds by method and route template",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=registry,
)


def record_http_request(method: str, path: str, status: int, duration_seconds: float) -> None:
    """Record one HTTP request. Never raises — instrumentation must not break requests."""
    try:
        http_request_duration_seconds.labels(method=method, path=path).observe(duration_seconds)
        http_requests_total.labels(method=method, path=path, status=str(status)).inc()
    except Exception:  # pragma: no cover - defensive
        logger.opt(exception=True).debug("record_http_request failed")


def record_controller_tick(duration_seconds: float, result: str) -> None:
    """Record one controller cycle. Never raises — metrics must not break the loop."""
    try:
        controller_cycle_seconds.observe(duration_seconds)
        controller_runs_total.labels(result=result).inc()
        controller_last_tick_timestamp.set(time.time())
    except Exception:  # pragma: no cover - defensive
        logger.opt(exception=True).debug("record_controller_tick failed")


def record_issues_created(count: int) -> None:
    """Add to the issues-created counter. Never raises."""
    try:
        if count:
            controller_issues_created_total.inc(count)
    except Exception:  # pragma: no cover - defensive
        logger.opt(exception=True).debug("record_issues_created failed")


# ── Business metrics (queried from the DB and formatted per scrape) ──────────


def _escape(value) -> str:
    """Escape a Prometheus label value (backslash, double-quote, newline)."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_business_metrics(session: Session) -> str:
    """Query current entity/issue counts and render Prometheus text.

    Runs a handful of cheap ``GROUP BY``/``COUNT`` aggregations. On-demand
    (no background job) so the numbers are always fresh; the scrape interval
    (15–60s) bounds how often these run.
    """
    lines: list[str] = []

    # Issues by status + type.
    lines.append("# HELP relayops_issues Issue count by status and type")
    lines.append("# TYPE relayops_issues gauge")
    issue_rows = (
        session.query(Issue.status, Issue.type, func.count(Issue.id))
        .group_by(Issue.status, Issue.type)
        .all()
    )
    for status, issue_type, count in issue_rows:
        lines.append(
            f'relayops_issues{{status="{_escape(status)}",type="{_escape(issue_type)}"}} {count}'
        )

    # Open issues top-line (open + in_progress) for simple alerting.
    open_count = (
        session.query(func.count(Issue.id))
        .filter(Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS]))
        .scalar()
    ) or 0
    lines.append("# HELP relayops_open_issues Issues in open or in_progress status")
    lines.append("# TYPE relayops_open_issues gauge")
    lines.append(f"relayops_open_issues {open_count}")

    # Entity inventory.
    for name, model, help_text in (
        ("relayops_projects_total", Project, "Total projects"),
        ("relayops_products_total", Product, "Total products"),
        ("relayops_jobs_total", Job, "Total jobs"),
    ):
        count = session.query(func.count(model.id)).scalar() or 0
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {count}")

    # Job executions in the last 24h, by status — a live pulse of the fleet.
    since = datetime.utcnow() - timedelta(hours=24)
    exec_rows = (
        session.query(JobExecution.status, func.count(JobExecution.id))
        .filter(JobExecution.timestamp >= since)
        .group_by(JobExecution.status)
        .all()
    )
    lines.append("# HELP relayops_job_executions_24h Job executions in the last 24h by status")
    lines.append("# TYPE relayops_job_executions_24h gauge")
    for status, count in exec_rows:
        lines.append(f'relayops_job_executions_24h{{status="{_escape(status or "unknown")}"}} {count}')

    return "\n".join(lines) + "\n"


def get_metrics_content_type() -> str:
    """Prometheus exposition content type (``text/plain; version=0.0.4; ...``)."""
    return CONTENT_TYPE_LATEST
