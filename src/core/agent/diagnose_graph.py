"""RelayOps module."""

from __future__ import annotations

import json
import re
from typing import Callable, Iterator, Optional

from core.auth.jwt import CurrentUser
from core.logging import get_logger
from core.models.database import get_db
from core.models.entities import (
    Application,
    ApplicationRecoveryScenario,
    Issue,
    IssueType,
    Job,
    JobExecution,
    JobFailureScenario,
    Product,
    Project,
)
from core.models.mmp_entities import MmpDriftSnapshot
from core.agent.knowledge import ISSUE_SCENARIO_HINTS, classify_handling_state

logger = get_logger(__name__)

# Per-section char budgets (newest-first lists are trimmed from the tail).
# Replaces the old single 30k blob truncation: the issue itself and matched
# runbook scenarios must never lose to noise like unmatched scenarios.
SECTION_BUDGETS = {
    "scenarios": 9_000,
    "recent_executions": 3_000,
    "cml_recent_runs": 3_000,
    "drift_timeline": 3_000,
    "recent_app_issues": 3_000,
    "similar_resolved_issues": 8_000,
}

_MMP_TYPES = {
    IssueType.MMP_DRIFT, IssueType.MMP_FAIRNESS_RISK,
    IssueType.MMP_RUN_PENDING_APPROVAL, IssueType.MMP_PENDING_REVIEW,
    IssueType.MMP_UNAPPROVED_EXP_RUN,
}
_GOVERNANCE_TYPES = {IssueType.HANDOVER_REVIEW, IssueType.SCOPE_CHANGE_REVIEW}
_JOB_TYPES = {IssueType.JOB_NOT_TRIGGERED, IssueType.JOB_FAILED, IssueType.JOB_STALE}


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _with_session(fn):
    """Each node owns (and always closes) its session, like the tool layer."""
    def wrapper(state: dict) -> dict:
        session = get_db().get_session()
        try:
            return fn(state, session)
        finally:
            session.close()
    wrapper.__name__ = fn.__name__
    return wrapper


# ── routing (deterministic decision tree) ─────────────────────────────


def route_branch(issue_type: str, has_job: bool, has_app: bool) -> str:
    if issue_type in _GOVERNANCE_TYPES:
        return "governance"
    if issue_type in _MMP_TYPES:
        return "mmp"
    if issue_type in _JOB_TYPES or (has_job and not has_app):
        return "job" if has_job else "plain"
    if issue_type == IssueType.APP_OFFLINE or has_app:
        return "app" if has_app else "plain"
    return "plain"


# ── nodes ─────────────────────────────────────────────────────────────


@_with_session
def collect_base(state: dict, session) -> dict:
    from core.agent.diagnose import check_access

    issue = check_access(session, state["actor"], state["issue_id"])
    handling = classify_handling_state(issue)
    bundle: dict = {
        "issue": {
            "id": issue.id, "type": issue.type, "status": issue.status,
            "handling_state": handling["state"], "handling_label": handling["label"],
            "handling_detail": handling["detail"],
            "sla_overdue": handling["sla_overdue"], "sla_at_risk": handling["sla_at_risk"],
            "title": issue.title, "description": (issue.description or "")[:4000],
            "created_at": _iso(issue.created_at), "sla_deadline": _iso(issue.sla_deadline),
            "external_url": issue.external_url or "",
        },
    }

    job = session.query(Job).filter(Job.id == issue.job_id).first() if issue.job_id else None
    app = session.query(Application).filter(Application.id == issue.app_id).first() if issue.app_id else None
    product = session.query(Product).filter(Product.id == issue.product_id).first() if issue.product_id else None
    if product is not None:
        project = session.query(Project).filter(Project.id == product.project_id).first()
        bundle["product"] = {"id": product.id, "name": product.name,
                            "project": project.name if project else ""}

    hints = ISSUE_SCENARIO_HINTS.get(issue.type, set())

    if job is not None:
        bundle["job"] = {
            "id": job.id, "cml_job_name": job.cml_job_name or "",
            "control_m_job_name": job.control_m_job_name or "",
            "schedule_cron": job.schedule_cron or "", "owner_contact": job.owner_contact or "",
            "mmp_model_id": job.mmp_model_id or "", "description": job.description or "",
            "dependency_notes": job.dependency_notes or "",
            "cml_binding_error": job.cml_binding_error or "",
        }
        scenarios = session.query(JobFailureScenario).filter(JobFailureScenario.job_id == job.id).all()
        bundle["scenarios"] = [_scenario(s, hints, diagnostic=True) for s in scenarios]

    if app is not None:
        bundle["app"] = {
            "id": app.id, "cml_application_name": app.cml_application_name or "",
            "application_url": app.application_url or "", "owner_contact": app.owner_contact or "",
            "description": app.description or "", "cml_binding_error": app.cml_binding_error or "",
        }
        scenarios = session.query(ApplicationRecoveryScenario).filter(
            ApplicationRecoveryScenario.application_id == app.id).all()
        bundle["scenarios"] = [_scenario(s, hints, diagnostic=False) for s in scenarios]

    state["issue_type"] = issue.type
    state["job_id"] = issue.job_id
    state["app_id"] = issue.app_id
    state["bundle"] = bundle
    return state


