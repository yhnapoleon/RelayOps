"""Live CML / MMP gateway tools for the Ops chat assistant.

The Ops database is a periodically-synced snapshot; these tools let the agent
go straight to the platforms when the snapshot can't answer — real-time job
runs with ``failure_reason``, current application status/resources, projects
that aren't onboarded into Ops yet, the MMP model-governance flags.

Boundary (CML/app.md §11, CML/job.md §9): **GET only**. These tools reuse the
same monitor-only clients as the checkers (``ControlInterface`` /
``MmpInterface``); nothing here can create, mutate, trigger or stop anything.

Identity note: live calls run under the Ops *service identity* — the same
identity the Create-Project CML picker and the background checkers use — so
their visibility is platform-wide, not per-user. That matches the existing
``/api/projects/cml/search`` precedent (any logged-in user may browse the
CML directory). When a tool takes a Ops ``job_id``/``app_id``, the Ops-side
entity is still RBAC-checked before its binding is used.

Every function degrades to ``{"error": ...}`` — a CML/MMP outage must read as
"platform unreachable", never as a stack trace or an invented answer.
"""

from __future__ import annotations

from typing import Optional

from core.auth.jwt import CurrentUser
from core.logging import get_logger
from core.models.entities import Application, Job, Product, Project

logger = get_logger(__name__)

_LIVE_TIMEOUT = 10  # seconds; chat turns shouldn't hang on a slow platform
_RUN_STATUS_FILTERS = ["failed", "stopped", "succeeded", "running",
                       "scheduling", "timeout"]


def _control():
    from core.services.cml_binding_resolver import build_control_interface

    return build_control_interface(timeout=_LIVE_TIMEOUT)


def _mmp():
    from core.config import get_config
    from core.integrations.mmp_interface import MmpInterface

    cfg = get_config()
    return MmpInterface(
        base_url=cfg.mmp_base_url,
        bearer_token=cfg.mmp_bearer_token,
        refresh_token=cfg.mmp_refresh_token,
        verify_ssl=cfg.mmp_verify_ssl,
        ca_bundle=cfg.mmp_ca_bundle_path or None,
        timeout=float(min(cfg.mmp_timeout_seconds, _LIVE_TIMEOUT)),
    )


def _cml_error(exc) -> dict:
    return {"error": f"CML 平台不可达或返回错误：{exc}"}


def _mmp_error(exc) -> dict:
    return {"error": f"MMP 平台不可达或返回错误：{exc}"}


def _iso(v) -> str:
    return str(v or "")


# ── Access guardrail ──────────────────────────────────────────────────
#
# Platform-wide live/raw read is a privilege of Ops members and admins. Every
# other user is scoped to the projects they have at least read access to in Ops
# (owned / member / AD-group — the same set the REST layer enforces). A
# non-privileged user may still use the curated live tools, but only against a
# CML project / MMP repo that maps to one of their accessible Ops projects; the
# arbitrary raw passthrough is denied outright (an opaque API path can't be
# reliably mapped back to a project to authorise).


def _is_privileged(actor: CurrentUser) -> bool:
    """True for admins and global Ops members — they get full platform-wide
    read. Everyone else is project-scoped."""
    from core.agent.tools import _is_admin
    from core.models.user import UserRole

    return _is_admin(actor) or actor.role == UserRole.RELAYOPS_MEMBER


def _accessible_project_ids(session, actor: CurrentUser):
    """None = unrestricted (privileged); else the Ops project ids the user can
    read. Delegates to the canonical helper so the rule stays in one place."""
    from core.agent.tools import _accessible_project_ids as _acc

    return _acc(session, actor)


def _can_read_cml_project(session, actor: CurrentUser, cml_project_name: str) -> bool:
    if _is_privileged(actor):
        return True
    pids = _accessible_project_ids(session, actor) or []
    if not pids:
        return False
    return session.query(Project.id).filter(
        Project.cml_project_name == (cml_project_name or "").strip(),
        Project.id.in_(pids),
    ).first() is not None


def _can_read_mmp_repo(session, actor: CurrentUser, repo_name: str) -> bool:
    if _is_privileged(actor):
        return True
    pids = _accessible_project_ids(session, actor) or []
    if not pids:
        return False
    return session.query(Project.id).filter(
        Project.mmp_project_id == (repo_name or "").strip(),
        Project.id.in_(pids),
    ).first() is not None


