"""Write tools (propose-only) for the assistant.

Each ``draft_*`` function is a plain ``(session, actor, ...) -> dict`` that does
a **read-only preview** and returns a :class:`WriteProposal` dump (a diff to be
confirmed) — it never mutates a row. The deterministic commit happens later in
the confirm endpoint. RBAC is pre-checked here (tool layer) and re-checked by
the service on commit (double gate).

The tool pool is the security boundary: only T1 operations are registered as
tools, so the agent physically cannot propose T2/T3 changes. S0 ships the
issue-action tools; job_sla / edit_* arrive in later phases.
"""
from __future__ import annotations

from typing import Optional

from core.auth.jwt import CurrentUser
from core.config import get_config
from core.models.constants import IssueStatus, IssueType
from core.models.database import get_db
from core.models.entities import Issue
from core.models.user import is_elevated_role
from core.agent.write_schemas import FieldChange, WriteProposal

_TERMINAL = (IssueStatus.RESOLVED, IssueStatus.CLOSED, IssueStatus.FALSE_POSITIVE)


def _can_act_on_issue(actor: CurrentUser, issue: Issue) -> bool:
    """Same rule as ``issue_service.run_action`` / the issues router: the
    assignee, or the elevated tier (admin / relayops_member), or a platform owner."""
    if is_elevated_role(actor.role):
        return True
    if actor.username in get_config().platform_owners:
        return True
    return issue.assignee_id == actor.user_id


# Active (non-terminal) statuses the generic status tool may transition between;
# terminal transitions go through the dedicated resolve / false_positive tools.
_ACTIVE_STATUSES = (IssueStatus.OPEN, IssueStatus.IN_PROGRESS)


def _validate_issue_for_write(session, actor: CurrentUser, issue_id: int):
    """Shared preconditions for the issue write tools. Returns ``(issue, None)``
    or ``(None, error_dict)``. Read-only — mutates nothing."""
    issue = session.query(Issue).filter(Issue.id == issue_id).first()
    if issue is None:
        return None, {"error": f"Issue #{issue_id} not found"}
    if issue.type == IssueType.HANDOVER_REVIEW:
        return None, {"error": "Handover-review issues are handled via approve/reject, not here"}
    if not _can_act_on_issue(actor, issue):
        return None, {"error": "You are not allowed to change this issue", "rbac_ok": False}
    if issue.status in _TERMINAL:
        return None, {"error": f"Issue #{issue_id} is already in a terminal state ({issue.status})"}
    return issue, None


def _resolution_proposal(issue_id: int, old_status: str, new_status: str, title: str,
                         resolution_description: str, impact: str) -> dict:
    """Build a status-change proposal that requires a resolution note, or a
    clarification when the note is missing."""
    if not (resolution_description or "").strip():
        return {"clarification": "A short reason is required.", "field": "resolution_description"}
    return WriteProposal(
        kind="false_positive" if new_status == IssueStatus.FALSE_POSITIVE else "resolve_issue",
        entity_type="issue",
        entity_id=issue_id,
        title=title,
        changes=[FieldChange(field_path="status", label="Status", old_value=str(old_status),
                             new_value=new_status, rationale=resolution_description.strip())],
        impact_note=impact,
        requires_resolution=True,
        commit_path="direct",
    ).model_dump()


def draft_false_positive(session, actor: CurrentUser, issue_id: int, resolution_description: str) -> dict:
    """Read-only preview: propose flipping an issue to ``false_positive``."""
    issue, err = _validate_issue_for_write(session, actor, issue_id)
    if err:
        return err
    return _resolution_proposal(
        issue_id, issue.status, IssueStatus.FALSE_POSITIVE,
        f"Mark Issue #{issue_id} as false positive", resolution_description,
        "Closes the issue as a false positive; affects future SLA / stale judgments.",
    )


def draft_resolve_issue(session, actor: CurrentUser, issue_id: int, resolution_description: str) -> dict:
    """Read-only preview: propose resolving an issue (real fix applied)."""
    issue, err = _validate_issue_for_write(session, actor, issue_id)
    if err:
        return err
    return _resolution_proposal(
        issue_id, issue.status, IssueStatus.RESOLVED,
        f"Resolve Issue #{issue_id}", resolution_description,
        "Marks the issue resolved; counts as a manual intervention in analytics.",
    )


