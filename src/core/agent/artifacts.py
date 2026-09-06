"""Chart/table artifacts for the Ops agent (plan: AGENT_INTELLIGENCE_PLAN.md §7).

Anti-hallucination design: the model never passes data rows. ``render`` takes
a **dataset name + fetch params**; the dataset registry resolves it through
the same RBAC-scoped tool implementations the chat tools use, assembles the
rows server-side, validates the spec, and pushes the full artifact into a
per-turn sink that ``run_turn`` drains into SSE ``artifact`` events. The tool
result the model sees is only a compact confirmation — row data costs zero
context tokens and cannot be fabricated.

Spec vocabulary is deliberately narrow: line (trends), bar (comparisons),
pie (composition). Tables are column/row payloads the frontend renders with
CSV export.
"""

from __future__ import annotations

import uuid
from typing import Callable, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from core.auth.jwt import CurrentUser

CHART_TYPES = ["line", "bar", "pie"]


class ChartSeries(BaseModel):
    key: str
    label: str = ""


class ChartSpec(BaseModel):
    type: Literal["line", "bar", "pie"]
    x_key: str
    series: List[ChartSeries] = Field(min_length=1)
    y_label: str = ""

    @field_validator("series")
    @classmethod
    def _pie_single_series(cls, v, info):
        if info.data.get("type") == "pie" and len(v) != 1:
            raise ValueError("pie chart must have exactly one series")
        return v


class NavButton(BaseModel):
    """Quick-jump button. ids are server-verified against the caller's scope
    before the artifact is built — the model cannot fabricate a destination."""

    target: Literal["project", "product"]
    id: int
    label: str
    # Parent project id for product buttons (the frontend's navigation
    # callback needs both to focus the product inside its project view).
    project_id: Optional[int] = None


class Artifact(BaseModel):
    id: str
    kind: Literal["chart", "table", "nav"]
    title: str
    chart: Optional[ChartSpec] = None
    columns: List[ChartSeries] = Field(default_factory=list)
    rows: List[dict] = Field(default_factory=list)
    buttons: List[NavButton] = Field(default_factory=list)
    source: dict = Field(default_factory=dict)

    @field_validator("chart")
    @classmethod
    def _chart_required_for_charts(cls, v, info):
        if info.data.get("kind") == "chart" and v is None:
            raise ValueError("chart spec required when kind == 'chart'")
        return v

    @field_validator("buttons")
    @classmethod
    def _buttons_required_for_nav(cls, v, info):
        if info.data.get("kind") == "nav" and not v:
            raise ValueError("nav artifact must carry at least one button")
        return v


def _validate_rows_against_spec(artifact: Artifact) -> None:
    """A chart whose rows don't carry the spec'd keys would render blank —
    fail fast server-side instead of shipping a broken artifact."""
    if artifact.kind != "chart" or not artifact.rows:
        return
    spec = artifact.chart
    needed = {spec.x_key} | {s.key for s in spec.series}
    sample = artifact.rows[0]
    missing = needed - set(sample.keys())
    if missing:
        raise ValueError(f"chart rows missing keys: {sorted(missing)}")


# ── dataset registry ──────────────────────────────────────────────────
# Each dataset: fetch(session, actor, params) -> dict with
#   rows / columns / default_chart ("line"|"bar"|"pie"|"table") / x_key /
#   series / title / meta. ``allowed_charts`` constrains the model override.


def _ds_product_failure_trend(session, actor: CurrentUser, p: dict) -> dict:
    from core.agent import tools

    out = tools.product_health_drilldown(
        session, actor, product_id=int(p.get("product_id") or 0),
        year=int(p.get("year") or 0), month=int(p.get("month") or 0),
        week_start=str(p.get("week_start") or ""))
    if "error" in out:
        return out
    name = out["data"]["summary"]["product_name"]
    return {
        "rows": out["data"]["daily_trend"],
        "columns": [
            {"key": "date", "label": "Date"},
            {"key": "job_runs_total", "label": "Runs"},
            {"key": "job_failures", "label": "Failures"},
            {"key": "failure_rate_percent", "label": "Failure rate %"},
        ],
        "default_chart": "line",
        "allowed_charts": ["line", "bar", "table"],
        "x_key": "date",
        "series": [{"key": "failure_rate_percent", "label": "Failure rate %"}],
        "title": f"{name} daily failure-rate trend ({out['meta']['period']['label']})",
        "meta": out["meta"],
    }


def _issue_stats(session, actor: CurrentUser, p: dict) -> dict:
    from core.agent import tools

    return tools.issue_stats(
        session, actor,
        year=int(p.get("year") or 0), month=int(p.get("month") or 0),
        week_start=str(p.get("week_start") or ""),
        project_id=int(p.get("project_id") or 0),
        product_id=int(p.get("product_id") or 0))