def _scope_denied(target: str = "该项目") -> dict:
    return {"error": (
        f"无权限：你只能查询自己有读权限的项目；{target}未授予你访问权限。"
        "（全平台实时直读仅限 Ops 成员与管理员）"
    )}


# ── CML: projects ─────────────────────────────────────────────────────


def cml_live_projects(session, actor: CurrentUser, q: str = "") -> dict:
    """Live CML project directory + which of them Ops already onboarded."""
    from core.integrations.control_interface import CmlApiError

    try:
        items, has_more = _control().list_projects_page(
            name_filter=(q or "").strip() or None)
    except CmlApiError as exc:
        return _cml_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning("cml_live_projects failed")
        return _cml_error(exc)

    privileged = _is_privileged(actor)
    accessible_pids = None if privileged else set(_accessible_project_ids(session, actor) or [])
    bound = {
        (p.cml_project_name or "").strip(): p.id
        for p in session.query(Project).all()
        if (p.cml_project_name or "").strip()
        and (privileged or p.id in accessible_pids)
    }
    rows = [{
        "cml_project_name": it["name"],
        "relayops_project_id": bound.get(it["name"]),   # None = 尚未在 Ops 建档
    } for it in items if it.get("name")]
    if not privileged:
        # Non-privileged users only see CML projects mapping to a Ops project
        # they can read; unonboarded / others' projects are hidden.
        rows = [r for r in rows if r["relayops_project_id"] is not None]
    return {
        "rows": rows,
        "has_more": has_more if privileged else False,
        "note": ("relayops_project_id 为空表示该 CML 项目尚未接入 Ops；数据为服务账号实时可见范围。"
                 if privileged else
                 "仅显示你有读权限的项目（全平台目录仅 Ops 成员/管理员可见）。"),
    }


def _resolve_cml_project_id(control, name: str) -> str:
    """name → id with the same exact-match semantics as the binding layer."""
    return control.resolve_project_id(name)


# ── CML: jobs & runs ──────────────────────────────────────────────────


def cml_live_jobs(session, actor: CurrentUser, cml_project_name: str, q: str = "") -> dict:
    """Live job definitions inside one CML project (schedule, paused, resources)."""
    from core.integrations.control_interface import CmlApiError

    if not (cml_project_name or "").strip():
        return {"error": "cml_project_name 必填（可先用 cml_live_projects 查名字）"}
    if not _can_read_cml_project(session, actor, cml_project_name):
        return _scope_denied(f"CML 项目 {cml_project_name!r} ")
    try:
        control = _control()
        project_id = _resolve_cml_project_id(control, cml_project_name.strip())
        jobs = control.list_jobs(project_id, name_filter=(q or "").strip() or None)
    except CmlApiError as exc:
        return _cml_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning("cml_live_jobs failed")
        return _cml_error(exc)

    bound = {
        (j.cml_job_name or "").strip(): j.id
        for j in session.query(Job).all()
        if (j.cml_job_name or "").strip()
    }
    rows = [{
        "cml_job_name": j.get("name") or "",
        "script": j.get("script") or "",
        "schedule": j.get("schedule") or "",
        "paused": bool(j.get("paused")),
        "cpu": j.get("cpu"), "memory": j.get("memory"),
        "timeout": j.get("timeout") or "",
        "updated_at": _iso(j.get("updated_at")),
        "relayops_job_id": bound.get((j.get("name") or "").strip()),
    } for j in jobs[:50]]
    return {"cml_project_name": cml_project_name, "rows": rows,
            "truncated": len(jobs) > 50}


def _binding_for_relayops_job(session, actor: CurrentUser, relayops_job_id: int):
    """(cml_project_name, cml_job_name, error). RBAC-checks the Ops job."""
    from core.agent.tools import _accessible_product_ids

    job = session.query(Job).filter(Job.id == relayops_job_id).first()
    product_ids = _accessible_product_ids(session, actor)
    if job is None or (product_ids is not None and job.product_id not in product_ids):
        return None, None, {"error": "job 不存在或无权限"}
    project_name = (job.cml_project_name or "").strip()
    if not project_name:
        product = session.query(Product).filter(Product.id == job.product_id).first()
        project = session.query(Project).filter(
            Project.id == product.project_id).first() if product else None
        project_name = (project.cml_project_name or "").strip() if project else ""
    job_name = (job.cml_job_name or "").strip()
    if not project_name or not job_name:
        return None, None, {"error": "该 Ops job 未配置 CML 绑定（cml_project_name/cml_job_name）"}
    return project_name, job_name, None


