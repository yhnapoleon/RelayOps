"""Generic structured query engine for the Ops chat assistant.

The architectural answer to "every new question shape needs a new tool":
instead of one bespoke tool per aggregation, the model composes a validated
**query spec** — entity × filters × group-by dimensions × metrics — and this
module compiles it against the same RBAC-scoped queries the REST layer uses.

Design constraints (same security story as ``core.agent.tools``):
  * read-only; entities and dimensions are a closed whitelist, never raw SQL
    fragments from the model;
  * every query runs through the caller's product/issue scope;
  * execution failure metrics are false-positive-corrected exactly like the
    analytics dashboards (``execution_is_failure``);
  * output is envelope-wrapped so the model always sees window/scope/
    truncation.

Entities
--------
``issues``       → metrics: count, open_count
``executions``   → metrics: runs, failures, failure_rate_percent

Dimensions
----------
shared:      product, project, day, week
issues:      type, status, job, app, assignee, scenario_type
executions:  job, status

``group_by`` takes 0-2 dimensions. Zero dims = one total row. ``day`` as the
only dimension is zero-filled across the window so time series are chartable
without the model inventing dates.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from core.auth.jwt import CurrentUser
from core.models.constants import IssueStatus, IssueType
from core.models.entities import Application, Issue, Job, JobExecution, Product, Project

_MAX_ROWS = 50
_MAX_SCAN_ROWS = 20000  # hard cap on rows pulled into Python for grouping

ISSUE_DIMS = ["type", "status", "product", "project", "job", "app",
              "assignee", "scenario_type", "day", "week"]
EXECUTION_DIMS = ["job", "product", "project", "status", "day", "week"]
_EXEC_STATUS_FILTERS = ["failed", "stopped", "succeeded", "running",
                        "scheduling", "timeout"]


class QuerySpec(BaseModel):
    """Validated query request. Every field the model can set is bounded."""

    entity: str = "issues"
    group_by: List[str] = Field(default_factory=list, max_length=2)
    # filters (0 / "" / empty list = not applied)
    status: List[str] = Field(default_factory=list)
    issue_type: List[str] = Field(default_factory=list)
    project_id: int = 0
    product_id: int = 0
    job_id: int = 0
    app_id: int = 0
    assignee_id: int = 0
    has_scenario: Optional[bool] = None
    title_contains: str = ""
    # time window: rolling `days` wins; else year/month/week_start period
    days: int = 0
    year: int = 0
    month: int = 0
    week_start: str = ""
    limit: int = _MAX_ROWS


def _bad(field: str, value, valid) -> dict:
    return {"error": f"{field}: invalid value {value!r}", "valid_values": list(valid)}


def _resolve_window(spec: QuerySpec) -> Tuple[Optional[datetime], Optional[datetime], str, Optional[dict]]:
    """(start, end, label, error). Rolling days wins over calendar period."""
    from core.services import analytics_service
    from core.exceptions import ValidationError

    if spec.days > 0:
        end = datetime.utcnow()
        return end - timedelta(days=spec.days), end, f"last {spec.days} days", None
    try:
        period = analytics_service.resolve_period(
            year=spec.year or None, month=spec.month or None,
            week_start=spec.week_start or None)
    except ValidationError as exc:
        return None, None, "", {"error": str(exc)}
    return period.start, period.end, period.label, None


def run_query(session, actor: CurrentUser, spec_dict: dict) -> dict:
    """Compile and run one query spec → envelope dict. Never raises."""
    from core.agent.tools import _accessible_product_ids, _envelope, _scoped_issue_query

    try:
        spec = QuerySpec.model_validate(spec_dict or {})
    except PydanticValidationError as exc:
        return {"error": f"invalid query parameters: {exc.errors()[0].get('msg', exc)}"}

    if spec.entity not in ("issues", "executions"):
        return _bad("entity", spec.entity, ["issues", "executions"])
    dims = ISSUE_DIMS if spec.entity == "issues" else EXECUTION_DIMS
    for d in spec.group_by:
        if d not in dims:
            return _bad("group_by", d, dims)
    if "day" in spec.group_by and "week" in spec.group_by:
        return {"error": "group_by cannot contain both day and week"}
    for s in spec.status:
        valid = IssueStatus.ALL if spec.entity == "issues" else _EXEC_STATUS_FILTERS
        if s not in valid:
            return _bad("status", s, valid)
    for t in spec.issue_type:
        if t not in IssueType.ALL:
            return _bad("issue_type", t, IssueType.ALL)

    start, end, label, err = _resolve_window(spec)
    if err:
        return err

    if spec.entity == "issues":
        rows, scanned, truncated_scan = _fetch_issues(
            session, actor, spec, start, end, _scoped_issue_query)
    else:
        rows, scanned, truncated_scan = _fetch_executions(
            session, actor, spec, start, end, _accessible_product_ids)
        if isinstance(rows, dict):  # error passthrough
            return rows

    grouped = _group(session, spec, rows)
    if not grouped and not spec.group_by:
        # A zero total beats an empty list — the model must see "0", not guess.
        grouped[()] = ({"count": 0, "open_count": 0} if spec.entity == "issues"
                       else {"runs": 0, "failures": 0, "failure_rate_percent": 0})
    if "day" in spec.group_by and len(spec.group_by) == 1:
        _zero_fill_days(grouped, spec, start, end)

    out_rows = _finalize_rows(session, grouped, spec)
    limit = max(1, min(spec.limit, _MAX_ROWS))
    truncated = truncated_scan or len(out_rows) > limit

    filters = ", ".join(x for x in (
        f"status={'/'.join(spec.status)}" if spec.status else "",
        f"issue_type={'/'.join(spec.issue_type)}" if spec.issue_type else "",
        f"project={spec.project_id}" if spec.project_id else "",
        f"product={spec.product_id}" if spec.product_id else "",
        f"job={spec.job_id}" if spec.job_id else "",
        f"app={spec.app_id}" if spec.app_id else "",
        f"has_scenario={spec.has_scenario}" if spec.has_scenario is not None else "",
        f"title~{spec.title_contains}" if spec.title_contains else "",
    ) if x) or "all visible scope"
    return _envelope(
        {"entity": spec.entity, "group_by": spec.group_by, "window": label,
         "rows": out_rows[:limit], "scanned": scanned},
        scope=filters, row_count=len(out_rows), truncated=truncated,
    )


# ── fetch (RBAC applied here) ─────────────────────────────────────────


def _fetch_issues(session, actor, spec: QuerySpec, start, end, scoped_issue_query):
    q = scoped_issue_query(session, actor).filter(
        Issue.created_at >= start, Issue.created_at < end)
    if spec.status:
        q = q.filter(Issue.status.in_(spec.status))
    if spec.issue_type:
        q = q.filter(Issue.type.in_(spec.issue_type))
    if spec.product_id:
        q = q.filter(Issue.product_id == spec.product_id)
    if spec.project_id:
        pids = [r.id for r in session.query(Product.id)
                .filter(Product.project_id == spec.project_id).all()]
        q = q.filter(Issue.product_id.in_(pids or [-1]))
    if spec.job_id:
        q = q.filter(Issue.job_id == spec.job_id)
    if spec.app_id:
        q = q.filter(Issue.app_id == spec.app_id)
    if spec.assignee_id:
        q = q.filter(Issue.assignee_id == spec.assignee_id)
    if spec.has_scenario is True:
        q = q.filter(Issue.selected_scenario_type.isnot(None),
                     Issue.selected_scenario_type != "")
    elif spec.has_scenario is False:
        q = q.filter((Issue.selected_scenario_type.is_(None))
                     | (Issue.selected_scenario_type == ""))
    if spec.title_contains.strip():
        q = q.filter(Issue.title.ilike(f"%{spec.title_contains.strip()}%"))
    rows = q.limit(_MAX_SCAN_ROWS + 1).all()
    truncated = len(rows) > _MAX_SCAN_ROWS
    return rows[:_MAX_SCAN_ROWS], len(rows[:_MAX_SCAN_ROWS]), truncated


def _fetch_executions(session, actor, spec: QuerySpec, start, end, accessible_product_ids):
    product_ids = accessible_product_ids(session, actor)
    q = (session.query(JobExecution, Job)
         .join(Job, JobExecution.job_id == Job.id)
         .filter(JobExecution.timestamp >= start, JobExecution.timestamp < end))
    if product_ids is not None:
        q = q.filter(Job.product_id.in_(product_ids or [-1]))
    if spec.job_id:
        q = q.filter(JobExecution.job_id == spec.job_id)
    if spec.product_id:
        if product_ids is not None and spec.product_id not in product_ids:
            return {"error": "product not found or access denied"}, 0, False
        q = q.filter(Job.product_id == spec.product_id)
    if spec.project_id:
        pids = [r.id for r in session.query(Product.id)
                .filter(Product.project_id == spec.project_id).all()]
        q = q.filter(Job.product_id.in_(pids or [-1]))
    if spec.status:
        # CML 状态过滤用 Ops 归一化口径：failed 同时匹配 timeout 类失败。
        from core.services.analytics_service import is_failed_execution_status

        pairs = q.limit(_MAX_SCAN_ROWS + 1).all()
        want = set(spec.status)

        def _match(e):
            s = (e.status or "").lower()
            if "failed" in want and is_failed_execution_status(e.status):
                return True
            return any(w in s for w in want)

        pairs = [(e, j) for e, j in pairs if _match(e)]
    else:
        pairs = q.limit(_MAX_SCAN_ROWS + 1).all()
    truncated = len(pairs) > _MAX_SCAN_ROWS
    return pairs[:_MAX_SCAN_ROWS], len(pairs[:_MAX_SCAN_ROWS]), truncated


# ── grouping ──────────────────────────────────────────────────────────


def _issue_dim_key(session, spec, i: Issue, dim: str):
    if dim == "type":
        return i.type
    if dim == "status":
        return i.status
    if dim == "product":
        return ("product", i.product_id or 0)
    if dim == "project":
        return ("project", _project_of(session, i.product_id))
    if dim == "job":
        return ("job", i.job_id or 0)
    if dim == "app":
        return ("app", i.app_id or 0)
    if dim == "assignee":
        return ("assignee", i.assignee_id or 0)
    if dim == "scenario_type":
        return i.selected_scenario_type or "(no scenario selected)"
    if dim == "day":
        return i.created_at.date().isoformat() if i.created_at else "(no timestamp)"
    if dim == "week":
        return _week_label(i.created_at)
    return ""


_project_cache_attr = "_qe_product_project"


def _project_of(session, product_id) -> int:
    if not product_id:
        return 0
    cache = getattr(session, _project_cache_attr, None)
    if cache is None:
        cache = {p.id: p.project_id for p in session.query(Product).all()}
        setattr(session, _project_cache_attr, cache)
    return cache.get(product_id) or 0


def _week_label(dt) -> str:
    if not dt:
        return "(no timestamp)"
    monday = dt.date() - timedelta(days=dt.weekday())
    return f"week of {monday.isoformat()}"


def _group(session, spec: QuerySpec, rows) -> dict:
    grouped: dict = {}
    if spec.entity == "issues":
        for i in rows:
            key = tuple(_issue_dim_key(session, spec, i, d) for d in spec.group_by)
            g = grouped.setdefault(key, {"count": 0, "open_count": 0})
            g["count"] += 1
            if i.status in (IssueStatus.OPEN, IssueStatus.IN_PROGRESS):
                g["open_count"] += 1
        return grouped

    # executions: rows are (JobExecution, Job) pairs; FP-corrected failures.
    from core.services.analytics_service import execution_is_failure
    from core.services.issue_service import false_positive_execution_keys

    job_ids = list({j.id for _, j in rows})
    fp_keys = false_positive_execution_keys(session, job_ids) if job_ids else set()
    for e, j in rows:
        key = tuple(_exec_dim_key(session, e, j, d) for d in spec.group_by)
        g = grouped.setdefault(key, {"runs": 0, "failures": 0})
        g["runs"] += 1
        if execution_is_failure(e, fp_keys):
            g["failures"] += 1
    for g in grouped.values():
        g["failure_rate_percent"] = round(g["failures"] / g["runs"] * 100) if g["runs"] else 0
    return grouped


def _exec_dim_key(session, e: JobExecution, j: Job, dim: str):
    if dim == "job":
        return ("job", j.id)
    if dim == "product":
        return ("product", j.product_id or 0)
    if dim == "project":
        return ("project", _project_of(session, j.product_id))
    if dim == "status":
        return e.status or "(no status)"
    if dim == "day":
        return e.timestamp.date().isoformat() if e.timestamp else "(no timestamp)"
    if dim == "week":
        return _week_label(e.timestamp)
    return ""


def _zero_fill_days(grouped: dict, spec: QuerySpec, start, end) -> None:
    zero = {"count": 0, "open_count": 0} if spec.entity == "issues" else \
        {"runs": 0, "failures": 0, "failure_rate_percent": 0}
    cursor = start.date()
    last = min(end, datetime.utcnow()).date()
    while cursor <= last:
        grouped.setdefault((cursor.isoformat(),), dict(zero))
        cursor += timedelta(days=1)


# ── label resolution & output shaping ─────────────────────────────────

_ENTITY_LABELS = {
    "job": (Job, lambda j: j.cml_job_name or j.control_m_job_name or f"job:{j.id}"),
    "app": (Application, lambda a: a.cml_application_name or f"app:{a.id}"),
    "product": (Product, lambda p: p.name),
    "project": (Project, lambda p: p.name),
}


def _finalize_rows(session, grouped: dict, spec: QuerySpec) -> list:
    # Collect ids per ref-dim for one lookup pass each.
    ref_ids: dict = {}
    for key in grouped:
        for part in key:
            if isinstance(part, tuple):
                ref_ids.setdefault(part[0], set()).add(part[1])

    labels: dict = {}
    for kind, ids in ref_ids.items():
        model, to_label = _ENTITY_LABELS.get(kind, (None, None))
        if model is None:
            continue
        found = session.query(model).filter(model.id.in_([i for i in ids if i] or [-1])).all()
        labels[kind] = {x.id: to_label(x) for x in found}

    def _part_label(part):
        if not isinstance(part, tuple):
            return part, None, None
        kind, ident = part
        if kind == "assignee":
            return (f"user:{ident}" if ident else "(unassigned)"), kind, ident
        name = labels.get(kind, {}).get(ident)
        if name is None:
            name = f"{kind}:{ident}" if ident else f"(no linked {kind})"
        return name, kind, ident

    rows = []
    for key, metrics in grouped.items():
        row: dict = {}
        for dim, part in zip(spec.group_by, key):
            label, kind, ident = _part_label(part)
            row[dim] = label
            if kind in ("job", "app", "product", "project") and ident:
                row[f"{dim}_id"] = ident
        row.update(metrics)
        rows.append(row)

    time_dim = next((d for d in spec.group_by if d in ("day", "week")), None)
    if time_dim and len(spec.group_by) == 1:
        rows.sort(key=lambda r: str(r[time_dim]))
    else:
        sort_metric = "count" if spec.entity == "issues" else "runs"
        rows.sort(key=lambda r: -r.get(sort_metric, 0))
    return rows