def _ds_issue_type_distribution(session, actor: CurrentUser, p: dict) -> dict:
    out = _issue_stats(session, actor, p)
    if "error" in out:
        return out
    rows = [{"type": e["type"], "count": e["count"]} for e in out["data"]["by_type"]]
    return {
        "rows": rows,
        "columns": [{"key": "type", "label": "Issue type"}, {"key": "count", "label": "Count"}],
        "default_chart": "pie",
        "allowed_charts": ["pie", "bar", "table"],
        "x_key": "type",
        "series": [{"key": "count", "label": "Count"}],
        "title": f"Issue type distribution ({out['meta']['period']['label']})",
        "meta": out["meta"],
    }


def _ds_issue_status_distribution(session, actor: CurrentUser, p: dict) -> dict:
    out = _issue_stats(session, actor, p)
    if "error" in out:
        return out
    d = out["data"]
    rows = [
        {"status": "open", "count": d["open_count"]},
        {"status": "in_progress", "count": d["in_progress_count"]},
        {"status": "resolved/closed", "count": d["resolved_count"] - d["false_positive_count"]},
        {"status": "false_positive", "count": d["false_positive_count"]},
    ]
    return {
        "rows": rows,
        "columns": [{"key": "status", "label": "Status"}, {"key": "count", "label": "Count"}],
        "default_chart": "pie",
        "allowed_charts": ["pie", "bar", "table"],
        "x_key": "status",
        "series": [{"key": "count", "label": "Count"}],
        "title": f"Issue status distribution ({out['meta']['period']['label']})",
        "meta": out["meta"],
    }


_BREAKDOWN_DIM_LABELS = {
    "job": "Job", "app": "App", "product": "Product", "project": "Project",
    "type": "Type", "status": "Status", "day": "Daily",
}


def _ds_issue_breakdown(session, actor: CurrentUser, p: dict) -> dict:
    """Issue counts grouped along one dimension (asset / type / status / day).
    group_by=day renders as a zero-filled daily line; everything else as a
    bar/pie composition."""
    from core.agent import tools

    group_by = str(p.get("group_by") or "type")
    out = tools.issue_breakdown(
        session, actor, group_by=group_by,
        year=int(p.get("year") or 0), month=int(p.get("month") or 0),
        week_start=str(p.get("week_start") or ""), days=int(p.get("days") or 0),
        status=str(p.get("status") or ""), issue_type=str(p.get("issue_type") or ""),
        project_id=int(p.get("project_id") or 0),
        product_id=int(p.get("product_id") or 0))
    if "error" in out:
        return out
    rows = [{"label": r["label"], "count": r["count"], "open_count": r["open_count"]}
            for r in out["data"]["rows"]]
    dim = _BREAKDOWN_DIM_LABELS.get(group_by, group_by)
    if group_by == "day":
        chart, allowed = "line", ["line", "bar", "table"]
        title = f"Daily Issue count ({out['data']['window']})"
    else:
        chart, allowed = "bar", ["bar", "pie", "table"]
        title = f"Issue by {dim} distribution ({out['data']['window']})"
    return {
        "rows": rows,
        "columns": [
            {"key": "label", "label": dim},
            {"key": "count", "label": "Issue count"},
            {"key": "open_count", "label": "Open of those"},
        ],
        "default_chart": chart,
        "allowed_charts": allowed,
        "x_key": "label",
        "series": [{"key": "count", "label": "Issue count"}],
        "title": title,
        "meta": out["meta"],
    }


def _ds_product_health_ranking(session, actor: CurrentUser, p: dict) -> dict:
    from core.agent import tools

    out = tools.product_health(
        session, actor,
        year=int(p.get("year") or 0), month=int(p.get("month") or 0),
        week_start=str(p.get("week_start") or ""),
        project_id=int(p.get("project_id") or 0))
    if "error" in out:
        return out
    rows = [{
        "product": i["product_name"],
        "failure_rate_percent": i["failure_rate_percent"],
        "anomaly_score": i["anomaly_score"],
        "open_issue_count": i["open_issue_count"],
        "severity": i["severity"],
    } for i in out["data"]["items"][:20]]
    return {
        "rows": rows,
        "columns": [
            {"key": "product", "label": "Product"},
            {"key": "failure_rate_percent", "label": "Failure rate %"},
            {"key": "anomaly_score", "label": "Anomaly score"},
            {"key": "open_issue_count", "label": "Open issues"},
            {"key": "severity", "label": "Severity"},
        ],
        "default_chart": "bar",
        "allowed_charts": ["bar", "table"],
        "x_key": "product",
        "series": [{"key": "failure_rate_percent", "label": "Failure rate %"},
                   {"key": "anomaly_score", "label": "Anomaly score"}],
        "title": f"Product health ranking ({out['meta']['period']['label']})",
        "meta": out["meta"],
    }