def draft_update_issue_status(session, actor: CurrentUser, issue_id: int, new_status: str) -> dict:
    """Read-only preview: propose moving an issue between active states
    (open ↔ in_progress). Terminal transitions go through resolve/false_positive."""
    issue, err = _validate_issue_for_write(session, actor, issue_id)
    if err:
        return err
    if new_status not in _ACTIVE_STATUSES:
        return {
            "error": "Use draft_resolve_issue / draft_false_positive to close an issue.",
            "valid_values": list(_ACTIVE_STATUSES),
        }
    if new_status == issue.status:
        return {"error": f"Issue #{issue_id} is already {new_status} (no change)."}
    return WriteProposal(
        kind="update_issue_status", entity_type="issue", entity_id=issue_id,
        title=f"Set Issue #{issue_id} status to {new_status}",
        changes=[FieldChange(field_path="status", label="Status", old_value=str(issue.status),
                             new_value=new_status, rationale="")],
        impact_note="", requires_resolution=False, commit_path="direct",
    ).model_dump()


def draft_record_step(session, actor: CurrentUser, issue_id: int, step_note: str) -> dict:
    """Read-only preview: propose recording a handling step on an issue
    (also moves it to in_progress on commit)."""
    issue, err = _validate_issue_for_write(session, actor, issue_id)
    if err:
        return err
    if not (step_note or "").strip():
        return {"clarification": "What step do you want to record?", "field": "step_note"}
    return WriteProposal(
        kind="record_step", entity_type="issue", entity_id=issue_id,
        title=f"Record a handling step on Issue #{issue_id}",
        changes=[FieldChange(field_path="action_summary.steps", label="Step",
                             old_value="", new_value=step_note.strip(), rationale="")],
        impact_note="Appends a step to the issue's handling log.",
        requires_resolution=False, commit_path="direct",
    ).model_dump()


def draft_job_sla(session, actor: CurrentUser, job_id: int, *, this_alert_age_minutes=None) -> dict:
    """Read-only preview: propose adjusting a job's cron / staleness threshold
    based on its REAL execution cadence (the false-positive → SLA red line).

    All numbers come from ``sla_advisor.recommend_sla`` (deterministic); this
    only packages them into a proposal. ``commit_path='version_flow'`` because
    job edits go through the project draft/version flow — not effective until
    submitted/approved (and blocked while a handover is under review)."""
    from core.exceptions import ForbiddenError, NotFoundError, SystemLockedError
    from core.models.entities import Job
    from core.services import job_service
    from core.agent.sla_advisor import recommend_sla

    job = session.get(Job, job_id)
    if job is None:
        return {"error": f"Job #{job_id} not found"}
    # Reuse the exact write gate (read-only here); service re-checks on commit.
    try:
        job_service._check_product_access(session, product_id=job.product_id, actor=actor, require_owner=True)
    except ForbiddenError:
        return {"error": "You are not allowed to edit this job's schedule/SLA", "rbac_ok": False}
    except (NotFoundError, SystemLockedError) as exc:
        return {"error": str(exc)}

    rec = recommend_sla(session, job_id, this_alert_age_minutes=this_alert_age_minutes)
    rationale = f"{rec.method}; 样本数={rec.sample_run_count}; 实测间隔中位数≈{rec.observed_interval_minutes:.0f}min"
    changes = []
    if rec.recommended_threshold_minutes is not None:
        changes.append(FieldChange(
            field_path="sla_custom_minutes", label="SLA 阈值(分钟)",
            old_value=str(rec.current_threshold_minutes),
            new_value=str(rec.recommended_threshold_minutes), rationale=rationale))
    if rec.recommended_cron:
        changes.append(FieldChange(
            field_path="schedule_cron", label="排程 cron",
            old_value=rec.current_cron or "", new_value=rec.recommended_cron, rationale=rationale))

    if not changes:
        note = "；".join(rec.warnings) or "按真实节奏，当前阈值已合适，无需调整。"
        return {
            "note": note,
            "observed_interval_minutes": rec.observed_interval_minutes,
            "sample_run_count": rec.sample_run_count,
        }

    return WriteProposal(
        kind="job_sla", entity_type="job", entity_id=job_id,
        title=f"调整 Job #{job_id} 的 SLA/排程",
        changes=changes,
        impact_note="改的是 Ops 库判定参数（不碰外部调度器）；提交进项目版本流，"
                    "可能需后续提交/审批，非即时生效。",
        requires_resolution=False, commit_path="version_flow",
    ).model_dump()


