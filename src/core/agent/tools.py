"""Read-only tools for the Ops chat assistant.

Layering rules (the whole security story of the chatbot lives here):
  * every tool runs with the calling user's identity (``actor``) and applies
    the same product/project scoping as the REST routers — the assistant can
    never see data the user's own UI wouldn't show;
  * tools are READ-ONLY. Write capabilities, when they come, will be separate
    draft_* tools behind explicit human confirmation;
  * implementations are plain functions (session, actor, params) → dict so
    they unit-test without langchain; :func:`build_langchain_tools` wraps
    them into tool objects at graph-build time (guarded import).

Result dicts are deliberately compact — they go straight into the model
context, so every field costs tokens.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from core.auth.jwt import CurrentUser
from core.config import get_config
from core.exceptions import ValidationError
from core.models.constants import IssueStatus, IssueType
from core.models.database import get_db
from core.models.entities import (
    Application,
    ApplicationRecoveryScenario,
    Issue,
    Job,
    JobExecution,
    JobFailureScenario,
    Product,
    Project,
    ProjectMember,
)
from core.models.mmp_entities import MmpDriftSnapshot
from core.models.user import UserRole, is_elevated_role
from core.agent.knowledge import (
    ISSUE_SCENARIO_HINTS,
    NO_RUNBOOK_HANDLING,
    build_domain_card,
    classify_handling_state,
)
from core.services import analytics_service
from core.services.support_group_service import user_has_project_group_access

_MAX_ROWS = 50  # context budget guard for any listing tool
# Per-turn backstop against a runaway "keep tweaking params" loop — NOT a
# discipline knob. Set well above legitimate fan-out (drilling into ~a dozen
# jobs/products is normal); over-query discipline lives in the prompt + dedup.
# The 2nd-round regression set this to 6 and broke a legitimate "check every
# job" task — never go that low again.
_PER_TOOL_CALL_LIMIT = 25


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _envelope(data, *, period=None, scope: str = "", row_count: Optional[int] = None,
              truncated: bool = False) -> dict:
    """Standard result wrapper for the analytics-grade tools: the model must
    always see what window/scope the numbers cover and whether rows were cut."""
    meta: dict = {"scope": scope, "truncated": truncated}
    if period is not None:
        meta["period"] = {
            "granularity": period.granularity, "label": period.label,
            "start": _iso(period.start), "end": _iso(period.end),
        }
    if row_count is not None:
        meta["row_count"] = row_count
    return {"data": data, "meta": meta}


def _bad_enum(field: str, value: str, valid: list) -> dict:
    return {"error": f"{field}: invalid value {value!r}", "valid_values": valid}


def _resolve_period_or_error(year: int, month: int, week_start: str):
    """(Period, None) on success, (None, error-dict) on bad input."""
    try:
        return analytics_service.resolve_period(
            year=year or None, month=month or None, week_start=week_start or None,
        ), None
    except ValidationError as exc:
        return None, {"error": str(exc)}


def _is_admin(actor: CurrentUser) -> bool:
    # Elevated tier (admin / relayops_member) OR platform owner → unrestricted read scope.
    return is_elevated_role(actor.role) or actor.username in get_config().platform_owners


def _accessible_project_ids(session, actor: CurrentUser) -> Optional[List[int]]:
    """None = unrestricted (admin); otherwise the project ids this user owns,
    is a member of, or reaches via AD-group access — same rule as the REST
    layer's access helpers."""
    if _is_admin(actor):
        return None
    owned = [r.id for r in session.query(Project.id).filter(Project.owner_id == actor.user_id).all()]
    member = [
        r.project_id
        for r in session.query(ProjectMember.project_id).filter(ProjectMember.user_id == actor.user_id).all()
    ]
    grouped = [
        p.id for p in session.query(Project).all()
        if user_has_project_group_access(session, p, actor.ad_groups)
    ]
    return list(set(owned + member + grouped))


def _accessible_product_ids(session, actor: CurrentUser) -> Optional[List[int]]:
    project_ids = _accessible_project_ids(session, actor)
    if project_ids is None:
        return None
    if not project_ids:
        return []
    return [r.id for r in session.query(Product.id).filter(Product.project_id.in_(project_ids)).all()]


# ── tool implementations (plain, testable) ───────────────────────────


def projects_overview(session, actor: CurrentUser) -> list:
    project_ids = _accessible_project_ids(session, actor)
    q = session.query(Project)
    if project_ids is not None:
        q = q.filter(Project.id.in_(project_ids or [-1]))
    out = []
    for p in q.order_by(Project.id).limit(_MAX_ROWS).all():
        products = session.query(Product).filter(Product.project_id == p.id).all()
        out.append({
            "project_id": p.id,
            "name": p.name,
            "cml_project_name": p.cml_project_name or "",
            "products": [{"product_id": x.id, "name": x.name} for x in products],
        })
    return out


def product_assets(session, actor: CurrentUser, product_id: int) -> dict:
    product_ids = _accessible_product_ids(session, actor)
    if product_ids is not None and product_id not in product_ids:
        return {"error": "product not found or access denied"}
    product = session.query(Product).filter(Product.id == product_id).first()
    if product is None:
        return {"error": "product not found or access denied"}
    jobs = session.query(Job).filter(Job.product_id == product_id).limit(_MAX_ROWS).all()
    apps = session.query(Application).filter(Application.product_id == product_id).limit(_MAX_ROWS).all()

    # Per-asset issue/run stats so "这个产品下每个 job/app 怎么样" is one call,
    # not one relayops_list_issues round-trip per asset.
    since_30d = datetime.utcnow() - timedelta(days=30)
    issue_rows = (
        _scoped_issue_query(session, actor)
        .filter(Issue.product_id == product_id)
        .with_entities(Issue.job_id, Issue.app_id, Issue.status, Issue.created_at)
        .all()
    )
    job_stats: dict = {}
    app_stats: dict = {}
    for job_id, app_id, st, created_at in issue_rows:
        if job_id:
            bucket = job_stats.setdefault(job_id, {"open_issues": 0, "issues_30d": 0})
        elif app_id:
            bucket = app_stats.setdefault(app_id, {"open_issues": 0, "issues_30d": 0})
        else:
            continue
        if st in (IssueStatus.OPEN, IssueStatus.IN_PROGRESS):
            bucket["open_issues"] += 1
        if created_at and created_at >= since_30d:
            bucket["issues_30d"] += 1

    last_runs: dict = {}
    if jobs:
        from core.services.issue_service import false_positive_execution_keys

        fp_keys = false_positive_execution_keys(session, [j.id for j in jobs])
        executions = (
            session.query(JobExecution)
            .filter(JobExecution.job_id.in_([j.id for j in jobs]),
                    JobExecution.timestamp >= since_30d)
            .order_by(JobExecution.timestamp.desc())
            .all()
        )
        for e in executions:
            run = last_runs.setdefault(e.job_id, {"runs_30d": 0, "failures_30d": 0,
                                                  "last_run_at": _iso(e.timestamp),
                                                  "last_run_status": e.status})
            run["runs_30d"] += 1
            if analytics_service.execution_is_failure(e, fp_keys):
                run["failures_30d"] += 1

    return {
        "product_id": product.id,
        "name": product.name,
        "jobs": [{
            "job_id": j.id,
            "cml_job_name": j.cml_job_name or "",
            "control_m_job_name": j.control_m_job_name or "",
            "schedule_cron": j.schedule_cron or "",
            "mmp_model_id": j.mmp_model_id or "",
            "owner_contact": j.owner_contact or "",
            "cml_bound": bool(j.cml_job_id),
            "binding_error": j.cml_binding_error or "",
            "stats": {**{"open_issues": 0, "issues_30d": 0},
                      **job_stats.get(j.id, {}),
                      **{"runs_30d": 0, "failures_30d": 0,
                         "last_run_at": None, "last_run_status": ""},
                      **last_runs.get(j.id, {})},
        } for j in jobs],
        "apps": [{
            "app_id": a.id,
            "cml_application_name": a.cml_application_name or "",
            "application_url": a.application_url or "",
            "owner_contact": a.owner_contact or "",
            "cml_bound": bool(a.cml_application_id),
            "binding_error": a.cml_binding_error or "",
            "stats": {**{"open_issues": 0, "issues_30d": 0}, **app_stats.get(a.id, {})},
        } for a in apps],
    }


def _scoped_issue_query(session, actor: CurrentUser):
    """Issue visibility rule shared by every issue-reading tool: product scope
    ∪ assigned-to-me ∪ created-by-me (admins unrestricted)."""
    product_ids = _accessible_product_ids(session, actor)
    q = session.query(Issue)
    if product_ids is not None:
        q = q.filter(
            (Issue.product_id.in_(product_ids or [-1]))
            | (Issue.assignee_id == actor.user_id)
            | (Issue.created_by == actor.user_id)
        )
    return q