def _ds_job_execution_timeline(session, actor: CurrentUser, p: dict) -> dict:
    from core.agent import tools

    out = tools.job_execution_history(
        session, actor, job_id=int(p.get("job_id") or 0),
        days=int(p.get("days") or 30))
    if "error" in out:
        return out
    return {
        "rows": out["data"]["executions"],
        "columns": [
            {"key": "at", "label": "Time"},
            {"key": "status", "label": "Status"},
            {"key": "is_failure", "label": "Counted as failure"},
        ],
        "default_chart": "table",
        "allowed_charts": ["table"],
        "x_key": "at",
        "series": [{"key": "status", "label": "Status"}],
        "title": f"Job {out['data']['control_m_job_name'] or out['data']['job_id']} execution timeline",
        "meta": out["meta"],
    }


def _ds_sla_compliance_trend(session, actor: CurrentUser, p: dict) -> dict:
    """Monthly SLA compliance / MTTR over the last N months (default 6)."""
    from datetime import datetime

    months = max(2, min(12, int(p.get("months") or 6)))
    end_year = int(p.get("year") or 0) or datetime.utcnow().year
    end_month = int(p.get("month") or 0) or datetime.utcnow().month
    rows = []
    y, m = end_year, end_month
    points = []
    for _ in range(months):
        points.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    for py, pm in reversed(points):
        out = _issue_stats(session, actor, {**p, "year": py, "month": pm, "week_start": ""})
        if "error" in out:
            return out
        d = out["data"]
        rows.append({
            "month": f"{py}-{pm:02d}",
            "sla_compliance_rate": d["sla_compliance_rate"],
            "avg_resolution_minutes": d["avg_resolution_minutes"],
            "issue_total": d["total"],
        })
    return {
        "rows": rows,
        "columns": [
            {"key": "month", "label": "Month"},
            {"key": "sla_compliance_rate", "label": "SLA compliance %"},
            {"key": "avg_resolution_minutes", "label": "Avg resolution min"},
            {"key": "issue_total", "label": "Issue count"},
        ],
        "default_chart": "line",
        "allowed_charts": ["line", "bar", "table"],
        "x_key": "month",
        "series": [{"key": "sla_compliance_rate", "label": "SLA compliance %"}],
        "title": f"last {months} months SLA compliance trend",
        "meta": {"scope": "same scope as relayops_issue_stats", "truncated": False},
    }


def _ds_period_comparison(session, actor: CurrentUser, p: dict) -> dict:
    from core.agent import tools

    out = tools.compare_periods(
        session, actor,
        a_year=int(p.get("a_year") or 0), a_month=int(p.get("a_month") or 0),
        a_week_start=str(p.get("a_week_start") or ""),
        b_year=int(p.get("b_year") or 0), b_month=int(p.get("b_month") or 0),
        b_week_start=str(p.get("b_week_start") or ""),
        scope=str(p.get("scope") or "all"), target_id=int(p.get("target_id") or 0))
    if "error" in out:
        return out
    deltas = out["data"]["deltas"]
    labels = {
        "issue_total": "Total Issues", "sla_compliance_rate": "SLA compliance %",
        "avg_resolution_minutes": "Avg resolution min", "manual_interventions": "Manual interventions",
        "job_runs_total": "Runs", "job_failures": "Failures",
        "failure_rate_percent": "Failure rate %",
    }
    rows = [{"metric": labels[k], "period_a": v["a"], "period_b": v["b"], "delta": v["delta"]}
            for k, v in deltas.items()]
    return {
        "rows": rows,
        "columns": [
            {"key": "metric", "label": "Metric"},
            {"key": "period_a", "label": out["data"]["period_a"]["label"]},
            {"key": "period_b", "label": out["data"]["period_b"]["label"]},
            {"key": "delta", "label": "Delta (B-A)"},
        ],
        "default_chart": "table",
        "allowed_charts": ["table", "bar"],
        "x_key": "metric",
        "series": [{"key": "period_a", "label": out["data"]["period_a"]["label"]},
                   {"key": "period_b", "label": out["data"]["period_b"]["label"]}],
        "title": "Period comparison",
        "meta": out["meta"],
    }


