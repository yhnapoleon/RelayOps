"""Job + failure-scenario routes — thin HTTP layer.

Business logic lives in core/services/job_service.py.
"""

import time
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.deps.rbac import BusinessOwnerOrAdmin
from api.schema import (
    JobCreate,
    JobDuplicateRequest,
    JobExecutionResponse,
    JobFailureScenarioCreate,
    JobFailureScenarioResponse,
    JobFailureScenarioUpdate,
    JobResponse,
    JobUpdate,
    JobVerificationRequest,
    JobVerificationResponse,
    MmpDriftSnapshotResponse,
    MmpVerificationRequest,
    MmpVerificationResponse,
)
from core.exceptions import NotFoundError
from core.logging import get_logger
from core.models.entities import Job, JobExecution
from core.models.mmp_entities import MmpDriftSnapshot
from core.services import job_service
from core.services.issue_service import false_positive_execution_keys

logger = get_logger(__name__)
router = APIRouter(tags=["jobs"])


def _to_job_response(job: Job) -> JobResponse:
    # The new UI exposes a single Job Name and a single Cron input, but the DB
    # still has the legacy control_m_* columns next to the cml_* ones. For
    # rows persisted before the unification (cml_job_name empty, control_m_*
    # populated), fall back so the new single-input UI shows the user's data
    # as-is without a hidden migration. The save path mirrors the unified
    # value back into both columns.
    unified_name = job.cml_job_name or job.control_m_job_name or ""
    unified_cron = job.schedule_cron or job.control_m_cron or ""
    # An asset is "configured" once it has a job name. The CML project comes
    # from the per-asset override (job.cml_project_name non-empty) or, when
    # the override is empty, is inherited from the owning Ops Project — so
    # gating on cml_project_name here would wrongly mark every inheritor as
    # "unconfigured" even when cml_project_id / cml_job_id resolve fine.
    has_name = bool(unified_name)

    if job.cml_binding_error:
        binding_status = "error"
    elif has_name:
        binding_status = "resolved" if (job.cml_project_id and job.cml_job_id) else "pending"
    else:
        binding_status = "unconfigured"
    return JobResponse(
        id=job.id,
        product_id=job.product_id,
        mmp_project_id=job.mmp_project_id or "",
        mmp_model_id=job.mmp_model_id or "",
        control_m_job_name=unified_name,
        control_m_cron=unified_cron,
        cml_project_name=job.cml_project_name or "",
        cml_job_name=unified_name,
        cml_project_id=job.cml_project_id,
        cml_job_id=job.cml_job_id,
        cml_binding_error=job.cml_binding_error,
        cml_binding_status=binding_status,
        schedule_cron=unified_cron,
        description=job.description or "",
        dependencies=job.dependencies,
        failure_strategy_summary=job.failure_strategy_summary or "",
        dependency_notes=job.dependency_notes or "",
        owner_contact=job.owner_contact or "",
        support_group_id=job.support_group_id,
        support_group_name=job.support_group_name_snapshot or job.support_group or "",
        support_group=job.support_group or "",
        runbook_required=bool(job.runbook_required),
        has_mmp_dependency=job.has_mmp_dependency,
        sla_preset=job.sla_preset,
        sla_custom_minutes=job.sla_custom_minutes,
        is_system=bool(job.is_system),
        last_checked_at=job.last_checked_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


# ── Verification endpoint (async I/O via threadpool around sync client) ────


@router.post("/api/jobs/verification/run", response_model=JobVerificationResponse)
async def run_job_verification(
    body: JobVerificationRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Verify a CML v2 binding by resolving project/job and reading the latest run.

    Target CML project precedence:
    1. ``cml_project_name`` on the body — used as the per-asset override
       (matches the create/update save path in job_service when the form
       carries a non-empty cml_project_name).
    2. Owning Ops Project's binding — looked up via ``project_id``.

    ``cml_project_name`` without ``project_id`` is the legacy script path
    and still works.
    """
    from core.integrations import CmlApiError
    from core.integrations.normalization import _normalize_controlm_status
    from core.services.cml_binding_resolver import build_control_interface
    from core.models.entities import Project

    override = (body.cml_project_name or "").strip()
    project_name = ""
    if override:
        # Per-asset override — matches job_service's save-time resolver. We
        # don't require project_id here, but we still validate it exists
        # when supplied so the UI surface stays consistent.
        if body.project_id is not None:
            project = session.query(Project).filter(Project.id == body.project_id).first()
            if project is None:
                return JobVerificationResponse(
                    ok=False, error=f"Ops Project {body.project_id} not found",
                )
        project_name = override
    elif body.project_id is not None:
        project = session.query(Project).filter(Project.id == body.project_id).first()
        if project is None:
            return JobVerificationResponse(
                ok=False, error=f"Ops Project {body.project_id} not found",
            )
        project_name = (project.cml_project_name or "").strip()
        if not project_name:
            return JobVerificationResponse(
                ok=False,
                error=(
                    "Owning Ops Project has no CML Project Name set — "
                    "configure it on the project page first"
                ),
            )

    job_name = (body.cml_job_name or body.control_m_job_name or "").strip()

    if not job_name:
        return JobVerificationResponse(
            ok=False, error="cml_job_name (or legacy control_m_job_name) is required",
        )
    if not project_name:
        return JobVerificationResponse(
            ok=False,
            error=(
                "Owning Ops Project's CML Project Name is required "
                "(set it on the project, then retry)"
            ),
        )

    control = build_control_interface(timeout=min(float(body.timeout_seconds), 60.0))

    start = time.perf_counter()
    try:
        project_id = await run_in_threadpool(control.resolve_project_id, project_name)
        job_id = await run_in_threadpool(control.resolve_job_id, project_id, job_name)
        runs = await run_in_threadpool(control.list_job_runs, project_id, job_id, limit=1)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
    except CmlApiError as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        not_found = exc.status_code == 404
        return JobVerificationResponse(
            ok=not not_found,           # 404 is "binding not found", not a server failure
            job_found=False,
            duration_ms=elapsed_ms,
            error=exc.message,
        )
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return JobVerificationResponse(
            ok=False, job_found=False, duration_ms=elapsed_ms, error=str(exc),
        )

    # Best-effort: pull the job detail so the UI can offer to auto-fill the
    # cron / timezone from CML. Failures here are intentionally swallowed —
    # the binding itself already verified, so we shouldn't downgrade ok to
    # False just because the detail endpoint hiccuped on the second call.
    cml_schedule: Optional[str] = None
    cml_timezone: Optional[str] = None
    try:
        job_detail = await run_in_threadpool(control.get_job, project_id, job_id)
        sched_raw = job_detail.get("schedule")
        if isinstance(sched_raw, str) and sched_raw.strip():
            cml_schedule = sched_raw.strip()
        tz_raw = job_detail.get("timezone")
        if isinstance(tz_raw, str) and tz_raw.strip():
            cml_timezone = tz_raw.strip()
    except Exception:
        pass

    if not runs:
        return JobVerificationResponse(
            ok=True,
            job_found=True,
            job_status=None,
            last_run=None,
            cml_project_id=project_id,
            cml_job_id=job_id,
            cml_schedule=cml_schedule,
            cml_timezone=cml_timezone,
            duration_ms=elapsed_ms,
            error=f"Job '{job_name}' has no run history yet",
        )

    latest = runs[0]
    try:
        normalized = _normalize_controlm_status(latest.get("status"))
    except Exception:
        normalized = "unknown"

    return JobVerificationResponse(
        ok=True,
        job_found=True,
        job_status=normalized,
        last_run=latest.get("finished_at") or latest.get("running_at") or latest.get("created_at"),
        cml_project_id=project_id,
        cml_job_id=job_id,
        cml_run_id=latest.get("id"),
        cml_schedule=cml_schedule,
        cml_timezone=cml_timezone,
        duration_ms=elapsed_ms,
        error=None,
    )


@router.post("/api/mmp/verification/run", response_model=MmpVerificationResponse)
async def run_mmp_verification(
    body: MmpVerificationRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Verify the MMP binding end-to-end — connectivity + project + model + drift.

    The check progressively narrows so each step's outcome is visible:
      1. Build an MmpInterface from current config and call
         ``list_projects_shallow()``. Failure here means token/URL/SSL is
         broken; nothing else is checked.
      2. If ``mmp_project_id`` was supplied, look it up in the directory.
      3. If ``mmp_model_id`` was also supplied, look it up under that
         project and fetch its full detail so we can report the current
         ``attention_required.model_drifted.status``.

    Returns a structured response so the UI can show partial success
    (e.g. "directory loaded but project not found").
    """
    from core.config import get_config
    from core.integrations.mmp_interface import MmpApiError, MmpInterface

    cfg = get_config()
    iface = MmpInterface(
        base_url=cfg.mmp_base_url,
        bearer_token=cfg.mmp_bearer_token,
        refresh_token=cfg.mmp_refresh_token,
        verify_ssl=cfg.mmp_verify_ssl,
        ca_bundle=cfg.mmp_ca_bundle_path or None,
        timeout=float(cfg.mmp_timeout_seconds),
    )
    if not iface.is_configured():
        return MmpVerificationResponse(
            ok=False,
            error=(
                "MmpInterface is not configured — set mmp.base_url and "
                "mmp.bearer_token (or env RELAYOPS_MMP_BASE_URL / RELAYOPS_MMP_BEARER_TOKEN)"
            ),
        )

    start = time.perf_counter()
    try:
        directory = await run_in_threadpool(iface.list_projects_shallow)
    except MmpApiError as exc:
        return MmpVerificationResponse(
            ok=False,
            duration_ms=int((time.perf_counter() - start) * 1000),
            error=exc.message,
        )
    except Exception as exc:  # noqa: BLE001 — verification path, expose error to UI
        return MmpVerificationResponse(
            ok=False,
            duration_ms=int((time.perf_counter() - start) * 1000),
            error=f"Unexpected error: {exc}",
        )

    total_projects = len(directory)
    repo_name = (body.mmp_project_id or "").strip()
    model_name = (body.mmp_model_id or "").strip()

    if not repo_name:
        return MmpVerificationResponse(
            ok=True,
            duration_ms=int((time.perf_counter() - start) * 1000),
            directory_loaded=True,
            total_projects=total_projects,
        )

    info = directory.get(repo_name)
    if info is None:
        return MmpVerificationResponse(
            ok=False,
            duration_ms=int((time.perf_counter() - start) * 1000),
            directory_loaded=True,
            total_projects=total_projects,
            project_found=False,
            error=f"Project '{repo_name}' not in MMP directory (workspace has {total_projects} projects)",
        )

    business_name = info.get("business_name") or None
    models = info.get("models") or []
    if not model_name:
        return MmpVerificationResponse(
            ok=True,
            duration_ms=int((time.perf_counter() - start) * 1000),
            directory_loaded=True,
            total_projects=total_projects,
            project_found=True,
            business_name=business_name,
            model_count=len(models),
        )

    model_match = next((m for m in models if m.get("name") == model_name), None)
    if model_match is None:
        return MmpVerificationResponse(
            ok=False,
            duration_ms=int((time.perf_counter() - start) * 1000),
            directory_loaded=True,
            total_projects=total_projects,
            project_found=True,
            business_name=business_name,
            model_count=len(models),
            model_found=False,
            error=f"Model '{model_name}' not found under '{repo_name}'",
        )

    # Full chain: fetch project detail to read drift state.
    try:
        project = await run_in_threadpool(iface.get_project, info["id"])
    except MmpApiError as exc:
        return MmpVerificationResponse(
            ok=False,
            duration_ms=int((time.perf_counter() - start) * 1000),
            directory_loaded=True,
            total_projects=total_projects,
            project_found=True,
            business_name=business_name,
            model_count=len(models),
            model_found=True,
            cml_model_id=model_match.get("id"),
            error=f"Project detail fetch failed: {exc.message}",
        )

    detail_model = next(
        (m for m in (project.get("models") or []) if m.get("model_name") == model_name),
        None,
    )

    def _signal(attention: dict, key: str) -> tuple[Optional[bool], Optional[str]]:
        block = attention.get(key) or {}
        if "status" in block:
            return bool(block.get("status")), block.get("description")
        return None, None

    drifted: Optional[bool] = None
    drift_details: Optional[str] = None
    pending_approval: Optional[bool] = None
    pending_approval_details: Optional[str] = None
    pending_review: Optional[bool] = None
    pending_review_details: Optional[str] = None
    if detail_model is not None:
        attention = detail_model.get("attention_required") or {}
        drifted, drift_details = _signal(attention, "model_drifted")
        pending_approval, pending_approval_details = _signal(attention, "run_pending_approval")
        pending_review, pending_review_details = _signal(attention, "run_pending_user_review")

    return MmpVerificationResponse(
        ok=True,
        duration_ms=int((time.perf_counter() - start) * 1000),
        directory_loaded=True,
        total_projects=total_projects,
        project_found=True,
        business_name=business_name,
        model_count=len(models),
        model_found=True,
        cml_model_id=model_match.get("id"),
        drifted=drifted,
        drift_details=drift_details,
        pending_approval=pending_approval,
        pending_approval_details=pending_approval_details,
        pending_review=pending_review,
        pending_review_details=pending_review_details,
    )


@router.get("/api/products/{product_id}/cml-status")
async def get_product_cml_status(
    product_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Return CML latest-run status for every Ops Job in this product.

    Iterates per-job because the v2 contract (CML/job.md §3.4) reads runs
    one job at a time. Jobs without resolved cml_project_id/cml_job_id are
    skipped with status=None so the UI can show "binding pending".
    """
    from core.integrations import CmlApiError
    from core.integrations.normalization import _normalize_controlm_status
    from core.services.cml_binding_resolver import build_control_interface

    jobs = (
        session.query(Job)
        .filter(Job.product_id == product_id)
        .all()
    )
    # Distinguish "product not found" (no rows ever) from "no jobs in product".
    if not jobs:
        if job_service.get_product_cml_status_pairs(session, product_id=product_id) is None:
            raise NotFoundError("Product not found")
        return {"product_id": product_id, "jobs": {}}

    control = build_control_interface()

    result: dict = {}
    # Stamp every job we actually polled with "now" so the asset card can
    # show "Last Check" even if the periodic checker hasn't run since.
    poll_started_at = datetime.utcnow()
    touched_any = False
    for job in jobs:
        if not job.cml_project_id or not job.cml_job_id:
            result[str(job.id)] = {
                "cml_status": None,
                "last_run": None,
                "last_checked_at": job.last_checked_at.isoformat() if job.last_checked_at else None,
                "binding": "unresolved",
            }
            continue
        try:
            runs = await run_in_threadpool(
                control.list_job_runs, job.cml_project_id, job.cml_job_id, limit=1
            )
        except CmlApiError as exc:
            # The poll itself happened (and failed) — record the attempt.
            job.last_checked_at = poll_started_at
            touched_any = True
            result[str(job.id)] = {
                "cml_status": None,
                "last_run": None,
                "last_checked_at": poll_started_at.isoformat(),
                "error": exc.message,
            }
            continue

        job.last_checked_at = poll_started_at
        touched_any = True

        if not runs:
            result[str(job.id)] = {
                "cml_status": None,
                "last_run": None,
                "last_checked_at": poll_started_at.isoformat(),
            }
            continue

        latest = runs[0]
        try:
            normalized = _normalize_controlm_status(latest.get("status"))
        except Exception:
            normalized = "unknown"
        result[str(job.id)] = {
            "cml_status": normalized,
            "last_run": (
                latest.get("finished_at")
                or latest.get("running_at")
                or latest.get("created_at")
            ),
            "cml_run_id": latest.get("id"),
            "last_checked_at": poll_started_at.isoformat(),
        }
    if touched_any:
        session.commit()
    return {"product_id": product_id, "jobs": result}


# ── Job CRUD ───────────────────────────────────────────────────────────


@router.post(
    "/api/products/{product_id}/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_job(
    product_id: int,
    body: JobCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    job = job_service.create(session, product_id=product_id, body=body, actor=current_user)
    return _to_job_response(job)


@router.get("/api/products/{product_id}/jobs", response_model=List[JobResponse])
def list_jobs(
    product_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    jobs = job_service.list_for_product(session, product_id=product_id, actor=current_user)
    return [_to_job_response(j) for j in jobs]


@router.put("/api/jobs/{job_id}", response_model=JobResponse)
def update_job(
    job_id: int,
    body: JobUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    job = job_service.update(session, job_id=job_id, body=body, actor=current_user)
    return _to_job_response(job)


@router.delete("/api/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(
    job_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    job_service.delete(session, job_id=job_id, actor=current_user)


@router.post("/api/jobs/{job_id}/duplicate", response_model=JobResponse)
def duplicate_job(
    job_id: int,
    body: JobDuplicateRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    job = job_service.duplicate(session, source_job_id=job_id, body=body, actor=current_user)
    return _to_job_response(job)


@router.post("/api/jobs/{job_id}/resolve-binding", response_model=JobResponse)
def resolve_job_binding(
    job_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Re-attempt CML name → id resolution for this Job and persist the result.

    Read-side only — it doesn't go through the draft/version flow because
    the user-facing scope (cml_project_name / cml_job_name) is unchanged;
    this just refreshes cached lookup ids so the monitoring loop and
    Check Now stop skipping the row.
    """
    job = job_service.resolve_binding(session, job_id=job_id, actor=current_user)
    return _to_job_response(job)


# ── Failure-scenario CRUD ──────────────────────────────────────────────


@router.get("/api/jobs/{job_id}/failure-scenarios", response_model=List[JobFailureScenarioResponse])
def list_job_failure_scenarios(
    job_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    scenarios = job_service.list_scenarios(session, job_id=job_id, actor=current_user)
    return [JobFailureScenarioResponse.model_validate(s) for s in scenarios]


@router.post(
    "/api/jobs/{job_id}/failure-scenarios",
    response_model=JobFailureScenarioResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_job_failure_scenario(
    job_id: int,
    body: JobFailureScenarioCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    scenario = job_service.create_scenario(session, job_id=job_id, body=body, actor=current_user)
    return JobFailureScenarioResponse.model_validate(scenario)


@router.put(
    "/api/job-failure-scenarios/{scenario_id}",
    response_model=JobFailureScenarioResponse,
)
def update_job_failure_scenario(
    scenario_id: int,
    body: JobFailureScenarioUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    scenario = job_service.update_scenario(
        session, scenario_id=scenario_id, body=body, actor=current_user
    )
    return JobFailureScenarioResponse.model_validate(scenario)


@router.delete(
    "/api/job-failure-scenarios/{scenario_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_job_failure_scenario(
    scenario_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    job_service.delete_scenario(session, scenario_id=scenario_id, actor=current_user)


@router.get("/api/job-executions/{job_id}", response_model=List[JobExecutionResponse])
def list_job_executions(
    job_id: int,
    limit: int = 50,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List the most recent execution snapshots persisted by the monitoring Controller's CML poll."""
    if session.query(Job).filter(Job.id == job_id).first() is None:
        raise NotFoundError("Job not found")
    rows = (
        session.query(JobExecution)
        .filter(JobExecution.job_id == job_id)
        .order_by(JobExecution.timestamp.desc(), JobExecution.id.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    # Runs whose failure/stale alert was dismissed as a false positive are
    # flagged so the Job Health timeline renders them as success points.
    false_positive_keys = false_positive_execution_keys(session, [job_id])

    def _serialize(row: JobExecution) -> JobExecutionResponse:
        payload = JobExecutionResponse.model_validate(row)
        payload.false_positive = (row.job_id, row.cml_run_id) in false_positive_keys
        return payload

    return [_serialize(r) for r in rows]


@router.get(
    "/api/jobs/{job_id}/mmp-drift-snapshots",
    response_model=List[MmpDriftSnapshotResponse],
)
def list_job_mmp_drift_snapshots(
    job_id: int,
    from_: Optional[datetime] = Query(default=None, alias="from"),
    to: Optional[datetime] = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Return persisted MMP drift snapshots for a Job within a time window.

    Feeds the MMP half of the unified Health Timeline. Mirrors the App
    health-checks endpoint: newest-first, optional ``from`` / ``to`` ISO
    datetimes (UTC), hard cap of 5000 rows so a misconfigured client can't
    pull the full history in one go. Returns ``[]`` (not 404) when the Job
    exists but has no MMP dependency / no snapshots yet — the UI uses the
    empty array to render its "no samples in this window" placeholder.
    """
    if session.query(Job).filter(Job.id == job_id).first() is None:
        raise NotFoundError("Job not found")

    q = session.query(MmpDriftSnapshot).filter(MmpDriftSnapshot.job_id == job_id)
    if from_ is not None:
        q = q.filter(MmpDriftSnapshot.observed_at >= from_)
    if to is not None:
        q = q.filter(MmpDriftSnapshot.observed_at <= to)
    rows = q.order_by(MmpDriftSnapshot.observed_at.desc()).limit(limit).all()
    return [MmpDriftSnapshotResponse.model_validate(r) for r in rows]