def list_issues(
    session,
    actor: CurrentUser,
    status: str = "",
    issue_type: str = "",
    days: int = 7,
    sla_risk_only: bool = False,
    limit: int = 20,
    product_id: int = 0,
    job_id: int = 0,
    app_id: int = 0,
) -> list:
    if status and status not in IssueStatus.ALL:
        return [_bad_enum("status", status, IssueStatus.ALL)]
    if issue_type and issue_type not in IssueType.ALL:
        return [_bad_enum("issue_type", issue_type, IssueType.ALL)]
    q = _scoped_issue_query(session, actor)
    if status:
        q = q.filter(Issue.status == status)
    if issue_type:
        q = q.filter(Issue.type == issue_type)
    if product_id:
        q = q.filter(Issue.product_id == product_id)
    if job_id:
        q = q.filter(Issue.job_id == job_id)
    if app_id:
        q = q.filter(Issue.app_id == app_id)
    if days > 0:
        q = q.filter(Issue.created_at >= datetime.utcnow() - timedelta(days=days))
    if sla_risk_only:
        q = q.filter(Issue.status.in_(("open", "in_progress"))).filter(Issue.sla_deadline.isnot(None))
        q = q.order_by(Issue.sla_deadline.asc())
    else:
        q = q.order_by(Issue.created_at.desc())
    out = []
    for i in q.limit(min(limit, _MAX_ROWS)).all():
        handling = classify_handling_state(i)
        out.append({
            "issue_id": i.id,
            "type": i.type,
            "status": i.status,
            "handling_state": handling["state"],
            "handling_label": handling["label"],
            "sla_overdue": handling["sla_overdue"],
            "sla_at_risk": handling["sla_at_risk"],
            "title": i.title,
            "product_id": i.product_id,
            "job_id": i.job_id,
            "app_id": i.app_id,
            "assignee_id": i.assignee_id,
            "selected_scenario_type": i.selected_scenario_type or "",
            "selected_scenario_name": i.selected_scenario_name or "",
            "sla_deadline": _iso(i.sla_deadline),
            "created_at": _iso(i.created_at),
            "resolved_at": _iso(i.resolved_at),
        })
    return out


def on_duty_now(session, actor: CurrentUser) -> list:
    # On-duty roster is org-public inside the team — no scoping needed.
    from core.issue_management.issue_engine import get_current_on_duty_members

    members = get_current_on_duty_members(get_db())
    return [{"user_id": m.id, "username": m.username, "display_name": m.display_name or ""} for m in members]


def job_runbook(session, actor: CurrentUser, job_id: int) -> dict:
    product_ids = _accessible_product_ids(session, actor)
    job = session.query(Job).filter(Job.id == job_id).first()
    if job is None or (product_ids is not None and job.product_id not in product_ids):
        return {"error": "job not found or access denied"}
    scenarios = session.query(JobFailureScenario).filter(JobFailureScenario.job_id == job_id).all()
    return {
        "job_id": job.id,
        "cml_job_name": job.cml_job_name or "",
        "control_m_job_name": job.control_m_job_name or "",
        "owner_contact": job.owner_contact or "",
        "description": job.description or "",
        "scenarios": [_scenario_dict(s, with_diagnostic=True) for s in scenarios],
    }


def app_runbook(session, actor: CurrentUser, app_id: int) -> dict:
    product_ids = _accessible_product_ids(session, actor)
    app = session.query(Application).filter(Application.id == app_id).first()
    if app is None or (product_ids is not None and app.product_id not in product_ids):
        return {"error": "app not found or access denied"}
    scenarios = session.query(ApplicationRecoveryScenario).filter(
        ApplicationRecoveryScenario.application_id == app_id
    ).all()
    return {
        "app_id": app.id,
        "cml_application_name": app.cml_application_name or "",
        "application_url": app.application_url or "",
        "owner_contact": app.owner_contact or "",
        "scenarios": [_scenario_dict(s, with_diagnostic=False) for s in scenarios],
    }


def _scenario_dict(s, *, with_diagnostic: bool) -> dict:
    def _steps(v):
        return [str(x) for x in v] if isinstance(v, list) else []

    out = {
        "scenario_type": s.scenario_type,
        "scenario_name": s.scenario_name or "",
        "condition": s.condition_description or "",
        "action_steps": _steps(s.action_steps),
        "verification_steps": _steps(s.verification_steps),
        "escalation_target": s.escalation_target or "",
    }
    if with_diagnostic:
        out["diagnostic_steps"] = _steps(s.diagnostic_steps)
    # Coverage flags so the model can never mistake an empty runbook field for
    # "fill it in from general knowledge": a false flag is an explicit instruction
    # to say "runbook 未记录", not to improvise (see CHAT_SYSTEM 事实核查纪律).
    out["runbook_coverage"] = {
        "has_action_steps": bool(out["action_steps"]),
        "has_verification_steps": bool(out["verification_steps"]),
        "has_escalation_target": bool(out["escalation_target"]),
        **({"has_diagnostic_steps": bool(out.get("diagnostic_steps"))} if with_diagnostic else {}),
    }
    return out


# ── domain glossary ───────────────────────────────────────────────────


def domain_glossary(session, actor: CurrentUser) -> dict:
    return {"glossary": build_domain_card()}


# ── issue detail (full record + derived handling state + timeline) ────


def issue_detail(session, actor: CurrentUser, issue_id: int) -> dict:
    issue = _scoped_issue_query(session, actor).filter(Issue.id == issue_id).first()
    if issue is None:
        return {"error": "issue not found or access denied"}

    handling = classify_handling_state(issue)
    summary = issue.action_summary_json if isinstance(issue.action_summary_json, dict) else {}

    timeline: list[dict] = []
    if summary.get("started_at"):
        timeline.append({"event": "start_working", "at": summary["started_at"]})
    for kind, key in (("record_step", "steps"), ("verification_passed", "verifications"),
                      ("escalate", "escalations")):
        for entry in summary.get(key) or []:
            if isinstance(entry, dict):
                timeline.append({
                    "event": kind,
                    "at": entry.get("recorded_at"),
                    "title": entry.get("title", ""),
                    "notes": (entry.get("notes") or "")[:300],
                    "target": entry.get("target", ""),
                })
    if summary.get("returned_to_owner_at"):
        timeline.append({"event": "return_to_owner", "at": summary["returned_to_owner_at"],
                         "notes": (summary.get("returned_to_owner_notes") or "")[:300]})
    timeline.sort(key=lambda e: e.get("at") or "")

    related: dict = {}
    job = None
    if issue.job_id:
        job = session.query(Job).filter(Job.id == issue.job_id).first()
        if job:
            related["job"] = {"job_id": job.id, "cml_job_name": job.cml_job_name or "",
                              "control_m_job_name": job.control_m_job_name or "",
                              "mmp_project_id": job.mmp_project_id or "",
                              "mmp_model_id": job.mmp_model_id or ""}
    if issue.app_id:
        app = session.query(Application).filter(Application.id == issue.app_id).first()
        if app:
            related["app"] = {"app_id": app.id,
                              "cml_application_name": app.cml_application_name or ""}
    if issue.product_id:
        product = session.query(Product).filter(Product.id == issue.product_id).first()
        if product:
            related["product"] = {"product_id": product.id, "name": product.name}

    result = {
        "issue_id": issue.id,
        "type": issue.type,
        "status": issue.status,
        "handling_state": handling["state"],
        "handling_label": handling["label"],
        "handling_detail": handling["detail"],
        "sla_overdue": handling["sla_overdue"],
        "sla_at_risk": handling["sla_at_risk"],
        "title": issue.title,
        "description": (issue.description or "")[:2000],
        "assignee_id": issue.assignee_id,
        "created_at": _iso(issue.created_at),
        "sla_deadline": _iso(issue.sla_deadline),
        "resolved_at": _iso(issue.resolved_at),
        "selected_scenario_type": issue.selected_scenario_type or "",
        "selected_scenario_name": issue.selected_scenario_name or "",
        "resolution_description": (issue.resolution_description or "")[:1500],
        "external_url": issue.external_url or "",
        "action_timeline": timeline,
        "related": related,
    }

    # MMP issues: approval_status / run drift / attention_required are NOT stored
    # on the Ops issue record — they live in MMP. Hand the agent the exact
    # binding + triggering run id and tell it to read them live, so it never
    # answers "not recorded / null" from the Ops snapshot for an MMP field.
    if (issue.type or "").startswith("mmp"):
        result["mmp"] = {
            "repo_name": (job.mmp_project_id or "") if job else "",
            "model_name": (job.mmp_model_id or "") if job else "",
            "run_id": getattr(issue, "mmp_run_id", None),
            "note": (
                "MMP fields (approval_status, run drift, attention_required) are "
                "NOT on the Ops issue — read them live via "
                "mmp_live_model_raw(repo_name, model_name, run_id=run_id). Do NOT "
                "report them as 'not recorded'/null without checking MMP first."
            ),
        }

    return result


# ── issue resolution playbook (deterministic scenario matching) ──────