# ── D: user-specified cron / staleness threshold (RELIABILITY_PLAN §12 D) ──
#
# draft_job_sla above only proposes the AUTO-recomputed values; these accept the
# value the USER asks for ("shift cron 4h later", "tighten staleness to 5h").
# Both reuse kind="job_sla" → the existing commit dispatch maps schedule_cron /
# sla_custom_minutes (agent_actions._commit_proposal), so no commit/schema change.

_MIN_STALE_THRESHOLD_MINUTES = 5  # mirrors staleness._CRON_MIN_THRESHOLD_MINUTES


def _job_for_write(session, actor: CurrentUser, job_id: int):
    """Load job + the same RBAC gate as draft_job_sla. (job, None) or (None, err)."""
    from core.exceptions import ForbiddenError, NotFoundError, SystemLockedError
    from core.models.entities import Job
    from core.services import job_service

    job = session.get(Job, job_id)
    if job is None:
        return None, {"error": f"Job #{job_id} not found"}
    try:
        job_service._check_product_access(session, product_id=job.product_id,
                                          actor=actor, require_owner=True)
    except ForbiddenError:
        return None, {"error": "You are not allowed to edit this job's schedule/SLA", "rbac_ok": False}
    except (NotFoundError, SystemLockedError) as exc:
        return None, {"error": str(exc)}
    return job, None


def _cron_is_parseable(cron: str) -> bool:
    """A 5-field cron the platform's own staleness parser accepts (no croniter dep)."""
    from datetime import datetime
    from core.integrations.staleness import next_fire_after
    return next_fire_after(cron, datetime(2026, 1, 1)) is not None


def _natural_interval_for(job) -> Optional[int]:
    cron = (job.schedule_cron or getattr(job, "control_m_cron", "") or "").strip()
    if not cron:
        return None
    from core.agent.tools import _natural_interval_minutes
    return _natural_interval_minutes(cron)


def draft_edit_cron(session, actor: CurrentUser, job_id: int, new_cron: str) -> dict:
    """Read-only preview: propose changing a job's schedule cron to a USER-given
    value (e.g. shift it a few hours later). Reuses kind='job_sla'."""
    job, err = _job_for_write(session, actor, job_id)
    if err:
        return err
    new_cron = (new_cron or "").strip()
    if not new_cron:
        return {"clarification": "想把 cron 改成什么（5 字段表达式，按 SGT）？", "field": "new_cron"}
    if not _cron_is_parseable(new_cron):
        return {"error": f"'{new_cron}' 不是合法的 5 字段 cron 表达式。"}
    old_cron = (job.schedule_cron or "").strip()
    if new_cron == old_cron:
        return {"note": f"cron 已经是 {new_cron}，无需修改。"}
    return WriteProposal(
        kind="job_sla", entity_type="job", entity_id=job_id,
        title=f"修改 Job #{job_id} 的排程 cron",
        changes=[FieldChange(field_path="schedule_cron", label="排程 cron",
                             old_value=old_cron, new_value=new_cron, rationale="用户指定")],
        impact_note="改 Ops 库判定用的排程 cron（按 SGT 解释，不碰外部 Control-M 调度器）；"
                    "进项目版本流，非即时生效。",
        requires_resolution=False, commit_path="version_flow",
    ).model_dump()


def draft_set_sla_threshold(session, actor: CurrentUser, job_id: int, custom_minutes) -> dict:
    """Read-only preview: propose a USER-given custom staleness threshold (minutes)
    for a job (e.g. 300 for "5 hours"). Reuses kind='job_sla'."""
    job, err = _job_for_write(session, actor, job_id)
    if err:
        return err
    try:
        minutes = int(custom_minutes)
    except (TypeError, ValueError):
        return {"error": "custom_minutes 必须是正整数分钟数。"}
    if minutes < _MIN_STALE_THRESHOLD_MINUTES:
        return {"error": f"阈值太小（< {_MIN_STALE_THRESHOLD_MINUTES} 分钟），不接受。"}
    old = getattr(job, "sla_custom_minutes", None)
    if old is not None and int(old) == minutes:
        return {"note": f"自定义阈值已经是 {minutes} 分钟，无需修改。"}
    impact = ("覆盖按 cron×safety_factor 推导的默认 staleness 阈值；超过该分钟数没有新的"
              "成功运行才判 stale。进项目版本流，非即时生效。")
    # Anti-sycophancy guard (RELIABILITY_PLAN §9): a threshold below the job's own
    # run interval will fire constantly — surface that instead of silently obeying.
    interval = _natural_interval_for(job)
    if interval and minutes < interval:
        impact += (f" ⚠️ 该 job 运行间隔约 {interval} 分钟，阈值设为 {minutes} 分钟（小于间隔）"
                   f"会导致它在两次正常运行之间就被判 stale、持续误报，请确认确实需要。")
    return WriteProposal(
        kind="job_sla", entity_type="job", entity_id=job_id,
        title=f"设置 Job #{job_id} 的自定义 staleness 阈值",
        changes=[FieldChange(field_path="sla_custom_minutes", label="自定义 staleness 阈值(分钟)",
                             old_value="" if old is None else str(old),
                             new_value=str(minutes), rationale="用户指定")],
        impact_note=impact, requires_resolution=False, commit_path="version_flow",
    ).model_dump()