def _scenario(s, hints: set, *, diagnostic: bool) -> dict:
    def _steps(v):
        return [str(x) for x in v] if isinstance(v, list) else []

    out = {
        "matched": s.scenario_type in hints,
        "scenario_type": s.scenario_type,
        "scenario_name": s.scenario_name or "",
        "condition": s.condition_description or "",
        "action_steps": _steps(s.action_steps),
        "verification_steps": _steps(s.verification_steps),
        "escalation_target": s.escalation_target or "",
    }
    if diagnostic:
        out["diagnostic_steps"] = _steps(s.diagnostic_steps)
    return out


def triage(state: dict) -> dict:
    """Decision tree: pick the evidence branch and write the checklist the
    report will show — new on-duty members learn *what to look at* from it."""
    branch = route_branch(state["issue_type"], bool(state.get("job_id")), bool(state.get("app_id")))
    hints = sorted(ISSUE_SCENARIO_HINTS.get(state["issue_type"], set()))
    checklist = ["Issue 上下文与处置状态（status + 动作时间线判定）",
                 f"runbook 场景（优先匹配类型：{hints or '无指定'}）"]
    if branch == "job":
        checklist += ["本地执行时间线（监控记录的最近 10 次）",
                      "CML 实时 run 历史与失败原因（best-effort，连不上则降级）"]
    elif branch == "app":
        checklist += ["应用恢复场景 runbook", "该 App 最近的相关 Issue（是否反复离线）"]
    elif branch == "mmp":
        checklist += ["MMP 漂移时间线（最近 10 个快照）",
                      "本地执行时间线（若绑定 Job）", "MMP 平台深链（处置在 MMP 侧完成）"]
    elif branch == "governance":
        checklist += ["项目/版本审批上下文（这是工作流审批，不是技术故障）"]
    checklist.append("历史相似已解决 Issue 的处置记录")
    state["branch"] = branch
    state["checklist"] = checklist
    return state


@_with_session
def enrich_job(state: dict, session) -> dict:
    bundle = state["bundle"]
    executions = (session.query(JobExecution).filter(JobExecution.job_id == state["job_id"])
                  .order_by(JobExecution.timestamp.desc()).limit(10).all())
    bundle["recent_executions"] = [
        {"status": e.status, "at": _iso(e.timestamp)} for e in executions
    ]
    job = session.query(Job).filter(Job.id == state["job_id"]).first()
    bundle["cml_recent_runs"] = _live_cml_runs(job) if job else []
    return state


@_with_session
def enrich_app(state: dict, session) -> dict:
    bundle = state["bundle"]
    rows = (session.query(Issue)
            .filter(Issue.app_id == state["app_id"], Issue.id != state["issue_id"])
            .order_by(Issue.created_at.desc()).limit(5).all())
    bundle["recent_app_issues"] = [{
        "issue_id": r.id, "type": r.type, "status": r.status,
        "handling_state": classify_handling_state(r)["state"],
        "created_at": _iso(r.created_at),
        "resolution": (r.resolution_description or "")[:300],
    } for r in rows]
    return state