def issue_resolution(session, actor: CurrentUser, issue_id: int) -> dict:
    """Grounded "how do I resolve this issue" answer. The issue-type → runbook
    scenario match is computed in CODE (ISSUE_SCENARIO_HINTS), so the model
    never guesses which scenario applies — it only narrates what we return.

    Shape: handling_mode tells the model the regime; matched_scenarios are the
    only runbook scenarios it may present as applicable; allowed_contacts is the
    closed set of escalation targets it may name (anything else = fabrication).
    """
    issue = _scoped_issue_query(session, actor).filter(Issue.id == issue_id).first()
    if issue is None:
        return {"error": "issue not found or access denied"}

    handling = classify_handling_state(issue)
    hints = ISSUE_SCENARIO_HINTS.get(issue.type, set())

    # Pull the bound asset's runbook (job xor app), flag each scenario matched/not.
    scenarios: list[dict] = []
    owner_contact = ""
    if issue.job_id:
        job = session.query(Job).filter(Job.id == issue.job_id).first()
        if job is not None:
            owner_contact = job.owner_contact or ""
            rows = session.query(JobFailureScenario).filter(
                JobFailureScenario.job_id == job.id).all()
            scenarios = [_scenario_dict(s, with_diagnostic=True) for s in rows]
    elif issue.app_id:
        app = session.query(Application).filter(Application.id == issue.app_id).first()
        if app is not None:
            owner_contact = app.owner_contact or ""
            rows = session.query(ApplicationRecoveryScenario).filter(
                ApplicationRecoveryScenario.application_id == app.id).all()
            scenarios = [_scenario_dict(s, with_diagnostic=False) for s in rows]

    for s in scenarios:
        s["matched"] = s["scenario_type"] in hints
    matched = [s for s in scenarios if s["matched"]]

    # Decide the regime the model must answer in.
    if issue.type in NO_RUNBOOK_HANDLING:
        handling_mode = "platform_workflow"      # no runbook by design
        handling_note = NO_RUNBOOK_HANDLING[issue.type]
    elif matched:
        handling_mode = "runbook_scenario"        # follow the matched scenario(s)
        handling_note = ("Follow the recorded steps in matched_scenarios below; an empty step means "
                         "'not recorded' — do not invent or fill it in.")
    elif scenarios:
        handling_mode = "runbook_no_match"        # asset has a runbook but none fits this type
        handling_note = ("The asset has a runbook, but no scenario matches this issue type ("
                         + str(issue.type)
                         + "). State plainly that there is no matching scenario and handle it with "
                         "generic triage — do not force-fit an unrelated scenario.")
    else:
        handling_mode = "no_runbook"              # nothing on file at all
        handling_note = ("The asset has no runbook scenarios on record. Handle it with generic triage "
                         "and suggest adding a runbook.")

    # Closed set of contacts the answer may name (mirrors diagnose self_check).
    allowed_contacts = sorted({
        *(s["escalation_target"].strip() for s in matched if s.get("escalation_target")),
        *([owner_contact.strip()] if owner_contact.strip() else []),
    })

    return {
        "issue_id": issue.id,
        "type": issue.type,
        "handling_state": handling["state"],
        "handling_label": handling["label"],
        "sla_overdue": handling["sla_overdue"],
        "sla_at_risk": handling["sla_at_risk"],
        "selected_scenario_type": issue.selected_scenario_type or "",
        "external_url": issue.external_url or "",
        "handling_mode": handling_mode,
        "handling_note": handling_note,
        "matched_scenario_types": sorted(hints),
        "matched_scenarios": matched,
        "other_scenarios": [s for s in scenarios if not s["matched"]],
        "allowed_contacts": allowed_contacts,
        "resolution_discipline": (
            "Present only matched_scenarios as applicable steps; contacts may come only from "
            "allowed_contacts; if allowed_contacts is empty, say 'the runbook specifies no "
            "escalation target' — never fabricate one."
        ),
    }


# ── analytics-grade tools (wrap analytics_service, never raw SQL) ─────


def _issues_in_period(session, actor: CurrentUser, period,
                      project_id: int = 0, product_id: int = 0, issue_type: str = ""):
    q = _scoped_issue_query(session, actor).filter(
        Issue.created_at >= period.start, Issue.created_at < period.end)
    if product_id:
        q = q.filter(Issue.product_id == product_id)
    if project_id:
        product_rows = session.query(Product.id).filter(Product.project_id == project_id).all()
        q = q.filter(Issue.product_id.in_([r.id for r in product_rows] or [-1]))
    if issue_type:
        q = q.filter(Issue.type == issue_type)
    return q.all()


def issue_stats(
    session,
    actor: CurrentUser,
    year: int = 0,
    month: int = 0,
    week_start: str = "",
    project_id: int = 0,
    product_id: int = 0,
    issue_type: str = "",
) -> dict:
    if issue_type and issue_type not in IssueType.ALL:
        return _bad_enum("issue_type", issue_type, IssueType.ALL)
    period, err = _resolve_period_or_error(year, month, week_start)
    if err:
        return err
    issues = _issues_in_period(session, actor, period,
                               project_id=project_id, product_id=product_id,
                               issue_type=issue_type)
    stats = analytics_service.compute_issue_stats(session, issues)
    fp_count = sum(1 for i in issues if i.status == IssueStatus.FALSE_POSITIVE)
    stats["false_positive_count"] = fp_count
    stats["false_positive_rate_percent"] = round(fp_count / len(issues) * 100) if issues else 0
    scope = f"project={project_id}" if project_id else (
        f"product={product_id}" if product_id else "all visible scope")
    return _envelope(stats, period=period, scope=scope, row_count=len(issues))


_BREAKDOWN_DIMS = ["type", "status", "product", "project", "job", "app", "day"]


def issue_breakdown(
    session,
    actor: CurrentUser,
    group_by: str,
    year: int = 0,
    month: int = 0,
    week_start: str = "",
    days: int = 0,
    status: str = "",
    issue_type: str = "",
    project_id: int = 0,
    product_id: int = 0,
) -> dict:
    """Group issues along one dimension (asset, type, status or day) so
    questions like "失败 Issue 分别是哪些 Job 造成的" / "本月每天多少 Issue"
    are one call instead of N issue_detail round-trips."""
    if group_by not in _BREAKDOWN_DIMS:
        return _bad_enum("group_by", group_by, _BREAKDOWN_DIMS)
    if status and status not in IssueStatus.ALL:
        return _bad_enum("status", status, IssueStatus.ALL)
    if issue_type and issue_type not in IssueType.ALL:
        return _bad_enum("issue_type", issue_type, IssueType.ALL)

    period = None
    if days > 0:
        end = datetime.utcnow()
        start = end - timedelta(days=days)
        label = f"last {days} days"
    else:
        period, err = _resolve_period_or_error(year, month, week_start)
        if err:
            return err
        start, end, label = period.start, period.end, period.label

    q = _scoped_issue_query(session, actor).filter(
        Issue.created_at >= start, Issue.created_at < end)
    if status:
        q = q.filter(Issue.status == status)
    if issue_type:
        q = q.filter(Issue.type == issue_type)
    if product_id:
        q = q.filter(Issue.product_id == product_id)
    if project_id:
        product_rows = session.query(Product.id).filter(Product.project_id == project_id).all()
        q = q.filter(Issue.product_id.in_([r.id for r in product_rows] or [-1]))
    issues = q.all()

    products = {p.id: p for p in session.query(Product).filter(
        Product.id.in_({i.product_id for i in issues if i.product_id} or {-1})).all()}

    def _group_key(i: Issue):
        if group_by == "type":
            return i.type, i.type
        if group_by == "status":
            return i.status, i.status
        if group_by == "day":
            d = i.created_at.date().isoformat() if i.created_at else "(no timestamp)"
            return d, d
        if group_by == "product":
            p = products.get(i.product_id)
            return i.product_id or 0, (p.name if p else "(no linked Product)")
        if group_by == "project":
            p = products.get(i.product_id)
            return (p.project_id if p else 0), ""
        if group_by == "job":
            return i.job_id or 0, ""
        return i.app_id or 0, ""  # app

    grouped: dict = {}
    for i in issues:
        key, name = _group_key(i)
        g = grouped.setdefault(key, {"label": name, "count": 0, "open_count": 0})
        g["count"] += 1
        if i.status in (IssueStatus.OPEN, IssueStatus.IN_PROGRESS):
            g["open_count"] += 1

    # Resolve display labels for id-keyed dimensions in one query each.
    if group_by == "job":
        jobs = {j.id: j for j in session.query(Job).filter(
            Job.id.in_([k for k in grouped if k] or [-1])).all()}
        for k, g in grouped.items():
            j = jobs.get(k)
            g["label"] = (j.cml_job_name or j.control_m_job_name) if j else (
                "(no linked Job)" if not k else f"job:{k}")
            g["job_id"] = k or None
    elif group_by == "app":
        apps = {a.id: a for a in session.query(Application).filter(
            Application.id.in_([k for k in grouped if k] or [-1])).all()}
        for k, g in grouped.items():
            a = apps.get(k)
            g["label"] = a.cml_application_name if a else (
                "(no linked App)" if not k else f"app:{k}")
            g["app_id"] = k or None
    elif group_by == "project":
        names = {p.id: p.name for p in session.query(Project).filter(
            Project.id.in_([k for k in grouped if k] or [-1])).all()}
        for k, g in grouped.items():
            g["label"] = names.get(k, "(no linked Project)" if not k else f"project:{k}")
            g["project_id"] = k or None
    elif group_by == "product":
        for k, g in grouped.items():
            g["product_id"] = k or None

    if group_by == "day":
        # Zero-fill so the series is chartable without the model inventing
        # missing dates.
        cursor = start.date()
        last = min(end, datetime.utcnow()).date()
        while cursor <= last:
            d = cursor.isoformat()
            grouped.setdefault(d, {"label": d, "count": 0, "open_count": 0})
            cursor += timedelta(days=1)
        rows = [grouped[k] for k in sorted(grouped)]
    else:
        rows = sorted(grouped.values(), key=lambda g: -g["count"])

    truncated = len(rows) > _MAX_ROWS
    filters = ", ".join(x for x in (
        f"status={status}" if status else "",
        f"issue_type={issue_type}" if issue_type else "",
        f"project={project_id}" if project_id else "",
        f"product={product_id}" if product_id else "",
    ) if x) or "all visible scope"
    return _envelope(
        {"group_by": group_by, "window": label, "rows": rows[:_MAX_ROWS]},
        period=period, scope=filters, row_count=len(rows), truncated=truncated,
    )