# ── S5: iterate write tools (member role + descriptive edits) ────────────
#
# Field whitelists are the boundary (P11): only descriptive fields are editable
# via the agent. owner_id / status / is_system / ownership / bindings are NOT in
# any whitelist, so even if the model proposes them they can never enter a
# proposal — they're dropped before the diff is built.

_EDIT_PROJECT_WHITELIST = ("name", "description", "prod_stat_url")
_EDIT_PRODUCT_WHITELIST = ("name",)
_PROJECT_FIELD_LABELS = {"name": "名称", "description": "描述", "prod_stat_url": "Prod 状态页 URL"}
# Per-project member roles the agent may set. business_owner is invariant-bound
# to Project.owner_id and changes only via owner transfer (a T2 guide op).
_MEMBER_ROLES = ("product_member", "relayops_member")


def _can_edit_project(session, actor: CurrentUser, project) -> bool:
    """Mirror project_service.update_project's gate (P4): elevated tier (admin /
    relayops_member), the project owner, or a project editor (product_member)."""
    from core.services.support_group_service import is_project_editor

    if is_elevated_role(actor.role):
        return True
    if project.owner_id == actor.user_id:
        return True
    return is_project_editor(session, project.id, actor.user_id)


def _whitelisted_changes(proposed: dict, whitelist, labels, current) -> list:
    """Build FieldChange list from ``proposed`` keeping only whitelisted fields
    that actually change. Sensitive/unknown keys are silently dropped (P11)."""
    changes = []
    for key in whitelist:
        if key not in (proposed or {}):
            continue
        new_val = ("" if proposed[key] is None else str(proposed[key])).strip()
        old_val = "" if current.get(key) is None else str(current.get(key))
        if new_val == old_val:
            continue
        changes.append(FieldChange(field_path=key, label=labels.get(key, key),
                                   old_value=old_val, new_value=new_val))
    return changes


def draft_edit_project(session, actor: CurrentUser, project_id: int, fields: dict) -> dict:
    """Read-only preview: propose editing a project's descriptive fields
    (name/description/prod_stat_url only). Sensitive fields are dropped (P11)."""
    from core.models.entities import Project

    project = session.get(Project, project_id)
    if project is None:
        return {"error": f"Project #{project_id} not found"}
    if getattr(project, "is_system", 0) == 1:
        return {"error": "System-managed project is read-only"}
    if not _can_edit_project(session, actor, project):
        return {"error": "You are not allowed to edit this project", "rbac_ok": False}
    current = {"name": project.name, "description": project.description,
               "prod_stat_url": getattr(project, "prod_stat_url", "")}
    changes = _whitelisted_changes(fields, _EDIT_PROJECT_WHITELIST, _PROJECT_FIELD_LABELS, current)
    if not changes:
        return {"note": "没有可改动的白名单字段（仅支持 名称/描述/Prod 状态页 URL）。"}
    return WriteProposal(
        kind="edit_project", entity_type="project", entity_id=project_id,
        title=f"编辑 Project #{project_id}", changes=changes,
        impact_note="仅改描述类字段；不涉及 owner/状态/绑定。", commit_path="direct",
    ).model_dump()