@_with_session
def enrich_mmp(state: dict, session) -> dict:
    bundle = state["bundle"]
    if state.get("job_id"):
        snaps = (session.query(MmpDriftSnapshot).filter(MmpDriftSnapshot.job_id == state["job_id"])
                 .order_by(MmpDriftSnapshot.observed_at.desc()).limit(10).all())
        bundle["drift_timeline"] = [
            {"drifted": bool(s.drifted), "at": _iso(s.observed_at),
             "details": (s.drift_details or "")[:500]}
            for s in snaps
        ]
        executions = (session.query(JobExecution).filter(JobExecution.job_id == state["job_id"])
                      .order_by(JobExecution.timestamp.desc()).limit(10).all())
        bundle["recent_executions"] = [
            {"status": e.status, "at": _iso(e.timestamp)} for e in executions
        ]
    return state


@_with_session
def enrich_governance(state: dict, session) -> dict:
    issue = session.query(Issue).filter(Issue.id == state["issue_id"]).first()
    if issue is not None and issue.project_id:
        project = session.query(Project).filter(Project.id == issue.project_id).first()
        if project is not None:
            state["bundle"]["governance"] = {
                "project": project.name, "project_status": project.status,
                "lifecycle_status": project.lifecycle_status,
                "note": "审批类 Issue：处置 = 审阅版本内容后在页面上 approve/reject，不是技术排查。",
            }
    return state


def _live_cml_runs(job) -> list:
    """Best-effort: last few real CML runs (status + failure reason — the
    control API has no log endpoint). Any failure → empty list."""
    if not (job.cml_project_id and job.cml_job_id):
        return []
    try:
        from core.services.cml_binding_resolver import build_control_interface

        runs = build_control_interface(timeout=10).list_job_runs(
            job.cml_project_id, job.cml_job_id, limit=5
        )
        return [{
            "status": r.get("status", ""), "created_at": r.get("created_at", ""),
            "finished_at": r.get("finished_at", ""),
            "failure": (str(r.get("failure_reason") or r.get("kill_reason") or ""))[:300],
        } for r in runs]
    except Exception as exc:
        logger.warning("diagnose: live CML run fetch skipped for job {}: {}", job.id, exc)
        return []


@_with_session
def retrieve_history(state: dict, session) -> dict:
    """Similar resolved issues. Prefers the FTS retrieval module when its
    index is available; falls back to structured-filter ranking."""
    issue = session.query(Issue).filter(Issue.id == state["issue_id"]).first()
    rows: list = []
    try:
        from core.agent import retrieval

        rows = retrieval.retrieve_similar(session, issue, limit=5)
    except Exception:
        rows = []
    if not rows:
        rows = _similar_issues(session, issue)
    state["bundle"]["similar_resolved_issues"] = rows
    return state


def _similar_issues(session, issue: Issue) -> list:
    q = session.query(Issue).filter(
        Issue.id != issue.id,
        Issue.status.in_(("resolved", "closed")),
        Issue.resolution_description.isnot(None),
        Issue.resolution_description != "",
    )
    if issue.job_id:
        scoped = q.filter(Issue.job_id == issue.job_id)
    elif issue.app_id:
        scoped = q.filter(Issue.app_id == issue.app_id)
    else:
        scoped = q.filter(Issue.type == issue.type)
    rows = scoped.order_by(Issue.resolved_at.desc()).limit(5).all()
    if len(rows) < 3:  # widen to same-type history when entity history is thin
        seen = {r.id for r in rows}
        rows += [r for r in q.filter(Issue.type == issue.type)
                 .order_by(Issue.resolved_at.desc()).limit(5).all() if r.id not in seen][: 5 - len(rows)]
    return [{
        "issue_id": r.id, "type": r.type, "title": r.title,
        "resolved_at": _iso(r.resolved_at),
        "resolution": (r.resolution_description or "")[:1000],
        "match_reason": "同一 Job/App 或同类型的历史已解决工单",
    } for r in rows]


def apply_budget(state: dict) -> dict:
    """Per-section budgets instead of one blob truncation. Lists are
    newest-first, so trimming the tail drops the oldest entries."""
    bundle = state["bundle"]
    truncated: list[str] = []
    for section, budget in SECTION_BUDGETS.items():
        items = bundle.get(section)
        if not isinstance(items, list):
            continue
        while items and len(json.dumps(items, ensure_ascii=False)) > budget:
            # Matched runbook scenarios are the highest-value evidence — drop
            # unmatched ones first when the scenarios section blows its budget.
            if section == "scenarios":
                idx = next((i for i in range(len(items) - 1, -1, -1)
                            if not items[i].get("matched")), len(items) - 1)
                items.pop(idx)
            else:
                items.pop()
            if section not in truncated:
                truncated.append(section)
    if truncated:
        bundle["_truncated_sections"] = truncated
    return state


