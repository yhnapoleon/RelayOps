"""Domain knowledge layer for the Ops agent (plan: AGENT_INTELLIGENCE_PLAN.md §2).

Two deterministic capabilities the LLM must never re-derive on its own:

* the **domain card** — a Chinese-language glossary of every enum the platform
  uses (issue types, statuses, action events, scenario vocabularies, health
  severities, anomaly rules). Generated from ``core.models.constants`` at call
  time so prompt text can never drift from code; the consistency tests fail
  the moment a constant gains a value without a meaning entry here;
* the **handling-state classifier** — collapses ``status`` + assignee +
  ``action_summary_json`` into the real-world处置状态 (claimed? escalated and
  waiting? auto-closed?), which the raw status column alone cannot express.

``ISSUE_SCENARIO_HINTS`` lives here as the canonical issue-type → runbook
scenario-type routing table (diagnose imports it from this module).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from core.models.constants import (
    AUTO_CLOSE_RESOLUTION,
    ApplicationRecoveryScenarioType,
    IssueActionType,
    IssueStatus,
    IssueType,
    JobFailureScenarioType,
)

# Minutes-to-deadline below which an open issue is flagged "SLA 风险".
DEFAULT_SLA_RISK_MINUTES = 30


# ── capability coverage + known gaps (AGENT_RELIABILITY_PLAN.md §3/§5) ──
#
# How the agent can serve a user-facing fact/action in chat:
#   CAP_READ    — a read-only tool answers it directly;
#   CAP_WRITE   — doable via a write-mode draft (needs the user's confirmation);
#   CAP_UI_ONLY — no agent capability; the user must do it in the UI.
# The "怎么 xx" dual-track answer (RELIABILITY_PLAN §6.5) decides "能否在对话完成"
# from THIS table — never from the model's own guess. New user re-ports map here
# first: a missing/CAP_UI_ONLY row means "I can't", a CAP_READ/WRITE row means
# "I can". The golden tests assert every named relayops_* tool actually exists.
CAP_READ = "read"
CAP_WRITE = "write"
CAP_UI_ONLY = "ui_only"

CAPABILITY_MATRIX = {
    "查询项目/产品/资产": (CAP_READ, "relayops_projects_overview / relayops_product_assets"),
    "查询/统计 Issue 与 SLA": (CAP_READ, "relayops_list_issues / relayops_issue_stats / relayops_query"),
    "产品健康/趋势分析": (CAP_READ, "relayops_product_health / relayops_product_health_drilldown"),
    "Job 运行历史": (CAP_READ, "relayops_job_execution_history"),
    "Job staleness 阈值 / SLA 档位": (CAP_READ, "relayops_job_sla_config"),
    "Job 计划-vs-实际运行偏差": (CAP_READ, "relayops_job_schedule_adherence"),
    "关单 / 标记误报 / 改 issue 状态": (CAP_WRITE, "写入模式 draft（需用户确认后落库）"),
    "改 job cron / staleness 阈值": (CAP_WRITE, "写入模式 draft_edit_cron / draft_set_sla_threshold（需确认，进版本流）"),
    "编辑 project/product 描述 / 成员角色": (CAP_WRITE, "写入模式 draft（需用户确认后落库）"),
    "onboarding 建档（项目/产品/job/app）": (CAP_WRITE, "接入向导：上传/粘贴交接文档→抽取→逐条补全"),
    "转让 owner / 删除实体 / 审批 handover / 改全局角色":
        (CAP_UI_ONLY, "高危操作，只能去对应页面手动完成"),
}

# Every CAP_WRITE capability must map to draft tool(s) that actually exist in the
# write pool (test-enforced in test_write_cron_sla) — so the dual-track "能力轨"
# can never promise a tool the pool lacks (RELIABILITY_PLAN §12 D, the cron 空头支票).
WRITE_CAPABILITY_TOOLS = {
    "关单/标记误报": ["draft_resolve_issue_tool", "draft_false_positive_tool"],
    "改 issue 状态 / 记录步骤": ["draft_update_issue_status_tool", "draft_record_step_tool"],
    "改 job cron": ["draft_edit_cron_tool"],
    "改 job staleness 阈值": ["draft_set_sla_threshold_tool", "draft_job_sla_tool"],
    "编辑 project/product 描述": ["draft_edit_project_tool", "draft_edit_product_tool"],
    "改项目成员角色": ["draft_set_member_role_tool"],
}

# Hard boundaries the agent must state plainly rather than work around. The real
# value is the *discipline* (exhaust tools first, then declare the boundary),
# not this static list — see CHAT_SYSTEM "能力边界与反迎合".
KNOWN_GAPS = [
    "写操作本身不由我执行：我只产出待确认草案，确认后系统才落库（见写入模式）",
    "高危操作（转让 owner / 删除 / 审批 handover / 改全局角色）只能在 UI 手动完成",
    "Ops 未建档实体的细节我读不到，除非升级 cml_live_*/mmp_live_* 直查平台",
]


# ── issue-type → runbook scenario-type routing (canonical home) ───────

ISSUE_SCENARIO_HINTS = {
    IssueType.JOB_NOT_TRIGGERED: {"not_triggered", "dependency_failed"},
    IssueType.JOB_FAILED: {"triggered_but_failed", "dependency_failed", "logic_issue", "external_system_issue"},
    IssueType.JOB_STALE: {"not_triggered", "dependency_failed", "external_system_issue"},
    IssueType.APP_OFFLINE: {"offline", "healthcheck_failed", "restart_required", "ray_actor_missing", "deployment_issue"},
    IssueType.MMP_DRIFT: {
        "mmp_drift_detected",
        "mmp_perf_drift",
        "mmp_feature_drift",
        "mmp_data_quality_drift",
        "mmp_no_significant_drift",
    },
    IssueType.MMP_FAIRNESS_RISK: {"mmp_fairness_risk"},
    IssueType.MMP_RUN_PENDING_APPROVAL: {"mmp_run_pending_approval"},
    IssueType.MMP_UNAPPROVED_EXP_RUN: {"mmp_unapproved_exp_run"},
}

# Issue types whose resolution is a platform workflow, NOT a Ops runbook. When
# the issue type is one of these, the correct answer is the canonical note here
# — never go fishing in the job/app runbook for an unrelated scenario to attach
# (the mmp_run_pending_approval → not_triggered misfire is exactly this bug).
NO_RUNBOOK_HANDLING = {
    IssueType.HANDOVER_REVIEW: "Approval workflow: review the handover version on the project page, then approve/reject. No Ops runbook — this is not technical triage.",
    IssueType.SCOPE_CHANGE_REVIEW: "Approval workflow: review the scope change on the page, then approve/reject. No Ops runbook — this is not technical triage.",
    IssueType.MMP_RUN_PENDING_APPROVAL: "MMP governance: open the pending run via the issue's external_url deeplink, review drift/metrics, approve/reject on the MMP platform, then close the issue in Ops. No Ops runbook.",
    IssueType.MMP_PENDING_REVIEW: "MMP governance: review the production run on the MMP platform via the external_url deeplink, then close the issue in Ops. No Ops runbook.",
    IssueType.MMP_UNAPPROVED_EXP_RUN: "MMP governance: check the unapproved experiment run on the MMP platform via the external_url deeplink, handle it, then close the issue in Ops. No Ops runbook.",
}


# ── enum semantics (one entry per constant; tests enforce coverage) ───

ISSUE_TYPE_MEANINGS = {
    IssueType.HANDOVER_REVIEW: "Project handover approval (a workflow, not an alert): the Business Owner submits a project version, awaiting platform-admin approval",
    IssueType.SCOPE_CHANGE_REVIEW: "Scope-change approval (a workflow, not an alert): an asset-scope change on an already-handed-over project, awaiting approval",
    IssueType.JOB_NOT_TRIGGERED: "Job alert: not triggered at its scheduled time (scheduling/dependency problem)",
    IssueType.JOB_FAILED: "Job alert: triggered but the run failed (the most common alert type)",
    IssueType.JOB_STALE: "Job alert: last status was success, but the time since the last run exceeds the SLA threshold (missed run)",
    IssueType.APP_OFFLINE: "App alert: the application is offline or its health check failed",
    IssueType.MMP_DRIFT: "MMP model governance: model-drift umbrella signal (pmetric/fmean/fmissing sub-flags are folded into the issue description, not raised as separate issues)",
    IssueType.MMP_FAIRNESS_RISK: "MMP model governance: fairness-risk flag",
    IssueType.MMP_RUN_PENDING_APPROVAL: "MMP model governance: a run is awaiting approval. Handled on the MMP platform (deeplink jump + one-click close); no Ops runbook",
    IssueType.MMP_PENDING_REVIEW: "MMP model governance: a production run is awaiting user review. Also handled on the MMP platform; no Ops runbook",
    IssueType.MMP_UNAPPROVED_EXP_RUN: "MMP model governance: an unapproved experiment run exists",
}

ISSUE_STATUS_MEANINGS = {
    IssueStatus.OPEN: "not yet being worked on (may be unclaimed)",
    IssueStatus.IN_PROGRESS: "being worked on (someone has started; may also be escalated and awaiting an external reply)",
    IssueStatus.RESOLVED: "resolved (carries a resolution_description record)",
    IssueStatus.CLOSED: "closed (includes monitoring auto-close: when the problem clears on its own the system writes 'Auto-closed: problem resolved')",
    IssueStatus.FALSE_POSITIVE: "false positive: in the stats, failed executions linked to this issue count as success (failure rate/trends are corrected)",
}

ISSUE_ACTION_MEANINGS = {
    IssueActionType.START_WORKING: "start working (status → in_progress)",
    IssueActionType.RECORD_STEP: "record a triage/handling step",
    IssueActionType.VERIFICATION_PASSED: "record that verification passed",
    IssueActionType.ESCALATE: "escalate to an external contact (target recorded in the action), awaiting their handling",
    IssueActionType.RETURN_TO_OWNER: "return to the project owner (status back to open, assignee set to the owner)",
    IssueActionType.RESOLVE: "resolve and close (a final conclusion is required)",
    IssueActionType.FALSE_POSITIVE: "mark as false positive and close (a reason is required)",
}

JOB_SCENARIO_MEANINGS = {
    JobFailureScenarioType.NOT_TRIGGERED: "not-triggered scenario",
    JobFailureScenarioType.TRIGGERED_BUT_FAILED: "triggered-but-failed scenario",
    JobFailureScenarioType.DEPENDENCY_FAILED: "upstream-dependency-failed scenario",
    JobFailureScenarioType.LOGIC_ISSUE: "business-logic issue (usually needs a code change by the project team)",
    JobFailureScenarioType.EXTERNAL_SYSTEM_ISSUE: "external-system issue (usually needs escalation to an external team)",
    JobFailureScenarioType.MMP_DRIFT_DETECTED: "MMP drift (auto-detected)",
    JobFailureScenarioType.MMP_FAIRNESS_RISK: "MMP fairness risk (auto-detected)",
    JobFailureScenarioType.MMP_RUN_PENDING_APPROVAL: "MMP run pending approval (auto-detected)",
    JobFailureScenarioType.MMP_UNAPPROVED_EXP_RUN: "MMP unapproved experiment run (auto-detected)",
    JobFailureScenarioType.MMP_PERF_DRIFT: "MMP performance drift pmetric (manual runbook only, no auto-detection)",
    JobFailureScenarioType.MMP_FEATURE_DRIFT: "MMP feature-mean drift fmean (manual runbook only)",
    JobFailureScenarioType.MMP_DATA_QUALITY_DRIFT: "MMP data-quality drift fmissing (manual runbook only)",
    JobFailureScenarioType.MMP_NO_SIGNIFICANT_DRIFT: "MMP drift reviewed and found within tolerance — benign outcome, close without remediation (manual runbook only)",
    JobFailureScenarioType.PASS_THRESHOLD: "(deprecated, kept for pre-Phase-8 legacy data)",
    JobFailureScenarioType.FAIL_THRESHOLD: "(deprecated, kept for pre-Phase-8 legacy data)",
    JobFailureScenarioType.OTHER: "other",
}

APP_SCENARIO_MEANINGS = {
    ApplicationRecoveryScenarioType.OFFLINE: "application offline",
    ApplicationRecoveryScenarioType.HEALTHCHECK_FAILED: "health check failed",
    ApplicationRecoveryScenarioType.RESTART_REQUIRED: "restart required",
    ApplicationRecoveryScenarioType.RAY_ACTOR_MISSING: "Ray actor missing",
    ApplicationRecoveryScenarioType.DEPLOYMENT_ISSUE: "deployment issue",
    ApplicationRecoveryScenarioType.OTHER: "other",
}

# Product-health severity ladder (analytics layer). Not a constants.py class —
# this module is its canonical home; analytics_service reuses it.
HEALTH_SEVERITIES = ["healthy", "watch", "risk", "anomaly"]

HEALTH_SEVERITY_MEANINGS = {
    "healthy": "healthy (anomaly_score < 25 and no rule triggered)",
    "watch": "watch (anomaly_score ≥ 25, no rule triggered)",
    "risk": "risk (1 anomaly rule triggered, or anomaly_score ≥ 50)",
    "anomaly": "anomaly (≥ 2 rules triggered, or anomaly_score ≥ 70)",
}


# ── handling-state classifier (deterministic; never let the LLM guess) ─

class HandlingState:
    """Derived处置状态 — what status+assignee+actions actually mean."""

    UNCLAIMED = "unclaimed"
    CLAIMED_NOT_STARTED = "claimed_not_started"
    IN_PROGRESS = "in_progress"
    ESCALATED_WAITING = "escalated_waiting"
    RETURNED_TO_OWNER = "returned_to_owner"
    RESOLVED = "resolved"
    CLOSED_AUTO = "closed_auto"
    CLOSED_MANUAL = "closed_manual"
    FALSE_POSITIVE = "false_positive"

    ALL = [
        UNCLAIMED, CLAIMED_NOT_STARTED, IN_PROGRESS, ESCALATED_WAITING,
        RETURNED_TO_OWNER, RESOLVED, CLOSED_AUTO, CLOSED_MANUAL, FALSE_POSITIVE,
    ]

    LABELS = {
        UNCLAIMED: "Unclaimed",
        CLAIMED_NOT_STARTED: "Claimed, not started",
        IN_PROGRESS: "In progress",
        ESCALATED_WAITING: "Escalated, awaiting external",
        RETURNED_TO_OWNER: "Returned to project team",
        RESOLVED: "Resolved",
        CLOSED_AUTO: "Closed (auto)",
        CLOSED_MANUAL: "Closed (manual)",
        FALSE_POSITIVE: "Closed (false positive)",
    }


def _parse_ts(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _latest_event(summary: dict) -> tuple[Optional[str], Optional[datetime]]:
    """(kind, timestamp) of the most recent workspace action recorded in
    action_summary_json. Kinds: started / step / verification / escalation."""
    candidates: list[tuple[str, datetime]] = []
    started = _parse_ts(summary.get("started_at"))
    if started:
        candidates.append(("started", started))
    for kind, key in (("step", "steps"), ("verification", "verifications"), ("escalation", "escalations")):
        for entry in summary.get(key) or []:
            ts = _parse_ts(entry.get("recorded_at")) if isinstance(entry, dict) else None
            if ts:
                candidates.append((kind, ts))
    if not candidates:
        return None, None
    return max(candidates, key=lambda c: c[1])


def classify_handling_state(
    issue,
    *,
    now: Optional[datetime] = None,
    sla_risk_minutes: int = DEFAULT_SLA_RISK_MINUTES,
) -> dict:
    """Collapse an Issue row into its real处置状态.

    Returns ``{"state", "label", "sla_overdue", "sla_at_risk", "detail"}``.
    Pure function over the row (plus ``now``) — see tests for the full matrix.
    """
    now = now or datetime.utcnow()
    status = issue.status
    summary = issue.action_summary_json if isinstance(issue.action_summary_json, dict) else {}
    detail = ""

    if status == IssueStatus.FALSE_POSITIVE:
        state = HandlingState.FALSE_POSITIVE
    elif status == IssueStatus.RESOLVED:
        state = HandlingState.RESOLVED
        if issue.resolution_description:
            detail = (issue.resolution_description or "")[:200]
    elif status == IssueStatus.CLOSED:
        if (issue.resolution_description or "").strip() == AUTO_CLOSE_RESOLUTION:
            state = HandlingState.CLOSED_AUTO
        else:
            state = HandlingState.CLOSED_MANUAL
    elif status == IssueStatus.OPEN:
        if summary.get("returned_to_owner_at"):
            state = HandlingState.RETURNED_TO_OWNER
            detail = summary.get("returned_to_owner_notes") or ""
        elif not issue.assignee_id:
            state = HandlingState.UNCLAIMED
        elif summary.get("started_at"):
            # open + already started shouldn't normally occur; treat as working.
            state = HandlingState.IN_PROGRESS
        else:
            state = HandlingState.CLAIMED_NOT_STARTED
    else:  # IN_PROGRESS (and any unknown status degrades to "working")
        kind, _ts = _latest_event(summary)
        if kind == "escalation":
            state = HandlingState.ESCALATED_WAITING
            escalations = summary.get("escalations") or []
            if escalations and isinstance(escalations[-1], dict):
                detail = f"escalation target: {escalations[-1].get('target', '')}"
        else:
            state = HandlingState.IN_PROGRESS

    sla_overdue = False
    sla_at_risk = False
    if status in (IssueStatus.OPEN, IssueStatus.IN_PROGRESS) and issue.sla_deadline:
        remaining = issue.sla_deadline - now
        if remaining <= timedelta(0):
            sla_overdue = True
        elif remaining <= timedelta(minutes=sla_risk_minutes):
            sla_at_risk = True

    return {
        "state": state,
        "label": HandlingState.LABELS[state],
        "sla_overdue": sla_overdue,
        "sla_at_risk": sla_at_risk,
        "detail": detail,
    }


# ── domain card builders ──────────────────────────────────────────────


def _enum_lines(meanings: dict) -> str:
    return "\n".join(f"  - `{value}`: {text}" for value, text in meanings.items())


def _anomaly_rules_text() -> str:
    """Live thresholds from config — never hardcode numbers into prompts."""
    from core.config import get_config

    cfg = get_config()
    return (
        f"Product-health anomaly rules (triggering any one = not healthy):\n"
        f"  - Rule A high failure rate: ≥ {cfg.analytics_anomaly_min_runs} runs in the period"
        f" and failure rate ≥ {cfg.analytics_anomaly_min_failure_rate_percent}%\n"
        f"  - Rule B repeat failures: max consecutive-failure streak ≥ {cfg.analytics_anomaly_min_repeat_failure_streak}\n"
        f"  - Rule C issue backlog: open issues ≥ {cfg.analytics_anomaly_min_open_issues}"
        f" and a failure within the last {cfg.analytics_anomaly_recent_failure_hours} hours\n"
        f"  - anomaly_score = failure_rate×0.5 + min(100, streak×20)×0.25 + min(100, open_issues×20)×0.25\n"
        f"Severity ladder:\n{_enum_lines(HEALTH_SEVERITY_MEANINGS)}"
    )


def build_domain_card(*, compact: bool = False) -> str:
    """Full glossary (or the compact slice that goes into the chat prompt)."""
    issue_types = _enum_lines(ISSUE_TYPE_MEANINGS)
    statuses = _enum_lines(ISSUE_STATUS_MEANINGS)
    handling = "\n".join(
        f"  - `{s}` ({HandlingState.LABELS[s]})" for s in HandlingState.ALL
    )

    head = (
        f"# RelayOps domain card\n"
        f"\n"
        f"Issue types — {len(IssueType.ALL)} in total:\n{issue_types}\n"
        f"\n"
        f"Issue status field — {len(IssueStatus.ALL)} values:\n{statuses}\n"
        f"\n"
        f"Handling state (handling_state — derived by the system from status + assignee + "
        f"action timeline, returned directly by the tools and more accurate than the status "
        f"field) — {len(HandlingState.ALL)} values:\n{handling}\n"
        f"\n"
        f"SLA: alert-type issues default to a 1-hour **handling** SLA; an empty sla_deadline = "
        f"no SLA tracked; the tools' sla_overdue / sla_at_risk are already computed — just cite them.\n"
        f"\n"
        f"Timezone: all cron schedules are interpreted in **SGT (UTC+8)** (e.g. `0 5 * * 2-5` = "
        f"weekdays 05:00 SGT, not UTC); timestamps returned by tools are UTC. Do not compute "
        f"deviations treating cron as UTC — for punctuality questions always use "
        f"relayops_job_schedule_adherence (the server handles timezone conversion).\n"
        f"\n"
        f"Job staleness threshold ≠ 1 hour: it = expected run interval × safety_factor, presets "
        f"strict(1.0)/normal(1.5, default)/loose(3.0); for a job's threshold/preset use "
        f"relayops_job_sla_config — do not guess.\n"
    )
    if compact:
        return head

    actions = _enum_lines(ISSUE_ACTION_MEANINGS)
    job_scenarios = _enum_lines(JOB_SCENARIO_MEANINGS)
    app_scenarios = _enum_lines(APP_SCENARIO_MEANINGS)
    hints = "\n".join(
        f"  - `{itype}` → {sorted(stypes)}" for itype, stypes in ISSUE_SCENARIO_HINTS.items()
    )
    return (
        head
        + f"\nAction-timeline events — {len(IssueActionType.ALL)} in total:\n{actions}\n"
        + f"\nJob failure scenario types (runbook scenario_type):\n{job_scenarios}\n"
        + f"\nApp recovery scenario types:\n{app_scenarios}\n"
        + f"\nIssue type → preferred matching runbook scenario types:\n{hints}\n"
        + "\n" + _anomaly_rules_text() + "\n"
    )


# ── failure signatures (controlled vocabulary for retrieval labels) ──
# Deterministically extracted at index/query time (substring match, lowered).
# Deliberately small and curated: a wrong label poisons retrieval ranking,
# a missing one only falls back to BM25 text similarity.

FAILURE_SIGNATURES = {
    "kerberos": ("kerberos", "kinit", "krb5"),
    "timeout": ("timeout", "timed out", "超时"),
    "dependency": ("dependency failed", "upstream", "依赖失败", "上游"),
    "oom": ("out of memory", "memoryerror", "oomkilled", " oom", "内存不足"),
    "connection": ("connection refused", "connection reset", "unreachable",
                   "connect failed", "连接失败", "网络不通"),
    "permission": ("permission denied", "access denied", "forbidden",
                   "unauthorized", "权限不足", "无权限"),
    "data_quality": ("schema mismatch", "missing column", "数据缺失", "数据质量"),
    "disk_space": ("no space left", "disk full", "磁盘空间不足", "磁盘已满"),
}


def extract_failure_signatures(text: str) -> list:
    """Controlled-vocabulary labels found in the text (sorted, deduped)."""
    lowered = (text or "").lower()
    return sorted(
        label
        for label, needles in FAILURE_SIGNATURES.items()
        if any(n in lowered for n in needles)
    )


def domain_card_for_issue(issue_type: str) -> str:
    """Diagnosis-prompt slice: this issue type's semantics + relevant vocab."""
    lines = [
        f"Issue type `{issue_type}`: {ISSUE_TYPE_MEANINGS.get(issue_type, '(unknown type)')}",
        f"\nIssue status semantics:\n{_enum_lines(ISSUE_STATUS_MEANINGS)}",
    ]
    hints = ISSUE_SCENARIO_HINTS.get(issue_type)
    if hints:
        relevant = {
            k: v for k, v in {**JOB_SCENARIO_MEANINGS, **APP_SCENARIO_MEANINGS}.items()
            if k in hints
        }
        lines.append(f"\nRunbook scenarios preferred for this type:\n{_enum_lines(relevant)}")
    return "\n".join(lines)