def draft_edit_product(session, actor: CurrentUser, product_id: int, name: str) -> dict:
    """Read-only preview: propose renaming a product. Goes through the project
    version flow on commit (so commit_path='version_flow')."""
    from core.exceptions import ForbiddenError, NotFoundError, SystemLockedError
    from core.models.entities import Product
    from core.services import product_service

    product = session.get(Product, product_id)
    if product is None:
        return {"error": f"Product #{product_id} not found"}
    if getattr(product, "is_system", 0) == 1:
        return {"error": "System-managed product is read-only"}
    try:
        product_service._check_project_access(session, project_id=product.project_id,
                                              actor=actor, require_owner=True)
    except ForbiddenError:
        return {"error": "You are not allowed to edit this product", "rbac_ok": False}
    except (NotFoundError, SystemLockedError) as exc:
        return {"error": str(exc)}
    changes = _whitelisted_changes({"name": name}, _EDIT_PRODUCT_WHITELIST,
                                   {"name": "名称"}, {"name": product.name})
    if not changes:
        return {"note": "名称没有变化，无需修改。"}
    return WriteProposal(
        kind="edit_product", entity_type="product", entity_id=product_id,
        title=f"重命名 Product #{product_id}", changes=changes,
        impact_note="改产品名属项目范围变更，进版本流，非即时生效。",
        commit_path="version_flow",
    ).model_dump()