def cml_live_job_runs(
    session,
    actor: CurrentUser,
    relayops_job_id: int = 0,
    cml_project_name: str = "",
    cml_job_name: str = "",
    status: str = "",
    limit: int = 10,
) -> dict:
    """Real-time run history straight from CML, including ``failure_reason``."""
    from core.integrations.control_interface import CmlApiError

    if status and status not in _RUN_STATUS_FILTERS:
        return {"error": f"status 取值非法: {status!r}",
                "valid_values": _RUN_STATUS_FILTERS}
    if relayops_job_id:
        cml_project_name, cml_job_name, err = _binding_for_relayops_job(
            session, actor, relayops_job_id)
        if err:
            return err
    if not (cml_project_name and cml_job_name):
        return {"error": "需要 relayops_job_id，或同时给 cml_project_name + cml_job_name"}
    # relayops_job_id path is already RBAC-checked in _binding_for_relayops_job; the raw
    # project+job name path is not, so scope it for non-privileged users.
    if not relayops_job_id and not _can_read_cml_project(session, actor, cml_project_name):
        return _scope_denied(f"CML 项目 {cml_project_name!r} ")

    limit = max(1, min(int(limit or 10), 25))
    try:
        control = _control()
        project_id = _resolve_cml_project_id(control, cml_project_name)
        job_id = control.resolve_job_id(project_id, cml_job_name)
        runs = control.list_job_runs(project_id, job_id,
                                     status_filter=status or None, limit=limit)
    except CmlApiError as exc:
        return _cml_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning("cml_live_job_runs failed")
        return _cml_error(exc)

    rows = [{
        "run_id": r.get("id") or "",
        "status": r.get("status") or "",
        "created_at": _iso(r.get("created_at")),
        "running_at": _iso(r.get("running_at")),
        "finished_at": _iso(r.get("finished_at")),
        "failure_reason": (str(r.get("failure_reason") or r.get("kill_reason") or ""))[:300],
    } for r in runs]
    return {"cml_project_name": cml_project_name, "cml_job_name": cml_job_name,
            "rows": rows,
            "note": "CML 实时数据（ENGINE_* 为原始状态）；failure_reason 为空时参考状态与时间戳。"}


# ── CML: applications ─────────────────────────────────────────────────


def cml_live_apps(
    session,
    actor: CurrentUser,
    cml_project_name: str = "",
    relayops_app_id: int = 0,
    q: str = "",
) -> dict:
    """Live application status/resources inside one CML project."""
    from core.agent.tools import _accessible_product_ids
    from core.integrations.control_interface import CmlApiError

    name_filter = (q or "").strip() or None
    if relayops_app_id:
        app = session.query(Application).filter(Application.id == relayops_app_id).first()
        product_ids = _accessible_product_ids(session, actor)
        if app is None or (product_ids is not None and app.product_id not in product_ids):
            return {"error": "app 不存在或无权限"}
        name_filter = (app.cml_application_name or "").strip() or name_filter
        if not (cml_project_name or "").strip():
            cml_project_name = (app.cml_project_name or "").strip()
            if not cml_project_name:
                product = session.query(Product).filter(Product.id == app.product_id).first()
                project = session.query(Project).filter(
                    Project.id == product.project_id).first() if product else None
                cml_project_name = (project.cml_project_name or "").strip() if project else ""
    if not (cml_project_name or "").strip():
        return {"error": "需要 cml_project_name（或带 CML 绑定的 relayops_app_id）"}
    # relayops_app_id path is RBAC-checked above; scope the raw project-name path.
    if not relayops_app_id and not _can_read_cml_project(session, actor, cml_project_name):
        return _scope_denied(f"CML 项目 {cml_project_name!r} ")

    try:
        control = _control()
        project_id = _resolve_cml_project_id(control, cml_project_name.strip())
        apps = control.list_applications(project_id, name_filter=name_filter)
    except CmlApiError as exc:
        return _cml_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning("cml_live_apps failed")
        return _cml_error(exc)

    rows = [{
        "cml_application_name": a.get("name") or "",
        "subdomain": a.get("subdomain") or "",
        "status": a.get("status") or "",
        "script": a.get("script") or "",
        "cpu": a.get("cpu"), "memory": a.get("memory"),
        "nvidia_gpu": a.get("nvidia_gpu"),
        "updated_at": _iso(a.get("updated_at")),
    } for a in apps[:50]]
    return {"cml_project_name": cml_project_name, "rows": rows,
            "note": "APPLICATION_* 为 CML 原始状态：RUNNING 运行中 / STOPPED 已停 / FAILED 失败。"}