def analyze(state: dict) -> dict:
    from core.agent.diagnose import run_analysis

    state["report"] = run_analysis(
        state["bundle"],
        issue_type=state["issue_type"],
        checklist=state["checklist"],
        violations=state.get("violations") or [],
        model=state.get("model"),
    )
    return state


# ── self check (the hard guarantees) ──────────────────────────────────


def _allowed_contacts(bundle: dict) -> set:
    contacts = set()
    for s in bundle.get("scenarios") or []:
        if s.get("escalation_target"):
            contacts.add(s["escalation_target"].strip())
    for key in ("job", "app"):
        contact = (bundle.get(key) or {}).get("owner_contact", "")
        if contact:
            contacts.add(contact.strip())
    return contacts


_TOKEN_RE = re.compile(r"[A-Za-z_][\w\-.]{2,}|#?\d{2,}")


def _evidence_grounded(evidence: str, bundle_text: str) -> bool:
    """Verifiable tokens (ids, job names, dates) cited in the evidence must
    exist in the bundle. Pure-prose evidence stays (nothing to verify)."""
    tokens = _TOKEN_RE.findall(evidence or "")
    if not tokens:
        return True
    return any(t.lstrip("#") in bundle_text for t in tokens)


def self_check(state: dict) -> dict:
    report = state["report"]
    bundle = state["bundle"]
    bundle_text = json.dumps(bundle, ensure_ascii=False)
    contacts = _allowed_contacts(bundle)
    violations: list[str] = []
    warnings: list[str] = state.setdefault("warnings", [])

    if report.escalation_needed:
        target = (report.escalation_target or "").strip()
        if target not in contacts:
            violations.append(
                f"escalation_target {target!r} 不在证据包的 escalation_target/owner_contact 里"
                f"（可选值：{sorted(contacts) or '无'}）")
    if report.email_draft is not None:
        if not report.escalation_needed:
            violations.append("escalation_needed=false 时不应有 email_draft")
        elif (report.email_draft.to or "").strip() not in contacts:
            violations.append(f"email_draft.to {report.email_draft.to!r} 不在证据包联系人里")

    ungrounded = [rc for rc in report.root_causes
                  if not _evidence_grounded(rc.evidence, bundle_text)]
    if ungrounded:
        violations.append(
            "以下根因的 evidence 引用了证据包里不存在的实体: "
            + "; ".join(rc.hypothesis[:50] for rc in ungrounded))

    if violations and not state.get("retried"):
        # One shot back through analyze with the violation list in-prompt.
        state["violations"] = violations
        state["retried"] = True
        state["needs_retry"] = True
        return state

    state["needs_retry"] = False
    if violations:
        # Retry didn't fix it (or no retry budget) — enforce in code.
        if report.escalation_needed and (report.escalation_target or "").strip() not in contacts:
            warnings.append(f"已移除未经证据支持的升级对象 {report.escalation_target!r}")
            report.escalation_needed = False
            report.escalation_target = ""
            report.email_draft = None
        if report.email_draft is not None and (
            not report.escalation_needed
            or (report.email_draft.to or "").strip() not in contacts
        ):
            warnings.append("已移除收件人未经证据支持的升级邮件草稿")
            report.email_draft = None
        kept = [rc for rc in report.root_causes if _evidence_grounded(rc.evidence, bundle_text)]
        if len(kept) != len(report.root_causes):
            warnings.append("已删除证据不在证据包内的根因假设")
            report.root_causes = kept
    return state