def _scoped_products(session, actor: CurrentUser, project_id: int = 0) -> list:
    product_ids = _accessible_product_ids(session, actor)
    q = session.query(Product)
    if product_ids is not None:
        q = q.filter(Product.id.in_(product_ids or [-1]))
    if project_id:
        q = q.filter(Product.project_id == project_id)
    return q.order_by(Product.name.asc(), Product.id.asc()).all()


def _open_issues_for_products(session, actor: CurrentUser, product_ids: list) -> list:
    if not product_ids:
        return []
    return _scoped_issue_query(session, actor).filter(
        Issue.product_id.in_(product_ids),
        Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS]),
    ).all()


def _health_item_compact(item: dict) -> dict:
    out = dict(item)
    out["last_failed_at"] = _iso(out.get("last_failed_at"))
    out["last_issue_at"] = _iso(out.get("last_issue_at"))
    return out


def product_health(
    session,
    actor: CurrentUser,
    year: int = 0,
    month: int = 0,
    week_start: str = "",
    project_id: int = 0,
) -> dict:
    period, err = _resolve_period_or_error(year, month, week_start)
    if err:
        return err
    products = _scoped_products(session, actor, project_id=project_id)
    open_issues = _open_issues_for_products(session, actor, [p.id for p in products])
    items = analytics_service.product_health_items(
        session, products,
        period_start=period.start, period_end=period.end,
        rules=analytics_service.get_anomaly_rules(), open_issues=open_issues,
    )
    truncated = len(items) > _MAX_ROWS
    return _envelope(
        {
            "anomaly_rules": analytics_service.get_anomaly_rules().as_dict(),
            "items": [_health_item_compact(i) for i in items[:_MAX_ROWS]],
        },
        period=period,
        scope=f"project={project_id}" if project_id else "all visible products",
        row_count=len(items), truncated=truncated,
    )


def product_health_drilldown(
    session,
    actor: CurrentUser,
    product_id: int,
    year: int = 0,
    month: int = 0,
    week_start: str = "",
) -> dict:
    period, err = _resolve_period_or_error(year, month, week_start)
    if err:
        return err
    product_ids = _accessible_product_ids(session, actor)
    product = session.query(Product).filter(Product.id == product_id).first()
    if product is None or (product_ids is not None and product_id not in product_ids):
        return {"error": "product not found or access denied"}
    open_issues = _open_issues_for_products(session, actor, [product_id])
    data = analytics_service.product_health_drilldown_data(
        session, product,
        period_start=period.start, period_end=period.end,
        rules=analytics_service.get_anomaly_rules(), open_issues=open_issues,
    )
    return _envelope(
        {
            "summary": _health_item_compact(data["summary"]),
            "daily_trend": data["daily_trend"],
            "jobs": [_health_item_compact(j) for j in data["jobs"][:20]],
        },
        period=period, scope=f"product={product_id}",
        row_count=len(data["jobs"]), truncated=len(data["jobs"]) > 20,
    )


def job_execution_history(
    session,
    actor: CurrentUser,
    job_id: int,
    days: int = 30,
    limit: int = 50,
) -> dict:
    from core.services.issue_service import false_positive_execution_keys

    product_ids = _accessible_product_ids(session, actor)
    job = session.query(Job).filter(Job.id == job_id).first()
    if job is None or (product_ids is not None and job.product_id not in product_ids):
        return {"error": "job not found or access denied"}

    since = datetime.utcnow() - timedelta(days=max(1, days))
    executions = (
        session.query(JobExecution)
        .filter(JobExecution.job_id == job_id, JobExecution.timestamp >= since)
        .order_by(JobExecution.timestamp.desc(), JobExecution.id.desc())
        .all()
    )
    fp_keys = false_positive_execution_keys(session, [job_id])

    total = len(executions)
    failures = sum(1 for e in executions if analytics_service.execution_is_failure(e, fp_keys))
    last_failed = next((e.timestamp for e in executions
                        if analytics_service.execution_is_failure(e, fp_keys)), None)
    last_success = next((e.timestamp for e in executions
                         if not analytics_service.execution_is_failure(e, fp_keys)), None)
    rows = [{
        "at": _iso(e.timestamp),
        "status": e.status,
        "is_failure": analytics_service.execution_is_failure(e, fp_keys),
    } for e in executions[:min(limit, _MAX_ROWS * 2)]]

    return _envelope(
        {
            "job_id": job.id,
            "cml_job_name": job.cml_job_name or "",
            "control_m_job_name": job.control_m_job_name or "",
            "schedule_cron": job.schedule_cron or "",
            "summary": {
                "total_runs": total,
                "failures": failures,
                "failure_rate_percent": round(failures / total * 100) if total else 0,
                "max_repeat_failure_streak": analytics_service.max_failed_streak(executions, fp_keys),
                "last_failed_at": _iso(last_failed),
                "last_success_at": _iso(last_success),
            },
            "executions": rows,
        },
        scope=f"job={job_id} last {days} days",
        row_count=total, truncated=total > len(rows),
    )


def _job_if_visible(session, actor: CurrentUser, job_id: int):
    """The Job if it exists and the actor may read it, else None — same scoping
    as job_execution_history."""
    product_ids = _accessible_product_ids(session, actor)
    job = session.query(Job).filter(Job.id == job_id).first()
    if job is None or (product_ids is not None and job.product_id not in product_ids):
        return None
    return job


def _natural_interval_minutes(cron: str) -> Optional[int]:
    """Schedule-intrinsic inter-run interval (minutes), independent of the wall
    clock and of when the last run happened. Median gap over the next ~12
    scheduled fires from a fixed reference, so daily→1440, weekly→10080, and a
    weekday-only cron→1440 (the modal 1-day gap, not the Fri→Mon outlier).

    Unlike staleness.estimate_cron_interval_minutes (which returns the gap from
    *now* to the next fire — only 0..interval), this is stable for SLA display."""
    from core.integrations.staleness import next_fire_after

    ref = datetime(2026, 1, 5)  # a Monday; fixed → clock-independent
    fires: list = []
    t = ref
    for _ in range(12):
        nf = next_fire_after(cron, t)
        if nf is None:
            break
        fires.append(nf)
        t = nf
    if len(fires) < 2:
        return None
    gaps = [(b - a).total_seconds() / 60.0 for a, b in zip(fires, fires[1:])]
    return int(round(_median(gaps)))


def job_sla_config(session, actor: CurrentUser, job_id: int) -> dict:
    """Read-only: a Job's staleness/SLA threshold config. Every number is derived
    the same way the monitoring staleness check does (cron × safety_factor); the
    model only relays it and never invents a "default 1 hour"."""
    import math

    from core.checker.cml_checker import _safety_factor_for
    from core.integrations.staleness import _CRON_SAFETY_FACTOR

    job = _job_if_visible(session, actor, job_id)
    if job is None:
        return {"error": "job not found or access denied"}

    cron = (job.schedule_cron or job.control_m_cron or "").strip()
    factor = _safety_factor_for(job.sla_preset)
    preset = (job.sla_preset or "").strip().lower() or "normal"
    if factor is None:  # unset / "normal" / unrecognised → global default
        factor = _CRON_SAFETY_FACTOR
        preset = "normal"
    expected = _natural_interval_minutes(cron) if cron else None
    threshold = int(math.ceil(expected * factor)) if expected else None

    return _envelope(
        {
            "job_id": job.id,
            "schedule_cron": cron,
            "sla_preset": preset,                 # strict / normal / loose
            "safety_factor": factor,              # 1.0 / 1.5 / 3.0
            "preset_options": {"strict": 1.0, "normal": 1.5, "loose": 3.0},
            "expected_interval_minutes": expected,
            "stale_threshold_minutes": threshold,  # None = cron missing/unparseable
            "timezone_note": "cron is interpreted in SGT (UTC+8)",
            "source": "stale_threshold = ceil(expected_interval × safety_factor), "
                      "same basis as the monitoring staleness check",
        },
        scope=f"job={job_id}",
    )