# ── MMP: directory & model governance ─────────────────────────────────


def mmp_live_projects(session, actor: CurrentUser, q: str = "") -> dict:
    """Live MMP project directory (+ which Ops projects/jobs bind to each)."""
    from core.integrations.mmp_interface import MmpApiError

    iface = _mmp()
    if not iface.is_configured():
        return {"error": "MMP 平台未配置（base_url/bearer_token）"}
    try:
        directory = iface.list_projects_shallow()
    except MmpApiError as exc:
        return _mmp_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning("mmp_live_projects failed")
        return _mmp_error(exc)

    ql = (q or "").strip().lower()
    privileged = _is_privileged(actor)
    accessible_pids = None if privileged else set(_accessible_project_ids(session, actor) or [])
    bound_projects = {
        (p.mmp_project_id or "").strip(): p.id for p in session.query(Project).all()
        if (p.mmp_project_id or "").strip()
        and (privileged or p.id in accessible_pids)
    }
    rows = []
    for repo_name, info in sorted(directory.items()):
        business = info.get("business_name") or ""
        if ql and ql not in repo_name.lower() and ql not in business.lower():
            continue
        relayops_project_id = bound_projects.get(repo_name)
        # Non-privileged users only see MMP repos mapping to a readable Ops project.
        if not privileged and relayops_project_id is None:
            continue
        models = info.get("models") or []
        rows.append({
            "repo_name": repo_name,
            "business_name": business,
            "model_count": len(models),
            "production_models": [m["name"] for m in models if m.get("is_production")],
            "relayops_project_id": relayops_project_id,
        })
    return {"rows": rows[:50], "truncated": len(rows) > 50,
            "note": ("relayops_project_id 为空表示该 MMP 项目未绑定任何 Ops 项目。"
                     if privileged else
                     "仅显示你有读权限的项目（全平台目录仅 Ops 成员/管理员可见）。")}


_ATTENTION_LABELS = {
    "model_drifted": "模型漂移",
    "has_fairness_risk": "公平性风险",
    "run_pending_approval": "run 待审批",
    "run_pending_user_review": "run 待用户 review",
    "has_unapproved_exp_run": "存在未审批实验 run",
}


def mmp_live_project_status(session, actor: CurrentUser, repo_name: str) -> dict:
    """One MMP project's models with attention flags + latest production run
    (drift sub-flags, deployment, approval timestamps)."""
    from core.integrations.mmp_interface import MmpApiError

    if not (repo_name or "").strip():
        return {"error": "repo_name 必填（可先用 mmp_live_projects 查）"}
    if not _can_read_mmp_repo(session, actor, repo_name):
        return _scope_denied(f"MMP 项目 {repo_name!r} ")
    iface = _mmp()
    if not iface.is_configured():
        return {"error": "MMP 平台未配置（base_url/bearer_token）"}
    try:
        directory = iface.list_projects_shallow()
        info = directory.get(repo_name.strip())
        if info is None:
            return {"error": f"MMP 项目 {repo_name!r} 不存在",
                    "hint": "用 mmp_live_projects 搜索正确的 repo_name"}
        payload = iface.get_project(info["id"])
    except MmpApiError as exc:
        return _mmp_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning("mmp_live_project_status failed")
        return _mmp_error(exc)

    models = []
    for m in payload.get("models") or []:
        attention = []
        for key, label in _ATTENTION_LABELS.items():
            flag = (m.get("attention_required") or {}).get(key) or {}
            if flag.get("status"):
                attention.append({"flag": key, "label": label,
                                  "detail": (flag.get("description") or "")[:200]})
        runs = sorted(
            (r for r in m.get("runs") or [] if r.get("is_production_run")),
            key=lambda r: str(r.get("date_created") or ""), reverse=True)
        latest = runs[0] if runs else {}
        models.append({
            "model_name": m.get("model_name") or "",
            "is_production": bool(m.get("is_production")),
            "attention_required": attention,
            "latest_production_run": {
                "date_created": _iso(latest.get("date_created")),
                "is_deployed": bool(latest.get("is_deployed")),
                "drifted": latest.get("drifted"),
                "pmetric_drifted": latest.get("pmetric_drifted"),
                "fmean_drifted": latest.get("fmean_drifted"),
                "fmissing_drifted": latest.get("fmissing_drifted"),
                "has_fairness_risk": latest.get("has_fairness_risk"),
            } if latest else None,
        })

    bound_jobs = [
        {"relayops_job_id": j.id, "cml_job_name": j.cml_job_name or "",
         "mmp_model_id": j.mmp_model_id or ""}
        for j in session.query(Job).filter(Job.mmp_project_id == repo_name.strip()).all()
    ]
    return {
        "repo_name": repo_name.strip(),
        "business_name": payload.get("business_understanding_project_name") or "",
        "models": models,
        "relayops_bound_jobs": bound_jobs,
        "note": "漂移子标志：pmetric=性能指标 / fmean=特征均值 / fmissing=特征缺失率。",
    }