DATASETS: dict[str, Callable] = {
    "product_failure_trend": _ds_product_failure_trend,
    "issue_type_distribution": _ds_issue_type_distribution,
    "issue_status_distribution": _ds_issue_status_distribution,
    "issue_breakdown": _ds_issue_breakdown,
    "product_health_ranking": _ds_product_health_ranking,
    "job_execution_timeline": _ds_job_execution_timeline,
    "sla_compliance_trend": _ds_sla_compliance_trend,
    "period_comparison": _ds_period_comparison,
}


def render_nav(
    session,
    actor: CurrentUser,
    *,
    project_ids: Optional[list] = None,
    product_ids: Optional[list] = None,
    title: str = "",
) -> tuple[Optional[dict], dict]:
    """Quick-jump button artifact. Every id is verified against the caller's
    scope and the label resolved server-side; unknown/invisible ids are
    skipped and reported back in the confirmation."""
    from core.agent.tools import _accessible_product_ids, _accessible_project_ids
    from core.models.entities import Product, Project

    project_ids = [int(i) for i in (project_ids or []) if i][:10]
    product_ids = [int(i) for i in (product_ids or []) if i][:10]
    if not project_ids and not product_ids:
        return None, {"error": "Provide at least one project_id or product_id"}

    visible_projects = _accessible_project_ids(session, actor)
    visible_products = _accessible_product_ids(session, actor)

    buttons: list[NavButton] = []
    skipped: list[str] = []
    seen: set = set()

    for pid in project_ids:
        if ("project", pid) in seen:
            continue
        seen.add(("project", pid))
        project = session.query(Project).filter(Project.id == pid).first()
        if project is None or (visible_projects is not None and pid not in visible_projects):
            skipped.append(f"project:{pid}")
            continue
        buttons.append(NavButton(target="project", id=pid, label=project.name))

    for pid in product_ids:
        if ("product", pid) in seen:
            continue
        seen.add(("product", pid))
        product = session.query(Product).filter(Product.id == pid).first()
        if product is None or (visible_products is not None and pid not in visible_products):
            skipped.append(f"product:{pid}")
            continue
        buttons.append(NavButton(target="product", id=pid, label=product.name,
                                 project_id=product.project_id))

    if not buttons:
        return None, {"error": "All ids are nonexistent or not permitted", "skipped": skipped}

    artifact = Artifact(
        id=uuid.uuid4().hex[:12],
        kind="nav",
        title=title or "Quick navigation",
        buttons=buttons,
        source={"dataset": "nav_buttons",
                "params": {"project_ids": project_ids, "product_ids": product_ids}},
    )
    confirmation = {
        "artifact_id": artifact.id,
        "kind": "nav",
        "buttons": [f"{b.target}:{b.id} {b.label}" for b in buttons],
        "skipped": skipped,
        "note": "Navigation buttons generated and shown with the answer; no need to list the links again in the body.",
    }
    return artifact.model_dump(), confirmation


def render(
    session,
    actor: CurrentUser,
    *,
    dataset: str,
    params: Optional[dict] = None,
    chart_type: str = "",
    title: str = "",
) -> tuple[Optional[dict], dict]:
    """(artifact_dict | None, model_confirmation). The confirmation is the only
    thing that enters the model context; the artifact goes to the SSE sink."""
    params = params or {}
    fetch = DATASETS.get(dataset)
    if fetch is None:
        return None, {"error": f"Unknown dataset: {dataset!r}",
                      "valid_values": sorted(DATASETS)}

    data = fetch(session, actor, params)
    if "error" in data:
        return None, data

    allowed = data.get("allowed_charts", CHART_TYPES + ["table"])
    chosen = chart_type or data["default_chart"]
    if chosen not in allowed:
        return None, {"error": f"This dataset does not support chart_type={chosen!r}",
                      "valid_values": allowed}

    kind = "table" if chosen == "table" else "chart"
    artifact = Artifact(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        title=title or data["title"],
        chart=ChartSpec(
            type=chosen, x_key=data["x_key"],
            series=[ChartSeries(**s) for s in data["series"]],
        ) if kind == "chart" else None,
        columns=[ChartSeries(**c) for c in data["columns"]],
        rows=data["rows"],
        source={"dataset": dataset, "params": params},
    )
    _validate_rows_against_spec(artifact)

    confirmation = {
        "artifact_id": artifact.id,
        "kind": artifact.kind,
        "chart_type": chosen if kind == "chart" else "table",
        "title": artifact.title,
        "row_count": len(artifact.rows),
        "meta": data.get("meta", {}),
        "note": "Chart generated and shown with the answer; reference the title in the body — don't restate every data row.",
    }
    return artifact.model_dump(), confirmation