def _nearest_scheduled_fire(cron: str, ts: datetime):
    """(nearest_fire_utc, signed_deviation_minutes) for one run timestamp.

    Deviation > 0 = ran after the fire, < 0 = before it, relative to the closest
    scheduled cron fire. cron is interpreted in SGT inside ``next_fire_after`` —
    all timezone math stays server-side so the model never recomputes it."""
    from core.integrations.staleness import next_fire_after

    nxt = next_fire_after(cron, ts)  # strictly after ts
    interval = _natural_interval_minutes(cron) or 1440
    walk = next_fire_after(cron, ts - timedelta(minutes=interval * 2 + 60))
    prev = None
    while walk is not None and walk <= ts:
        prev = walk
        walk = next_fire_after(cron, walk)
    candidates = [f for f in (prev, nxt) if f is not None]
    if not candidates:
        return None, None
    nearest = min(candidates, key=lambda f: abs((ts - f).total_seconds()))
    return nearest, (ts - nearest).total_seconds() / 60.0


# A run "belongs to" a scheduled fire only if within this tolerance of it;
# beyond it the run is an extra/unscheduled execution (manual rerun, missed slot
# processed late), NOT a giant deviation. Tolerance = a quarter of the natural
# interval, capped at 12h so a weekly job's mid-week rerun is "extra", not
# "3 days early" (the 2nd-round bug). on_time band: within 5 minutes.
def _adherence_tolerance_minutes(interval_minutes: int) -> float:
    return min(interval_minutes * 0.25, 720.0)


def job_schedule_adherence(session, actor: CurrentUser, job_id: int,
                           days: int = 30) -> dict:
    """Read-only: classify each run vs the cron schedule (on_time/early/late, or
    **extra** when it maps to no scheduled slot) and report deviation stats over
    the aligned runs only. cron is interpreted in SGT; all timezone + arithmetic
    happens here — the model must never recompute deviations from raw timestamps."""
    from core.integrations.staleness import _resolve_cron_tz_offset_minutes, next_fire_after

    job = _job_if_visible(session, actor, job_id)
    if job is None:
        return {"error": "job not found or access denied"}
    cron = (job.schedule_cron or job.control_m_cron or "").strip()
    if not cron:
        return _envelope(
            {"job_id": job.id, "schedule_cron": "",
             "note": "this job has no cron; schedule deviation cannot be computed"},
            scope=f"job={job_id}",
        )

    since = datetime.utcnow() - timedelta(days=max(1, days))
    executions = (
        session.query(JobExecution)
        .filter(JobExecution.job_id == job_id, JobExecution.timestamp >= since)
        .order_by(JobExecution.timestamp.desc())
        .limit(_MAX_ROWS)
        .all()
    )
    offset = _resolve_cron_tz_offset_minutes()
    interval = _natural_interval_minutes(cron) or 1440
    tol = _adherence_tolerance_minutes(interval)

    rows: list = []
    aligned_devs: List[float] = []
    extra_count = 0
    for e in executions:
        ts = e.timestamp
        if ts is None:
            continue
        fire, dev = _nearest_scheduled_fire(cron, ts)
        scheduled_sgt = _iso(fire + timedelta(minutes=offset)) if fire else None
        if dev is None or abs(dev) > tol:
            # No scheduled slot within tolerance → extra/unscheduled run. We do
            # NOT report a deviation for it (a mid-week run on a weekly job is an
            # extra run, not "3 days early").
            extra_count += 1
            rows.append({
                "at_utc": _iso(ts), "scheduled_sgt": scheduled_sgt,
                "deviation_minutes": None, "classification": "extra",
                "note": "too far from any scheduled fire; treated as an extra/backfill run, excluded from deviation stats",
            })
            continue
        cls = "on_time" if abs(dev) <= 5 else ("late" if dev > 0 else "early")
        aligned_devs.append(abs(dev))
        rows.append({
            "at_utc": _iso(ts), "scheduled_sgt": scheduled_sgt,
            "deviation_minutes": round(dev), "classification": cls,
        })

    # Missed slots: scheduled fires inside the observed window with no aligned run.
    run_ts = [e.timestamp for e in executions if e.timestamp is not None]
    missed = 0
    if len(run_ts) >= 2:
        lo, hi = min(run_ts), max(run_ts)
        fire = next_fire_after(cron, lo - timedelta(minutes=1))
        guard = 0
        while fire is not None and fire <= hi and guard < 500:
            guard += 1
            if not any(abs((r - fire).total_seconds()) / 60.0 <= tol for r in run_ts):
                missed += 1
            fire = next_fire_after(cron, fire)

    return _envelope(
        {
            "job_id": job.id,
            "schedule_cron": cron,
            "timezone": f"cron is interpreted in SGT (UTC+{offset // 60}); at_utc=UTC, "
                        f"scheduled_sgt=SGT wall clock; deviation>0 ran late, <0 ran early",
            "expected_interval_minutes": interval,
            "alignment_tolerance_minutes": round(tol),
            "aligned_run_count": len(aligned_devs),
            "extra_run_count": extra_count,
            "missed_slot_count": missed,
            "median_abs_deviation_minutes": round(_median(aligned_devs)) if aligned_devs else None,
            "max_abs_deviation_minutes": round(max(aligned_devs)) if aligned_devs else None,
            "runs": rows,
        },
        scope=f"job={job_id} last {days} days",
        row_count=len(rows),
    )


def mmp_overview(session, actor: CurrentUser, product_id: int = 0) -> dict:
    products = _scoped_products(session, actor)
    if product_id:
        products = [p for p in products if p.id == product_id]
        if not products:
            return {"error": "product not found or access denied"}
    pids = [p.id for p in products]

    mmp_types = [IssueType.MMP_DRIFT, IssueType.MMP_FAIRNESS_RISK,
                 IssueType.MMP_RUN_PENDING_APPROVAL, IssueType.MMP_PENDING_REVIEW,
                 IssueType.MMP_UNAPPROVED_EXP_RUN]
    open_mmp = _scoped_issue_query(session, actor).filter(
        Issue.type.in_(mmp_types),
        Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS]),
        Issue.product_id.in_(pids or [-1]),
    ).order_by(Issue.created_at.desc()).limit(_MAX_ROWS).all()

    by_type: dict[str, int] = {}
    items = []
    for i in open_mmp:
        by_type[i.type] = by_type.get(i.type, 0) + 1
        handling = classify_handling_state(i)
        items.append({
            "issue_id": i.id, "type": i.type, "title": i.title,
            "handling_state": handling["state"], "product_id": i.product_id,
            "job_id": i.job_id, "external_url": i.external_url or "",
            "created_at": _iso(i.created_at),
        })

    mmp_jobs = (session.query(Job)
                .filter(Job.product_id.in_(pids or [-1]), Job.mmp_model_id != "",
                        Job.mmp_model_id.isnot(None))
                .limit(_MAX_ROWS).all())
    drift = []
    for job in mmp_jobs:
        snap = (session.query(MmpDriftSnapshot)
                .filter(MmpDriftSnapshot.job_id == job.id)
                .order_by(MmpDriftSnapshot.observed_at.desc()).first())
        drift.append({
            "job_id": job.id, "mmp_model_id": job.mmp_model_id,
            "cml_job_name": job.cml_job_name or "",
            "latest_drifted": bool(snap.drifted) if snap else None,
            "observed_at": _iso(snap.observed_at) if snap else None,
            "details": (snap.drift_details or "")[:200] if snap else "",
        })

    return _envelope(
        {"open_issue_counts_by_type": by_type, "open_issues": items,
         "model_drift_status": drift},
        scope=f"product={product_id}" if product_id else "all visible products",
        row_count=len(items),
    )


# ── knowledge retrieval (FTS sidecar; RBAC re-filter happens here) ────

_SEARCH_TARGETS = ["resolutions", "runbooks"]