def _annotated_run(run: dict) -> dict:
    """Shallow-copy a raw MMP run and append a human-readable approval_status
    label, keeping the original numeric code intact alongside it."""
    from core.integrations.mmp_interface import describe_approval_status

    r = dict(run)
    r["approval_status_label"] = describe_approval_status(run.get("approval_status"))
    return r


def build_mmp_model_raw_view(
    iface, repo_name: str, model_name: str, *, max_runs: int = 20,
    focus_run_id: Optional[int] = None,
) -> dict:
    """The unsummarised "complete MMP response body" for one model, plus
    annotations — shared by the agent gateway tool and the issue-detail
    drill-down endpoint.

    Returns the raw ``attention_required`` block, the run the alert traces to
    (``focus_run`` — located by ``focus_run_id`` when given, else the latest
    production run), the current latest production run, and the most-recent N
    production runs — every run annotated with its ``approval_status`` meaning.

    When ``focus_run_id`` is supplied we look it up across *all* the model's
    runs (not just the recent window) so an older triggering run still
    resolves; ``focus_run_found`` reports whether it did. Raises
    ``LookupError`` / ``MmpApiError`` (callers translate to an error payload).
    """
    from core.integrations.mmp_interface import approval_status_legend

    model = iface.get_model_raw(repo_name, model_name)
    runs = model.get("runs") or []
    prod_runs = sorted(
        (r for r in runs if isinstance(r, dict) and r.get("is_production_run")),
        key=lambda r: str(r.get("date_created") or ""), reverse=True)
    latest = _annotated_run(prod_runs[0]) if prod_runs else None

    # Trace the issue's triggering run by id (search all runs, any window).
    focus = None
    focus_found: Optional[bool] = None
    if focus_run_id is not None:
        match = next(
            (r for r in runs if isinstance(r, dict) and r.get("id") == focus_run_id),
            None)
        focus_found = match is not None
        focus = _annotated_run(match) if match is not None else None
    if focus is None:  # no id requested, or the run is no longer returned by MMP
        focus = latest
    focus_is_latest = bool(
        latest and focus and focus.get("id") == latest.get("id"))

    capped = max(1, min(int(max_runs or 20), 100))
    return {
        "repo_name": repo_name,
        "model_name": model_name,
        "is_production": bool(model.get("is_production")),
        # Raw, verbatim model-level attention flags.
        "attention_required": model.get("attention_required") or {},
        "signals": model.get("signals") or {},
        # The run this alert traces to (by id), full raw body.
        "focus_run": focus,
        "focus_run_id": focus_run_id,
        "focus_run_found": focus_found,   # None = no id requested
        "focus_run_is_latest": focus_is_latest,
        # The current latest production run, so the UI can show if state moved on.
        "latest_production_run": latest,
        # Recent production runs (raw + annotated), behind a "show more" in the UI.
        "recent_production_runs": [_annotated_run(r) for r in prod_runs[:capped]],
        "total_production_runs": len(prod_runs),
        "truncated": len(prod_runs) > capped,
        # Lifecycle values defined by the public demo protocol.
        "approval_status_legend": approval_status_legend(),
        "note": (
            "Demo approval states: 0 = draft, 1 = pending review, 2 = approved. "
            "Drift is an independent explicit signal."
        ),
    }


# ── Raw API passthrough (read-only GET, any endpoint) ─────────────────
#
# These let the assistant read the *complete* raw JSON of any CML/MMP GET
# endpoint when the curated tools above don't surface a needed field. Same
# principles as the curated live tools: GET only (no mutation), Ops service
# identity (platform-visible range), graceful {"error": ...} degrade. A size
# cap protects the context window — oversized bodies come back as a truncated
# preview with a hint to narrow the path.

_RAW_MAX_CHARS = 50000