def draft_set_member_role(session, actor: CurrentUser, project_id: int, user_id: int,
                          new_role: str) -> dict:
    """Read-only preview: propose changing a project member's per-project role
    between product_member ↔ relayops_member. Never touches business_owner (P11)."""
    from core.models.entities import Project, ProjectMember

    if new_role not in _MEMBER_ROLES:
        return {"error": f"Unsupported member role: {new_role!r}", "valid_values": list(_MEMBER_ROLES)}
    project = session.get(Project, project_id)
    if project is None:
        return {"error": f"Project #{project_id} not found"}
    if getattr(project, "is_system", 0) == 1:
        return {"error": "System-managed project is read-only"}
    # Member management is NOT part of the relayops_member elevation (Phase R kept it
    # owner/admin-only); mirror that exact gate (P4): platform admin, the
    # project owner, or the member themselves.
    is_admin = actor.role == "admin" or actor.username in get_config().platform_owners
    if not (is_admin or project.owner_id == actor.user_id or user_id == actor.user_id):
        return {"error": "You are not allowed to change member roles in this project", "rbac_ok": False}
    pm = (session.query(ProjectMember)
          .filter(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
          .first())
    if pm is None:
        return {"error": f"User #{user_id} is not a member of project #{project_id}"}
    if pm.role == "business_owner":
        return {"error": "Cannot change the business owner's role; transfer ownership instead (guide-only)."}
    if pm.role == new_role:
        return {"error": f"Member is already {new_role} (no change)."}
    return WriteProposal(
        kind="set_member_role", entity_type="project", entity_id=project_id,
        title=f"设置 Project #{project_id} 成员 #{user_id} 的角色",
        changes=[FieldChange(field_path=f"members.{user_id}.role", label="项目角色",
                             old_value=str(pm.role), new_value=new_role, rationale="")],
        impact_note="仅在本项目内生效，可逆。", commit_path="direct",
    ).model_dump()


def build_write_tools(actor: CurrentUser, *, proposal_sink: list) -> list:
    """Wrap the T1 write tools as langchain tools bound to ``actor``.

    A tool that produces a proposal pushes it onto ``proposal_sink`` (the write
    turn drains it → persists a token → emits the ``write_proposal`` event) and
    returns a compact summary to the model. Errors/clarifications are returned
    to the model as-is (no proposal produced).

    Tool pool = the security boundary: only T1 issue-action tools are here, so
    the agent cannot propose T2/T3 changes (owner transfer, delete, approval,
    new-entity creation, admin/global ops) — they are simply not registered."""
    from langchain_core.tools import tool

    def _propose(fn, /, **params) -> dict:
        session = get_db().get_session()
        try:
            result = fn(session, actor, **params)
        finally:
            session.close()
        if isinstance(result, dict) and result.get("kind"):
            proposal_sink.append(result)
            return {
                "proposed": True,
                "title": result.get("title", ""),
                "note": "A confirmation card was shown to the user; await their confirmation.",
            }
        return result  # error / clarification — surface to the model verbatim

    @tool
    def draft_false_positive_tool(issue_id: int, resolution_description: str) -> dict:
        """Propose marking an issue as a FALSE POSITIVE (not a real failure).

        Use when the user says an alert/issue is a false positive. ``issue_id``
        is the numeric id (e.g. from "#123"); ``resolution_description`` is the
        user's short reason. Only PROPOSES — does not close anything itself."""
        return _propose(draft_false_positive, issue_id=issue_id, resolution_description=resolution_description)

    @tool
    def draft_resolve_issue_tool(issue_id: int, resolution_description: str) -> dict:
        """Propose RESOLVING an issue (a real fix was applied). ``issue_id`` is
        the numeric id; ``resolution_description`` is how it was fixed. Only
        PROPOSES — the user confirms before anything is committed."""
        return _propose(draft_resolve_issue, issue_id=issue_id, resolution_description=resolution_description)

    @tool
    def draft_update_issue_status_tool(issue_id: int, new_status: str) -> dict:
        """Propose moving an issue between ACTIVE states (open ↔ in_progress).
        For closing an issue use the resolve / false-positive tools instead.
        ``new_status`` must be 'open' or 'in_progress'."""
        return _propose(draft_update_issue_status, issue_id=issue_id, new_status=new_status)

    @tool
    def draft_record_step_tool(issue_id: int, step_note: str) -> dict:
        """Propose recording a handling step on an issue (also moves it to
        in_progress). ``step_note`` is what was done. Only PROPOSES."""
        return _propose(draft_record_step, issue_id=issue_id, step_note=step_note)

    @tool
    def draft_job_sla_tool(job_id: int) -> dict:
        """Propose adjusting a JOB's cron / staleness threshold based on its real
        execution cadence — use when a stale alert is judged a false positive and
        the job's actual schedule should be reflected. ``job_id`` is the numeric
        id. All values are computed deterministically from real run history; you
        only relay them. Only PROPOSES — the change goes through the project
        version flow after the user confirms."""
        return _propose(draft_job_sla, job_id=job_id)

    @tool
    def draft_edit_cron_tool(job_id: int, new_cron: str) -> dict:
        """Propose changing a JOB's schedule cron to a SPECIFIC expression the user
        gives (e.g. shift it 3-4h later). ``new_cron`` is a 5-field cron in SGT
        (e.g. '0 18 * * 5'). Use this whenever the user wants a particular new
        schedule — do NOT use draft_job_sla_tool (that one only auto-recomputes
        from history and ignores a user-specified value). Only PROPOSES — the user
        confirms; the change goes through the project version flow."""
        return _propose(draft_edit_cron, job_id=job_id, new_cron=new_cron)

    @tool
    def draft_set_sla_threshold_tool(job_id: int, custom_minutes: int) -> dict:
        """Propose a SPECIFIC custom staleness threshold (minutes) the user gives
        for a job (e.g. 300 for "5 hours"). Use when the user wants to tighten or
        loosen the stale-alert tolerance to a specific value — do NOT use
        draft_job_sla_tool. Only PROPOSES — the user confirms (version flow)."""
        return _propose(draft_set_sla_threshold, job_id=job_id, custom_minutes=custom_minutes)

    @tool
    def draft_edit_project_tool(project_id: int, name: str = "", description: str = "",
                                prod_stat_url: str = "") -> dict:
        """Propose editing a project's DESCRIPTIVE fields (name / description /
        prod_stat_url only). Leave a field empty to leave it unchanged. Cannot
        touch owner/status/bindings. Only PROPOSES — the user confirms."""
        fields = {}
        if name:
            fields["name"] = name
        if description:
            fields["description"] = description
        if prod_stat_url:
            fields["prod_stat_url"] = prod_stat_url
        return _propose(draft_edit_project, project_id=project_id, fields=fields)

    @tool
    def draft_edit_product_tool(product_id: int, name: str) -> dict:
        """Propose RENAMING a product. ``name`` is the new product name. The
        change goes through the project version flow after the user confirms."""
        return _propose(draft_edit_product, product_id=product_id, name=name)

    @tool
    def draft_set_member_role_tool(project_id: int, user_id: int, new_role: str) -> dict:
        """Propose changing a project member's per-project role. ``new_role`` must
        be 'product_member' or 'relayops_member'. Cannot change the business owner.
        Only PROPOSES — the user confirms before anything changes."""
        return _propose(draft_set_member_role, project_id=project_id, user_id=user_id, new_role=new_role)

    return [
        draft_false_positive_tool,
        draft_resolve_issue_tool,
        draft_update_issue_status_tool,
        draft_record_step_tool,
        draft_job_sla_tool,
        draft_edit_cron_tool,
        draft_set_sla_threshold_tool,
        draft_edit_project_tool,
        draft_edit_product_tool,
        draft_set_member_role_tool,
    ]