def search_knowledge(
    session,
    actor: CurrentUser,
    query: str,
    target: str = "resolutions",
    issue_type: str = "",
    limit: int = 5,
) -> dict:
    from core.agent import retrieval

    if target not in _SEARCH_TARGETS:
        return _bad_enum("target", target, _SEARCH_TARGETS)
    if issue_type and issue_type not in IssueType.ALL:
        return _bad_enum("issue_type", issue_type, IssueType.ALL)
    if not (query or "").strip():
        return {"error": "query must not be empty"}
    limit = max(1, min(limit, 20))

    if target == "runbooks":
        hits = retrieval.search_runbooks(query, limit=limit * 3)
        # Scope: runbook scenarios hang off jobs/apps inside accessible products.
        product_ids = _accessible_product_ids(session, actor)
        if product_ids is not None:
            job_ids = {r.id for r in session.query(Job.id)
                       .filter(Job.product_id.in_(product_ids or [-1])).all()}
            app_ids = {r.id for r in session.query(Application.id)
                       .filter(Application.product_id.in_(product_ids or [-1])).all()}
            hits = [h for h in hits
                    if (h["kind"] == "job" and h["entity_id"] in job_ids)
                    or (h["kind"] == "app" and h["entity_id"] in app_ids)]
        rows = [{k: v for k, v in h.items() if k != "score"} for h in hits[:limit]]
        return _envelope(rows, scope="runbooks of visible products", row_count=len(rows))

    hits = retrieval.search_resolutions(query, issue_type=issue_type, limit=limit * 3)
    if hits:
        visible_ids = {
            r.id for r in _scoped_issue_query(session, actor)
            .filter(Issue.id.in_([h["issue_id"] for h in hits])).all()
        }
        hits = [h for h in hits if h["issue_id"] in visible_ids]
    rows = [{
        "issue_id": h["issue_id"], "type": h["issue_type"], "title": h["title"],
        "resolved_at": h["resolved_at"] or None, "resolution": h["resolution"],
    } for h in hits[:limit]]
    note = "" if rows else ("(index empty or no match — the index is written incrementally on issue "
                            "close; you can also run resolution_fts_backfill to rebuild it)")
    return _envelope({"hits": rows, "note": note},
                     scope="resolution history of visible issues", row_count=len(rows))


_COMPARE_SCOPES = ["all", "project", "product"]


def compare_periods(
    session,
    actor: CurrentUser,
    a_year: int = 0,
    a_month: int = 0,
    a_week_start: str = "",
    b_year: int = 0,
    b_month: int = 0,
    b_week_start: str = "",
    scope: str = "all",
    target_id: int = 0,
) -> dict:
    if scope not in _COMPARE_SCOPES:
        return _bad_enum("scope", scope, _COMPARE_SCOPES)
    if scope != "all" and not target_id:
        return {"error": "target_id is required when scope is project/product"}
    period_a, err = _resolve_period_or_error(a_year, a_month, a_week_start)
    if err:
        return {"error": f"period_a: {err['error']}"}
    period_b, err = _resolve_period_or_error(b_year, b_month, b_week_start)
    if err:
        return {"error": f"period_b: {err['error']}"}

    project_id = target_id if scope == "project" else 0
    product_id = target_id if scope == "product" else 0

    def _snapshot(period) -> dict:
        issues = _issues_in_period(session, actor, period,
                                   project_id=project_id, product_id=product_id)
        stats = analytics_service.compute_issue_stats(session, issues)
        products = _scoped_products(session, actor, project_id=project_id)
        if product_id:
            products = [p for p in products if p.id == product_id]
        open_issues = _open_issues_for_products(session, actor, [p.id for p in products])
        health = analytics_service.product_health_items(
            session, products,
            period_start=period.start, period_end=period.end,
            rules=analytics_service.get_anomaly_rules(), open_issues=open_issues,
        )
        runs = sum(h["job_runs_total"] for h in health)
        fails = sum(h["job_failures"] for h in health)
        return {
            "label": period.label,
            "issue_total": stats["total"],
            "sla_compliance_rate": stats["sla_compliance_rate"],
            "avg_resolution_minutes": stats["avg_resolution_minutes"],
            "manual_interventions": stats["manual_interventions"],
            "job_runs_total": runs,
            "job_failures": fails,
            "failure_rate_percent": round(fails / runs * 100) if runs else 0,
            "by_type": stats["by_type"],
        }

    snap_a = _snapshot(period_a)
    snap_b = _snapshot(period_b)
    deltas = {
        key: {"a": snap_a[key], "b": snap_b[key], "delta": snap_b[key] - snap_a[key]}
        for key in ("issue_total", "sla_compliance_rate", "avg_resolution_minutes",
                    "manual_interventions", "job_runs_total", "job_failures",
                    "failure_rate_percent")
    }
    return _envelope(
        {"period_a": snap_a, "period_b": snap_b, "deltas": deltas},
        scope=f"{scope}={target_id}" if target_id else "all visible scope",
    )


# ── langchain wrapping (guarded) ─────────────────────────────────────