def _validate_raw_path(path: str) -> Optional[str]:
    """Guard the passthrough path: a relative API path only, so it can't be
    redirected to another host or traverse out. Returns an error string or None."""
    p = (path or "").strip()
    if not p:
        return "path 必填，例如 /api/projects/189"
    if "://" in p or p.startswith("//"):
        return "path 必须是相对 API 路径（以 / 开头），不能是完整 URL/跨域地址"
    if not p.startswith("/"):
        return "path 必须以 / 开头"
    if ".." in p:
        return "path 不能包含 .."
    return None


def _cap_raw(path: str, data) -> dict:
    """Return the raw body when small enough, else a truncated preview + hint."""
    import json

    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(data)
    if len(text) <= _RAW_MAX_CHARS:
        return {"path": path, "data": data,
                "note": "平台实时原始返回（GET，只读，Ops 服务账号可见范围）。"}
    return {
        "path": path,
        "truncated": True,
        "size_chars": len(text),
        "preview": text[:_RAW_MAX_CHARS],
        "note": (
            f"返回体过大（{len(text)} 字符）已截断到前 {_RAW_MAX_CHARS} 字符。"
            "请用更具体的 path 缩小范围（如带 id 的单资源端点），"
            "或改用 cml_live_*/mmp_live_* 摘要工具。"
        ),
    }


def _raw_privilege_denied() -> dict:
    return {"error": (
        "无权限：原始 API 直读（cml_get_raw / mmp_get_raw）仅限 Ops 成员与管理员。"
        "你可用 cml_live_jobs / cml_live_job_runs / cml_live_apps / "
        "mmp_live_project_status / mmp_live_model_raw 查询你有读权限的项目。"
    )}


def cml_get_raw(session, actor: CurrentUser, path: str) -> dict:
    """Read-only GET of an arbitrary CML API path → complete raw JSON.
    Platform-wide; restricted to Ops members / admins (an opaque path can't be
    scoped to a project, so non-privileged users are denied)."""
    from core.integrations.control_interface import CmlApiError

    if not _is_privileged(actor):
        return _raw_privilege_denied()
    err = _validate_raw_path(path)
    if err:
        return {"error": err}
    try:
        data = _control().get_raw(path.strip())
    except CmlApiError as exc:
        return _cml_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning("cml_get_raw failed")
        return _cml_error(exc)
    return _cap_raw(path.strip(), data)


def mmp_get_raw(session, actor: CurrentUser, path: str) -> dict:
    """Read-only GET of an arbitrary MMP API path → complete raw JSON.
    Platform-wide; restricted to Ops members / admins (see cml_get_raw)."""
    from core.integrations.mmp_interface import MmpApiError

    if not _is_privileged(actor):
        return _raw_privilege_denied()
    err = _validate_raw_path(path)
    if err:
        return {"error": err}
    iface = _mmp()
    if not iface.is_configured():
        return {"error": "MMP 平台未配置（base_url/bearer_token）"}
    try:
        data = iface.get_raw(path.strip())
    except MmpApiError as exc:
        return _mmp_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning("mmp_get_raw failed")
        return _mmp_error(exc)
    return _cap_raw(path.strip(), data)


def mmp_live_model_raw(
    session, actor: CurrentUser, repo_name: str, model_name: str,
    max_runs: int = 20, run_id: int = 0,
) -> dict:
    """Live, complete raw MMP model body (attention_required + production runs)
    for one model — every run annotated with its approval_status meaning.

    ``run_id`` (optional) traces a specific production run as ``focus_run``."""
    from core.integrations.mmp_interface import MmpApiError

    if not (repo_name or "").strip():
        return {"error": "repo_name 必填（可先用 mmp_live_projects 查）"}
    if not (model_name or "").strip():
        return {"error": "model_name 必填（可先用 mmp_live_project_status 查）"}
    if not _can_read_mmp_repo(session, actor, repo_name):
        return _scope_denied(f"MMP 项目 {repo_name!r} ")
    iface = _mmp()
    if not iface.is_configured():
        return {"error": "MMP 平台未配置（base_url/bearer_token）"}
    try:
        return build_mmp_model_raw_view(
            iface, repo_name.strip(), model_name.strip(), max_runs=max_runs,
            focus_run_id=int(run_id) or None)
    except LookupError as exc:
        return {"error": str(exc), "hint": "用 mmp_live_project_status 查正确的 repo/model 名"}
    except MmpApiError as exc:
        return _mmp_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning("mmp_live_model_raw failed")
        return _mmp_error(exc)