def _bundle_artifacts(bundle: dict) -> list:
    """Render the collected timelines as table artifacts (same schema as the
    chat `artifact` events — the frontend reuses one renderer + export).
    Deterministic server-side data; the LLM never touches these."""
    import uuid

    from core.agent.artifacts import Artifact, ChartSeries

    specs = [
        ("执行时间线（监控记录，最新在前）", "recent_executions",
         [("at", "时间"), ("status", "状态")]),
        ("CML 实时 run 历史", "cml_recent_runs",
         [("created_at", "开始"), ("finished_at", "结束"), ("status", "状态"), ("failure", "失败原因")]),
        ("MMP 漂移时间线", "drift_timeline",
         [("at", "观测时间"), ("drifted", "漂移"), ("details", "明细")]),
        ("该 App 最近的相关 Issue", "recent_app_issues",
         [("issue_id", "Issue"), ("type", "类型"), ("handling_state", "处置状态"),
          ("created_at", "创建时间"), ("resolution", "处置记录")]),
        ("历史相似已解决 Issue", "similar_resolved_issues",
         [("issue_id", "Issue"), ("title", "标题"), ("resolved_at", "解决时间"),
          ("match_reason", "相似原因")]),
    ]
    out = []
    for title, section, columns in specs:
        rows = bundle.get(section)
        if not isinstance(rows, list) or not rows:
            continue
        out.append(Artifact(
            id=uuid.uuid4().hex[:12], kind="table", title=title,
            columns=[ChartSeries(key=k, label=v) for k, v in columns],
            rows=rows, source={"dataset": f"diagnose:{section}", "params": {}},
        ).model_dump())
    return out


def finalize(state: dict) -> dict:
    payload = state["report"].model_dump()
    payload["handling_state"] = state["bundle"]["issue"].get("handling_state", "")
    payload["evidence_checklist"] = state["checklist"]
    payload["warnings"] = state.get("warnings", [])
    payload["artifacts"] = _bundle_artifacts(state["bundle"])
    if state["bundle"].get("_truncated_sections"):
        payload["warnings"] = payload["warnings"] + [
            f"证据包以下部分因超预算被裁剪（保留最新）：{state['bundle']['_truncated_sections']}"]
    state["payload"] = payload
    return state


# ── pipeline assembly ─────────────────────────────────────────────────

_ENRICH_NODES: dict[str, Callable] = {
    "job": enrich_job,
    "app": enrich_app,
    "mmp": enrich_mmp,
    "governance": enrich_governance,
}

# (node, stage label) — the stage stream the SSE contract exposes.
NODE_STAGES = [
    (collect_base, "collecting"),
    (triage, "collecting"),
    ("enrich", "enriching"),
    (retrieve_history, "retrieving"),
    (apply_budget, "retrieving"),
    (analyze, "analyzing"),
    (self_check, "verifying"),
    (finalize, "verifying"),
]


def collect_evidence(actor: CurrentUser, issue_id: int, *, model=None) -> dict:
    """Run the grounding stages only (collect → triage → enrich → history →
    budget) and return the state, without the LLM analyze/self-check/finalize.
    Reused by the concise diagnose (chat/FAB) so its brief is grounded in the
    same evidence the full structured pipeline uses. Raises NotFound/Forbidden
    via ``collect_base``'s access check."""
    state: dict = {"actor": actor, "issue_id": issue_id, "model": model}
    state = collect_base(state)
    state = triage(state)
    enrich = _ENRICH_NODES.get(state["branch"])
    if enrich is not None:
        state = enrich(state)
    state = retrieve_history(state)
    state = apply_budget(state)
    return state


def run_pipeline(actor: CurrentUser, issue_id: int, *, model=None) -> Iterator[dict]:
    """Yields ``{"stage": ...}`` progress dicts, then ``{"payload": report}``.

    Same node functions whether langgraph is present or not; the sequential
    runner below *is* the graph (linear + one fan-out + one retry edge), so a
    StateGraph would add a dependency without adding behavior — revisit when
    the diagnosis flow gains real parallel branches.
    """
    state: dict = {"actor": actor, "issue_id": issue_id, "model": model}
    last_stage = ""
    for node, stage in NODE_STAGES:
        if stage != last_stage:
            last_stage = stage
            yield {"stage": stage}
        if node == "enrich":
            enrich = _ENRICH_NODES.get(state["branch"])
            if enrich is not None:
                state = enrich(state)
            continue
        state = node(state)
        if node is self_check and state.pop("needs_retry", False):
            yield {"stage": "analyzing"}
            state = analyze(state)
            state = self_check(state)
            last_stage = "verifying"
    yield {"payload": state["payload"]}