def build_langchain_tools(actor: CurrentUser, artifact_sink: Optional[list] = None) -> list:
    """Wrap the implementations as langchain tools bound to ``actor``. Each
    call opens (and always closes) its own session.

    ``artifact_sink``: per-turn list the render tool pushes full chart/table
    artifacts into; the chat loop drains it into SSE events. Row data never
    enters the model context (see core/agent/artifacts.py)."""
    from langchain_core.tools import tool

    # Per-turn call guards (RELIABILITY_PLAN §8): exact-repeat dedup returns the
    # cached result (the 调用克制 prompt rule turned into a mechanism), and a
    # high runaway-backstop cap (_PER_TOOL_CALL_LIMIT). The cap must stay well
    # above legitimate fan-out — over-query discipline is the prompt's job.
    _seen: dict = {}
    _name_counts: dict = {}

    def _run(fn, /, **params):
        name = fn.__name__
        key = (name, repr(sorted(params.items())))
        if key in _seen:
            return _seen[key]
        _name_counts[name] = _name_counts.get(name, 0) + 1
        if _name_counts[name] > _PER_TOOL_CALL_LIMIT:
            return {"error": f"reached the safety cap on calls to {name} this turn "
                             f"({_PER_TOOL_CALL_LIMIT}). Answer from the results already "
                             f"gathered, or switch to a more aggregated tool "
                             f"(e.g. relayops_query / relayops_issue_breakdown)."}
        session = get_db().get_session()
        try:
            result = fn(session, actor, **params)
        finally:
            session.close()
        _seen[key] = result
        return result

    @tool
    def relayops_projects_overview() -> list:
        """列出当前用户可见的所有 Ops 项目及其产品（id、名称、CML 绑定）。回答"有哪些项目/产品"类问题时先调这个。"""
        return _run(projects_overview)

    @tool
    def relayops_product_assets(product_id: int) -> dict:
        """列出某个 product 下的全部 Job 和 App：排程 cron、Control-M 真名、MMP 绑定、owner、
        CML 绑定状态，且每个资产带 stats（未关 issue 数、近 30 天 issue 数；Job 另有近 30 天
        运行/失败次数与最近一次运行）。回答"这个产品下各资产怎么样"直接用它，不要逐个查。"""
        return _run(product_assets, product_id=product_id)

    @tool
    def relayops_list_issues(status: str = "", issue_type: str = "", days: int = 7,
                        sla_risk_only: bool = False, limit: int = 20,
                        product_id: int = 0, job_id: int = 0, app_id: int = 0) -> list:
        """查询 Issue（告警/工单）。status 可选 open/in_progress/resolved/closed；issue_type 如
        job_failed/app_offline/mmp_drift；days 为最近 N 天（0=不限，查"历史上有没有"必须传 0）；
        sla_risk_only=True 时按 SLA 截止时间升序只看未关单，用于"哪些快超时"。
        product_id/job_id/app_id 可把范围锁定到具体资产。返回行带 selected_scenario_type/name
        （处理人选择的 runbook 场景，空=未选）。"""
        return _run(list_issues, status=status, issue_type=issue_type, days=days,
                    sla_risk_only=sla_risk_only, limit=limit,
                    product_id=product_id, job_id=job_id, app_id=app_id)

    @tool
    def relayops_query(entity: str = "issues", group_by: List[str] = [],
                  status: List[str] = [], issue_type: List[str] = [],
                  project_id: int = 0, product_id: int = 0,
                  job_id: int = 0, app_id: int = 0, assignee_id: int = 0,
                  has_scenario: Optional[bool] = None, title_contains: str = "",
                  days: int = 0, year: int = 0, month: int = 0,
                  week_start: str = "", limit: int = 50) -> dict:
        """通用结构化聚合查询——现成工具没有的维度组合用它自己拼，不要说"接口里没有"。
        entity=issues（指标 count/open_count）或 executions（Job 运行记录，指标
        runs/failures/failure_rate_percent，误报已修正）。
        group_by 0-2 个维度：通用 product/project/day/week；issues 另有
        type/status/job/app/assignee/scenario_type；executions 另有 job/status。
        例：哪些 Job 的失败 issue 最多且按类型拆 → entity=issues, issue_type=
        ["job_failed"], group_by=["job","type"]；本月每周运行失败率 →
        entity=executions, group_by=["week"]。
        过滤：status/issue_type 列表、资产 id、has_scenario（是否已选 runbook 场景）、
        title_contains 模糊匹配；时间窗 days（滚动）或 year/month/week_start（日历）。
        group_by=[] 时返回单行总计。group_by=["day"] 已按天补零，可直接配
        relayops_render_chart 出时序图。"""
        from core.agent.query_engine import run_query

        spec = {"entity": entity, "group_by": group_by, "status": status,
                "issue_type": issue_type, "project_id": project_id,
                "product_id": product_id, "job_id": job_id, "app_id": app_id,
                "assignee_id": assignee_id, "has_scenario": has_scenario,
                "title_contains": title_contains, "days": days, "year": year,
                "month": month, "week_start": week_start, "limit": limit}
        return _run(run_query, spec_dict=spec)

    @tool
    def relayops_issue_breakdown(group_by: str, year: int = 0, month: int = 0,
                            week_start: str = "", days: int = 0,
                            status: str = "", issue_type: str = "",
                            project_id: int = 0, product_id: int = 0) -> dict:
        """Issue 按单一维度聚合计数（每组带 count 与 open_count）。group_by 可选：
        job/app/product/project（"这些 issue 是哪些资产造成的"，带资产 id 与名称）、
        type/status（分布）、day（每日计数，已补零，可直接画时序图）。
        支持 status/issue_type/project_id/product_id 过滤与 year/month/week_start 或 days 窗口。
        回答"按 X 拆分/分别是谁造成的/每天多少"用它，禁止逐条翻 relayops_issue_detail 自己数。"""
        return _run(issue_breakdown, group_by=group_by, year=year, month=month,
                    week_start=week_start, days=days, status=status,
                    issue_type=issue_type, project_id=project_id, product_id=product_id)

    @tool
    def relayops_on_duty_now() -> list:
        """查询当前值班（on-duty）的 Ops 成员。"""
        return _run(on_duty_now)

    @tool
    def relayops_job_runbook(job_id: int) -> dict:
        """取某个 Job 的详情与全部故障场景 runbook（诊断/处置/验证步骤、升级联系人）。"""
        return _run(job_runbook, job_id=job_id)

    @tool
    def relayops_app_runbook(app_id: int) -> dict:
        """取某个 App 的详情与全部恢复场景 runbook（处置/验证步骤、升级联系人）。"""
        return _run(app_runbook, app_id=app_id)

    @tool
    def relayops_domain_glossary() -> dict:
        """取平台领域词表全文：全部 Issue 类型/状态/处置动作的语义、runbook 场景词表、
        健康度异常规则与分档。对枚举含义不确定时先查它，禁止凭感觉解释。"""
        return _run(domain_glossary)

    @tool
    def relayops_issue_detail(issue_id: int) -> dict:
        """取单个 Issue 全量信息：描述、系统判定的处置状态（handling_state，比 status 准确）、
        动作时间线（谁在何时做了什么/升级给了谁）、处置记录、关联 job/app/product。
        讨论某个具体 Issue 时必须先调它。"""
        return _run(issue_detail, issue_id=issue_id)

    @tool
    def relayops_issue_resolution(issue_id: int) -> dict:
        """"这个 Issue 怎么处理/怎么 resolve/有没有 runbook" 必用。代码确定性地把
        Issue 类型映射到对应 runbook 场景（matched_scenarios），你只复述、不要自己猜哪个场景适用。
        返回 handling_mode（platform_workflow=无 runbook 走平台审批 / runbook_scenario=按匹配场景 /
        runbook_no_match=有 runbook 但无匹配场景 / no_runbook=无任何场景）、handling_note 处置纲领、
        allowed_contacts（唯一可命名的升级联系人闭集，为空就说未指定，禁止编造）、external_url 深链。
        比直接 relayops_job_runbook/relayops_app_runbook 更准——后者不告诉你哪个场景对得上本 Issue。"""
        return _run(issue_resolution, issue_id=issue_id)

    @tool
    def relayops_issue_stats(year: int = 0, month: int = 0, week_start: str = "",
                        project_id: int = 0, product_id: int = 0, issue_type: str = "") -> dict:
        """Issue 统计分析（月或周粒度）：总数、按类型/产品/项目分布、SLA 达标率、
        MTTR（平均/最大解决分钟数）、人工干预数、误报率、open/in_progress/resolved 计数。
        回答"分布/占比/达标率/处理效率"类问题用它，不要自己数 relayops_list_issues 的行。
        year/month 不传 = 当月；week_start=YYYY-MM-DD 切周视图。"""
        return _run(issue_stats, year=year, month=month, week_start=week_start,
                    project_id=project_id, product_id=product_id, issue_type=issue_type)

    @tool
    def relayops_product_health(year: int = 0, month: int = 0, week_start: str = "",
                           project_id: int = 0) -> dict:
        """全部可见产品的健康度排名（最差在前）：失败率、连续失败 streak、open issue 数、
        anomaly_score、severity（healthy/watch/risk/anomaly）、命中的异常规则。
        回答"哪个产品有问题/不健康/异常"用它。"""
        return _run(product_health, year=year, month=month, week_start=week_start,
                    project_id=project_id)

    @tool
    def relayops_product_health_drilldown(product_id: int, year: int = 0, month: int = 0,
                                     week_start: str = "") -> dict:
        """单个产品的健康下钻：周期汇总 + 日粒度失败率趋势（折线图数据源）+ 按 Job
        拆分的失败排名。分析"某产品最近怎么样/趋势"必用。"""
        return _run(product_health_drilldown, product_id=product_id,
                    year=year, month=month, week_start=week_start)

    @tool
    def relayops_job_execution_history(job_id: int, days: int = 30, limit: int = 50) -> dict:
        """单个 Job 的执行历史（误报已修正）：每次运行的时间/状态/是否失败 + 汇总
        （失败率、最大连续失败、最近一次成功/失败时间）。分析"这个 job 最近怎么样"用它。"""
        return _run(job_execution_history, job_id=job_id, days=days, limit=limit)

    @tool
    def relayops_job_sla_config(job_id: int) -> dict:
        """读取某个 Job 的 staleness/SLA 配置：sla_preset（strict/normal/loose 三档，
        对应 safety_factor 1.0/1.5/3.0，未设=normal）、按 cron 推导的预期运行间隔、
        以及算出的 stale 阈值分钟数。**问"这个 job 的阈值是多少 / 有没有个性化 SLA 档位 /
        是哪一档"必须用我**——不要从 runbook 里猜，更不要回答"默认 1 小时"。"""
        return _run(job_sla_config, job_id=job_id)

    @tool
    def relayops_job_schedule_adherence(job_id: int, days: int = 30) -> dict:
        """计算某 Job 实际运行 vs cron 计划时间（cron 按 SGT，时区换算在服务端）。
        每次运行带 classification：on_time/early/late（对齐计划点）或 **extra**
        （距任何计划点过远=额外/补跑，**不计偏差**，deviation_minutes 为 null）。
        汇总：aligned_run_count / extra_run_count / missed_slot_count、以及**仅按对齐
        运行**算的中位/最大绝对偏差。**问"运行是否准时/偏离多少/是否多跑漏跑"必须用我，
        直接引用我返回的 classification 与统计，禁止自己拿时间戳和 cron 硬算偏差，
        更不要把 extra 运行说成"提前 N 小时"。**"""
        return _run(job_schedule_adherence, job_id=job_id, days=days)

    @tool
    def relayops_mmp_overview(product_id: int = 0) -> dict:
        """MMP 模型治理总览：当前未关的 MMP 类 Issue（漂移/公平性/待审批/待 review）
        按类型计数与明细（带 MMP 深链）、每个绑定模型 Job 的最新漂移快照。
        回答"几个 run 等审批/哪些模型在漂移"用它。"""
        return _run(mmp_overview, product_id=product_id)

    @tool
    def relayops_compare_periods(a_year: int = 0, a_month: int = 0, a_week_start: str = "",
                            b_year: int = 0, b_month: int = 0, b_week_start: str = "",
                            scope: str = "all", target_id: int = 0) -> dict:
        """两个时间段的环比对比（A=基期，B=对比期）：issue 总数、SLA 达标率、MTTR、
        人工干预、运行/失败次数、失败率，差值已算好（delta=B-A）。
        回答"比上月好转还是恶化"用它。scope 可选 all/project/product（后两者需 target_id）。"""
        return _run(compare_periods, a_year=a_year, a_month=a_month, a_week_start=a_week_start,
                    b_year=b_year, b_month=b_month, b_week_start=b_week_start,
                    scope=scope, target_id=target_id)

    @tool
    def relayops_search_resolutions(query: str, target: str = "resolutions",
                               issue_type: str = "", limit: int = 5) -> dict:
        """全文检索历史知识（BM25）。target=resolutions：搜历史已解决 Issue 的处置记录
        （"以前遇到过 XXX 怎么解决的"）；target=runbooks：搜 runbook 场景全文
        （"哪个 runbook 提到过 kerberos"）。query 用问题里的关键词（报错文本、系统名、现象）。"""
        return _run(search_knowledge, query=query, target=target,
                    issue_type=issue_type, limit=limit)

    @tool
    def relayops_render_chart(dataset: str, chart_type: str = "", title: str = "",
                         product_id: int = 0, project_id: int = 0, job_id: int = 0,
                         year: int = 0, month: int = 0, week_start: str = "",
                         days: int = 0, months: int = 0,
                         group_by: str = "", status: str = "", issue_type: str = "",
                         scope: str = "", target_id: int = 0,
                         a_year: int = 0, a_month: int = 0, a_week_start: str = "",
                         b_year: int = 0, b_month: int = 0, b_week_start: str = "") -> dict:
        """生成图表/表格并随回答展示给用户（数据由服务端按 dataset 取，禁止也无法传数据行）。
        dataset 可选：
        - product_failure_trend（折线，需 product_id）：某产品日失败率趋势
        - issue_type_distribution（饼图）：Issue 类型分布
        - issue_status_distribution（饼图）：Issue 状态分布
        - issue_breakdown（需 group_by=job/app/product/project/type/status/day）：
          Issue 按维度聚合；day=每日计数时序折线（"每天产生多少 issue"用它），
          其余为柱状/饼图；可加 status/issue_type/product_id/project_id 过滤
        - product_health_ranking（柱状）：产品健康排名
        - job_execution_timeline（表格，需 job_id）：Job 执行时间线
        - sla_compliance_trend（折线，months 默认 6）：逐月 SLA 达标率
        - period_comparison（表格，参数同 relayops_compare_periods）：周期对比
        chart_type 可在 dataset 允许范围内覆盖默认（line/bar/pie/table）。
        用户要"画图/趋势图/饼图/导出表格"时用它；生成后正文引用图表标题，不要复述数据行。"""
        from core.agent import artifacts as artifacts_module

        params = {k: v for k, v in {
            "product_id": product_id, "project_id": project_id, "job_id": job_id,
            "year": year, "month": month, "week_start": week_start,
            "days": days, "months": months,
            "group_by": group_by, "status": status, "issue_type": issue_type,
            "scope": scope, "target_id": target_id,
            "a_year": a_year, "a_month": a_month, "a_week_start": a_week_start,
            "b_year": b_year, "b_month": b_month, "b_week_start": b_week_start,
        }.items() if v}
        session = get_db().get_session()
        try:
            artifact, confirmation = artifacts_module.render(
                session, actor, dataset=dataset, params=params,
                chart_type=chart_type, title=title)
        finally:
            session.close()
        if artifact is not None and artifact_sink is not None:
            artifact_sink.append(artifact)
        elif artifact is not None:
            confirmation = {**confirmation, "warning": "this session has no channel to display charts"}
        return confirmation

    @tool
    def relayops_nav_buttons(project_ids: List[int] = [], product_ids: List[int] = [],
                        title: str = "") -> dict:
        """生成"跳转到 project/product 页面"的快捷按钮，随回答展示给用户。
        当回答聚焦在某几个具体 project 或 product 时调用（id 必须来自工具返回，
        服务端会校验存在性与权限，无权限的 id 会被跳过）。最多各 10 个。"""
        from core.agent import artifacts as artifacts_module

        session = get_db().get_session()
        try:
            artifact, confirmation = artifacts_module.render_nav(
                session, actor, project_ids=project_ids, product_ids=product_ids,
                title=title)
        finally:
            session.close()
        if artifact is not None and artifact_sink is not None:
            artifact_sink.append(artifact)
        elif artifact is not None:
            confirmation = {**confirmation, "warning": "this session has no channel to display buttons"}
        return confirmation

    # ── live platform gateway (CML / MMP read-only, service identity) ──
    from core.agent import live_tools as live

    @tool
    def cml_live_projects(q: str = "") -> dict:
        """实时列出 CML 平台上服务账号可见的全部项目（可选 q 按名搜索），并标注哪些
        已在 Ops 建档（relayops_project_id）。用于：Ops 没建档的项目、对账 Ops 与平台。
        实时接口较慢，Ops 库能答的问题不要用它。"""
        return _run(live.cml_live_projects, q=q)

    @tool
    def cml_live_jobs(cml_project_name: str, q: str = "") -> dict:
        """实时列出某个 CML 项目下的 Job 定义：脚本、排程 schedule、是否 paused、
        CPU/内存、最近更新时间，并标注已建档的 relayops_job_id。用于核对排程/暂停状态、
        发现 Ops 漏建档的 Job。"""
        return _run(live.cml_live_jobs, cml_project_name=cml_project_name, q=q)

    @tool
    def cml_live_job_runs(relayops_job_id: int = 0, cml_project_name: str = "",
                          cml_job_name: str = "", status: str = "",
                          limit: int = 10) -> dict:
        """直接从 CML 拉某 Job 的实时运行历史，含 failure_reason（失败原因）——
        诊断"为什么失败"必用，Ops 库里没有失败原因。优先传 relayops_job_id（自动解析
        CML 绑定并校验权限）；未建档时传 cml_project_name + cml_job_name。
        status 可过滤 failed/succeeded/running/timeout 等。"""
        return _run(live.cml_live_job_runs, relayops_job_id=relayops_job_id,
                    cml_project_name=cml_project_name, cml_job_name=cml_job_name,
                    status=status, limit=limit)

    @tool
    def cml_live_apps(cml_project_name: str = "", relayops_app_id: int = 0,
                      q: str = "") -> dict:
        """实时查 CML Application 当前状态（APPLICATION_RUNNING/STOPPED/FAILED）、
        subdomain、资源配置。"这个 app 现在挂没挂"用它；可传 relayops_app_id 自动解析绑定。"""
        return _run(live.cml_live_apps, cml_project_name=cml_project_name,
                    relayops_app_id=relayops_app_id, q=q)

    @tool
    def mmp_live_projects(q: str = "") -> dict:
        """实时列出 MMP 平台项目目录：repo_name、业务名、模型数、生产模型清单，
        并标注绑定的 relayops_project_id。用于找 MMP 项目、对账绑定关系。"""
        return _run(live.mmp_live_projects, q=q)

    @tool
    def mmp_live_project_status(repo_name: str) -> dict:
        """实时查一个 MMP 项目的治理状态：每个模型的 attention_required（漂移/公平性/
        待审批/待 review/未审批实验 run）+ 最新生产 run 的漂移子标志
        （pmetric/fmean/fmissing）与部署状态，以及绑定它的 Ops Job 列表。
        "模型现在漂没漂/卡在哪一步"用它，比 Ops 快照更准。"""
        return _run(live.mmp_live_project_status, repo_name=repo_name)

    @tool
    def mmp_live_model_raw(repo_name: str, model_name: str, max_runs: int = 20,
                           run_id: int = 0) -> dict:
        """实时拉取某个 MMP 模型的【完整原始返回体】：原样的 attention_required +
        focus_run（按 run_id 溯源的那条 run，缺省为最新生产 run）的完整字段 +
        当前最新生产 run + 最近 max_runs 条生产 run（每条都附 approval_status 的
        中文/英文含义标注），并带 approval_status 映射图例。
        传 run_id 可定位到某次具体 run；需要看 MMP 原始 status/run 明细时用它。
        approval_status：11=已审批·漂移, 12=已审批·无漂移, <11 多为审批中/非生产。"""
        return _run(live.mmp_live_model_raw, repo_name=repo_name,
                    model_name=model_name, max_runs=max_runs, run_id=run_id)

    @tool
    def cml_get_raw(path: str) -> dict:
        """【兜底·原始直读】对 CML API 发只读 GET，返回**完整原始 JSON**（不摘要、不裁剪）。
        当 cml_live_* 摘要工具没暴露你需要的字段、或要分析某端点的完整返回时用它。
        path 是相对路径，例：/api/v2/projects、/api/v2/projects/{id}、
        /api/v2/projects/{id}/jobs、/api/v2/projects/{id}/jobs/{job_id}/runs。
        只读不可写；返回过大会自动截断并提示你缩小 path。正确性优先：能用摘要工具
        就别用它，确有需要再兜底，且只基于真实返回字段作答。"""
        return _run(live.cml_get_raw, path=path)

    @tool
    def mmp_get_raw(path: str) -> dict:
        """【兜底·原始直读】对 MMP API 发只读 GET，返回**完整原始 JSON**（不摘要、不裁剪）。
        当 mmp_live_* 摘要工具不够、要看某 MMP 端点完整返回时用它。
        path 例：/api/projects?shallow=1&page_size=200、
        /api/projects/{id}（含 models+runs+attention_required 完整树）。
        只读不可写；返回过大会自动截断并提示缩小 path。approval_status 含义见
        mmp_live_model_raw 的图例。正确性优先：优先用摘要工具，确有需要再兜底。"""
        return _run(live.mmp_get_raw, path=path)

    return [relayops_projects_overview, relayops_product_assets, relayops_list_issues,
            relayops_query, relayops_issue_breakdown,
            relayops_on_duty_now, relayops_job_runbook, relayops_app_runbook,
            relayops_domain_glossary, relayops_issue_detail, relayops_issue_resolution, relayops_issue_stats,
            relayops_product_health, relayops_product_health_drilldown,
            relayops_job_execution_history, relayops_job_sla_config, relayops_job_schedule_adherence,
            relayops_mmp_overview, relayops_compare_periods,
            relayops_search_resolutions, relayops_render_chart, relayops_nav_buttons,
            cml_live_projects, cml_live_jobs, cml_live_job_runs,
            cml_live_apps, mmp_live_projects, mmp_live_project_status,
            mmp_live_model_raw, cml_get_raw, mmp_get_raw]
