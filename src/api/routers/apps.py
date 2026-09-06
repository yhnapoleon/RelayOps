"""Application + recovery-scenario routes — thin HTTP layer.

Business logic lives in core/services/app_service.py. This module
only handles HTTP-level concerns (parsing, the verification proxy
endpoint, response shaping).
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.deps.rbac import BusinessOwnerOrAdmin
from api.schema import (
    AppCreate,
    AppDuplicateRequest,
    AppResponse,
    AppUpdate,
    ApplicationApiCheckCreate,
    ApplicationBindingProbeRequest,
    ApplicationBindingProbeResponse,
    ApplicationApiCheckResponse,
    ApplicationApiCheckRunResponse,
    ApplicationApiCheckRunResult,
    ApplicationApiCheckUpdate,
    ApplicationHealthCheckResponse,
    ApplicationRecoveryScenarioCreate,
    ApplicationRecoveryScenarioResponse,
    ApplicationRecoveryScenarioUpdate,
    ApplicationVerificationRequest,
    ApplicationVerificationResponse,
)
from core.exceptions import NotFoundError, ValidationError
from core.logging import get_logger
from core.models.app_entities import (
    ApplicationApiCheck,
    ApplicationApiCheckRun,
    ApplicationHealthCheck,
)
from core.models.entities import Application
from core.services import app_service
from core.services.audit_service import log_audit

logger = get_logger(__name__)
router = APIRouter(tags=["applications"])
MAX_VERIFICATION_RESPONSE_BODY = 4000


def _to_app_response(app: Application) -> AppResponse:
    # An asset is "configured" once it has an application name or subdomain.
    # The CML project comes from the per-asset override (app.cml_project_name
    # non-empty) or, when the override is empty, is inherited from the owning
    # Ops Project — so gating on cml_project_name here would wrongly mark
    # every inheritor as "unconfigured" even when cml_project_id /
    # cml_application_id resolve fine.
    has_name = bool(app.cml_application_name or app.cml_subdomain)
    if app.cml_binding_error:
        binding_status = "error"
    elif has_name:
        binding_status = "resolved" if (app.cml_project_id and app.cml_application_id) else "pending"
    else:
        binding_status = "unconfigured"
    # The new App UI exposes a single Serving URL input; legacy DB rows may
    # have populated only application_url or only health_check_url, so fall
    # back across the three columns when serializing so the form initializer
    # always sees a value. The save path mirrors the unified value back.
    unified_url = app.cml_serving_url or app.application_url or app.health_check_url or ""
    return AppResponse(
        id=app.id,
        product_id=app.product_id,
        application_url=unified_url,
        health_check_url=unified_url,
        cml_project_name=app.cml_project_name or "",
        cml_application_name=app.cml_application_name or "",
        cml_subdomain=app.cml_subdomain or "",
        cml_app_type=app.cml_app_type or "generic",
        cml_project_id=app.cml_project_id,
        cml_application_id=app.cml_application_id,
        cml_serving_url=unified_url,
        cml_binding_error=app.cml_binding_error,
        cml_binding_status=binding_status,
        last_cml_status=app.last_cml_status,
        last_relayops_health=app.last_relayops_health,
        last_checked_at=app.last_checked_at,
        last_check_error=app.last_check_error,
        description=app.description or "",
        restart_supported=bool(app.restart_supported),
        restart_summary=app.restart_summary or "",
        owner_contact=app.owner_contact or "",
        support_group_id=app.support_group_id,
        support_group_name=app.support_group_name_snapshot or app.support_group or "",
        support_group=app.support_group or "",
        is_system=bool(app.is_system),
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


# ── Verification proxy (genuine async I/O — keep async def) ────────────


def _normalize_verification_headers(headers: Dict[str, str] | None) -> Dict[str, str]:
    if not headers:
        return {}
    out: Dict[str, str] = {}
    for key, value in headers.items():
        key_text = (key or "").strip()
        if not key_text:
            continue
        out[key_text] = str(value)
    return out


def _resolve_verification_payload(body: Any):
    if body is None:
        return "", False
    if isinstance(body, (dict, list)):
        return body, True
    if isinstance(body, str):
        raw = body.strip()
        if raw == "":
            return "", False
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, (dict, list)):
                return parsed, True
        except json.JSONDecodeError:
            pass
        return body, False
    return body, False


_METHODS_WITHOUT_BODY = {"GET", "HEAD", "DELETE"}


def _hostname_hint(target_url: str, error_msg: str) -> Optional[str]:
    """If the URL host looks like a Docker service name and the error is a
    connection/DNS failure, suggest using localhost instead."""
    try:
        host = (urlparse(target_url).hostname or "").strip()
    except Exception:
        return None
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return None
    # Heuristic: docker service names are bare hostnames (no dots, not pure IPs)
    if "." in host:
        return None
    if host.replace(":", "").isdigit():
        return None
    lowered = error_msg.lower()
    if "connection" not in lowered and "name" not in lowered and "resolve" not in lowered:
        return None
    return (
        f"Hint: '{host}' looks like a Docker service name. "
        "If the backend is running on the host (not inside docker-compose), "
        "use 'localhost' or '127.0.0.1' with the mapped port instead."
    )


def _probe_cml_app_binding(
    session: Session,
    *,
    project_id: Optional[int],
    cml_application_name: Optional[str],
    cml_subdomain: Optional[str],
    cml_project_name_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the same name → application_id resolver the save path uses.

    Returns a dict with keys:
    * ``ok``                 — None (not asked), True (found), False (rejected).
    * ``application_id``     — resolved id when ``ok`` is True, else None.
    * ``error``              — human-readable reason when ``ok`` is False.
    * ``resolved_name``      — the app's real CML name when resolved (lets the
      form reverse-fill the name field from a subdomain-only entry).
    * ``other``              — when the subdomain misses under the bound CML
      project but exists elsewhere in the workspace, the owning project / app
      (see ``ControlInterface.find_application_by_subdomain``); else None.

    When ``cml_project_name_override`` is non-empty the probe resolves
    against that CML project instead of the owning Ops project's binding —
    mirrors the per-asset override that app_service applies on save.
    """
    def _fail(msg: str, other: Optional[dict] = None,
              scan_note: Optional[str] = None) -> Dict[str, Any]:
        return {"ok": False, "application_id": None, "error": msg,
                "resolved_name": None, "other": other, "scan_note": scan_note}

    subdomain = (cml_subdomain or "").strip()
    if not cml_application_name and not subdomain:
        # Nothing to resolve — treat as "binding not requested" (ok=None) so a
        # plain URL-reachability probe doesn't surface a spurious binding error.
        return {"ok": None, "application_id": None, "error": None,
                "resolved_name": None, "other": None, "scan_note": None}

    from core.integrations import CmlApiError
    from core.models.entities import Project
    from core.services.cml_binding_resolver import (
        build_control_interface,
        resolve_project_id as _resolve_cml_project_id,
    )

    control = build_control_interface()

    def _scan_workspace(exclude: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
        """Best-effort workspace search by subdomain.

        Returns ``(match, note)`` — ``match`` is the owning project/app (or
        None), ``note`` is a human-readable summary of the scan when nothing
        matched (so the UI never shows a silent empty result). Both None when
        there's no subdomain to search.
        """
        if not subdomain:
            return None, None
        try:
            diag = control.find_application_by_subdomain(subdomain, exclude_project_id=exclude)
        except CmlApiError as exc:
            return None, f"Workspace scan failed: {exc.message}"
        match = diag.get("match")
        if match:
            return match, None
        if diag.get("error"):
            return None, f"Could not list CML projects: {diag['error']}"
        scanned, total = diag.get("scanned", 0), diag.get("total", 0)
        note = f"Searched {scanned} of {total} accessible CML project(s) by subdomain — no match"
        if diag.get("timed_out"):
            note += " (stopped at the time budget — not all projects scanned)"
        elif diag.get("capped"):
            note += " (stopped at the scan cap — not all projects scanned)"
        else:
            note += ". It may live in a CML project your Ops identity can't access."
        return None, note

    # Resolve the CML project we should look inside first (override wins, else
    # the owning Ops Project's cached binding). cml_pid stays None when there's
    # no usable binding — we still fall back to a workspace scan by subdomain
    # so a Fetch works mid-onboarding before the parent project is bound.
    cml_pid: Optional[str] = None
    bind_note: Optional[str] = None
    override = (cml_project_name_override or "").strip()
    if override:
        try:
            pid, perr = _resolve_cml_project_id(control, override)
            cml_pid = pid or None
            if not cml_pid:
                bind_note = f"Override CML project '{override}' not resolvable: {perr or 'no id returned'}"
        except CmlApiError as exc:
            bind_note = f"Override project lookup failed: {exc.message}"
    elif project_id is not None:
        project = session.query(Project).filter(Project.id == project_id).first()
        if project is None:
            bind_note = f"Ops Project {project_id} not found"
        else:
            cml_pid = (project.cml_project_id or "").strip() or None
            if not cml_pid:
                bind_note = (
                    f"Project unresolved: {project.cml_binding_error}"
                    if (project.cml_binding_error or "").strip()
                    else "Owning Ops Project has no CML Project Name set"
                )

    # Fast path: try inside the bound project (when we have one).
    if cml_pid:
        try:
            app_id = control.resolve_application_id(
                cml_pid,
                name=(cml_application_name or None),
                subdomain=(subdomain or None),
            )
        except CmlApiError as exc:
            # Missed here — point at the project the subdomain actually owns.
            other, scan_note = _scan_workspace(exclude=cml_pid)
            return _fail(exc.message, other, scan_note)
        resolved_name = None
        try:
            detail = control.get_application(cml_pid, app_id)
            resolved_name = (str(detail.get("name") or "").strip()) or None
        except CmlApiError:
            resolved_name = None
        return {"ok": True, "application_id": app_id, "error": None,
                "resolved_name": resolved_name, "other": None, "scan_note": None}

    # No usable project binding. A subdomain Fetch can still discover the
    # owning project by scanning the workspace; a name-only lookup cannot.
    if subdomain:
        other, scan_note = _scan_workspace(exclude=None)
        return _fail(
            bind_note or "No CML project bound — searched the workspace by subdomain",
            other, scan_note,
        )
    return _fail(bind_note or "No CML project bound and no subdomain to search")


@router.post("/api/apps/verification/run", response_model=ApplicationVerificationResponse)
async def run_application_verification(
    body: ApplicationVerificationRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    target_url = (body.url or "").strip()
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError("Verification URL must be a valid http/https URL")
    # SSRF guard — block private / loopback / cloud-metadata ranges. Same
    # protection the persisted ApplicationApiCheck.run endpoint applies.
    _blocked, _blocked_reason = _is_blocked_host(parsed.hostname or "")
    if _blocked:
        raise ValidationError(f"Blocked target: {_blocked_reason}")

    # Run the CML binding check up-front — it's cheap (a couple of GETs to
    # CML) and the result is independent of whether the URL probe succeeds.
    # On a subdomain miss it may also scan the workspace for the owning
    # project (bounded, best-effort).
    probe = _probe_cml_app_binding(
        session,
        project_id=body.project_id,
        cml_application_name=body.cml_application_name,
        cml_subdomain=body.cml_subdomain,
        cml_project_name_override=body.cml_project_name,
    )
    cml_ok = probe["ok"]
    cml_app_id = probe["application_id"]
    cml_err = probe["error"]
    cml_resolved_name = probe["resolved_name"]
    cml_scan_note = probe.get("scan_note")
    _other = probe["other"] or {}
    cml_other_project_id = _other.get("project_id")
    cml_other_project_name = _other.get("project_name")
    cml_other_application_id = _other.get("application_id")
    cml_other_application_name = _other.get("application_name")

    method = (body.method or "POST").strip().upper()
    request_headers = _normalize_verification_headers(body.headers)
    payload, use_json = _resolve_verification_payload(body.body)
    timeout = max(1.0, min(float(body.timeout_seconds or 15.0), 60.0))
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, verify=False) as client:
            kwargs: Dict[str, Any] = {"headers": request_headers}
            # Methods like GET/HEAD/DELETE conventionally have no body. Only
            # attach payload for methods that carry one, even if the user
            # populated the body field.
            if method not in _METHODS_WITHOUT_BODY:
                if use_json:
                    kwargs["json"] = payload
                else:
                    kwargs["content"] = payload if payload is not None else ""
            response = await client.request(method, target_url, **kwargs)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        text = response.text or ""
        if len(text) > MAX_VERIFICATION_RESPONSE_BODY:
            text = text[:MAX_VERIFICATION_RESPONSE_BODY] + "\n...(truncated)"
        return ApplicationVerificationResponse(
            ok=response.is_success,
            status_code=response.status_code,
            duration_ms=elapsed_ms,
            response_headers=dict(response.headers),
            response_body=text,
            error=None,
            cml_binding_ok=cml_ok,
            cml_application_id=cml_app_id,
            cml_binding_error=cml_err,
            cml_resolved_application_name=cml_resolved_name,
            cml_other_project_id=cml_other_project_id,
            cml_other_project_name=cml_other_project_name,
            cml_other_application_id=cml_other_application_id,
            cml_other_application_name=cml_other_application_name,
            cml_scan_note=cml_scan_note,
        )
    except httpx.RequestError as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        error_msg = str(exc)
        hint = _hostname_hint(target_url, error_msg)
        if hint:
            error_msg = f"{error_msg} — {hint}"
        return ApplicationVerificationResponse(
            ok=False,
            status_code=None,
            duration_ms=elapsed_ms,
            response_headers={},
            response_body="",
            error=error_msg,
            cml_binding_ok=cml_ok,
            cml_application_id=cml_app_id,
            cml_binding_error=cml_err,
            cml_resolved_application_name=cml_resolved_name,
            cml_other_project_id=cml_other_project_id,
            cml_other_project_name=cml_other_project_name,
            cml_other_application_id=cml_other_application_id,
            cml_other_application_name=cml_other_application_name,
            cml_scan_note=cml_scan_note,
        )


@router.post("/api/apps/binding/probe", response_model=ApplicationBindingProbeResponse)
def probe_application_binding(
    body: ApplicationBindingProbeRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Resolve a CML app binding from name/subdomain — no HTTP probe.

    Lets the App form's Subdomain "Fetch" button reverse-fill the real CML app
    name and, on a same-project miss, point at the project the subdomain
    actually belongs to. Cheap (a couple of CML GETs; a bounded workspace scan
    only on a subdomain miss).
    """
    probe = _probe_cml_app_binding(
        session,
        project_id=body.project_id,
        cml_application_name=body.cml_application_name,
        cml_subdomain=body.cml_subdomain,
        cml_project_name_override=body.cml_project_name,
    )
    other = probe["other"] or {}
    return ApplicationBindingProbeResponse(
        cml_binding_ok=probe["ok"],
        cml_application_id=probe["application_id"],
        cml_binding_error=probe["error"],
        cml_resolved_application_name=probe["resolved_name"],
        cml_other_project_id=other.get("project_id"),
        cml_other_project_name=other.get("project_name"),
        cml_other_application_id=other.get("application_id"),
        cml_other_application_name=other.get("application_name"),
        cml_scan_note=probe.get("scan_note"),
    )


# ── App CRUD ───────────────────────────────────────────────────────────


@router.post(
    "/api/products/{product_id}/apps",
    response_model=AppResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_app(
    product_id: int,
    body: AppCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    app = app_service.create(session, product_id=product_id, body=body, actor=current_user)
    return _to_app_response(app)


@router.get("/api/products/{product_id}/apps", response_model=List[AppResponse])
def list_apps(
    product_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    apps = app_service.list_for_product(session, product_id=product_id, actor=current_user)
    return [_to_app_response(a) for a in apps]


@router.put("/api/apps/{app_id}", response_model=AppResponse)
def update_app(
    app_id: int,
    body: AppUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    app = app_service.update(session, app_id=app_id, body=body, actor=current_user)
    return _to_app_response(app)


@router.delete("/api/apps/{app_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_app(
    app_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    app_service.delete(session, app_id=app_id, actor=current_user)


@router.post("/api/apps/{app_id}/duplicate", response_model=AppResponse)
def duplicate_app(
    app_id: int,
    body: AppDuplicateRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    app = app_service.duplicate(session, source_app_id=app_id, body=body, actor=current_user)
    return _to_app_response(app)


@router.post("/api/apps/{app_id}/resolve-binding", response_model=AppResponse)
def resolve_app_binding(
    app_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Re-attempt CML name → id resolution for this Application and persist."""
    app = app_service.resolve_binding(session, app_id=app_id, actor=current_user)
    return _to_app_response(app)


# ── Recovery scenarios ─────────────────────────────────────────────────


@router.get(
    "/api/apps/{app_id}/recovery-scenarios",
    response_model=List[ApplicationRecoveryScenarioResponse],
)
def list_application_recovery_scenarios(
    app_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    scenarios = app_service.list_scenarios(session, app_id=app_id, actor=current_user)
    return [ApplicationRecoveryScenarioResponse.model_validate(s) for s in scenarios]


@router.post(
    "/api/apps/{app_id}/recovery-scenarios",
    response_model=ApplicationRecoveryScenarioResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_application_recovery_scenario(
    app_id: int,
    body: ApplicationRecoveryScenarioCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    scenario = app_service.create_scenario(session, app_id=app_id, body=body, actor=current_user)
    return ApplicationRecoveryScenarioResponse.model_validate(scenario)


@router.put(
    "/api/application-recovery-scenarios/{scenario_id}",
    response_model=ApplicationRecoveryScenarioResponse,
)
def update_application_recovery_scenario(
    scenario_id: int,
    body: ApplicationRecoveryScenarioUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    scenario = app_service.update_scenario(
        session, scenario_id=scenario_id, body=body, actor=current_user
    )
    return ApplicationRecoveryScenarioResponse.model_validate(scenario)


@router.delete(
    "/api/application-recovery-scenarios/{scenario_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_application_recovery_scenario(
    scenario_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    app_service.delete_scenario(session, scenario_id=scenario_id, actor=current_user)


@router.get(
    "/api/apps/{app_id}/health-checks",
    response_model=List[ApplicationHealthCheckResponse],
)
def list_application_health_checks(
    app_id: int,
    from_: Optional[datetime] = Query(default=None, alias="from"),
    to: Optional[datetime] = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Return persisted health-check samples for an app within a time window.

    Feeds the App Health time-series chart. Newest-first; pass ``from`` /
    ``to`` (ISO datetime, UTC) to bound the window. Hard cap of 5000 rows
    so a misconfigured client can't pull the full history in one go.
    """
    if session.query(Application).filter(Application.id == app_id).first() is None:
        raise NotFoundError("Application not found")

    q = session.query(ApplicationHealthCheck).filter(
        ApplicationHealthCheck.application_id == app_id
    )
    if from_ is not None:
        q = q.filter(ApplicationHealthCheck.checked_at >= from_)
    if to is not None:
        q = q.filter(ApplicationHealthCheck.checked_at <= to)
    rows = q.order_by(ApplicationHealthCheck.checked_at.desc()).limit(limit).all()
    return [ApplicationHealthCheckResponse.model_validate(r) for r in rows]


# ── Persisted API Checks (Postman-style validation suite per app) ──────
#
# Why this exists alongside /api/apps/verification/run:
#   * /verification/run is a one-shot probe wired to the App create/edit
#     form — its inputs live in form state and die with the dialog.
#   * Once an app is live Ops still wants to re-run those probes during
#     incidents / handovers / audits. ApplicationApiCheck is the saved,
#     re-runnable version — owned by an App, listed on a dedicated page,
#     executed through the same backend httpx proxy.


import socket as _socket

# Max persisted response body. Matches the legacy probe so audit and run
# history rows are the same size class.
_API_CHECK_RUN_HISTORY_PER_CHECK = 50  # rolling retention

# The cloud-metadata endpoint — AWS / Azure / GCP / OCI all expose credentials
# at 169.254.169.254. It is never a legitimate validation target and is the
# canonical SSRF credential-theft vector, so it stays blocked.
#
# Everything else (localhost, RFC1918, Docker service names, link-local) is
# ALLOWED on purpose: this is a Ops monitoring platform whose entire job is
# blacklist rejected exactly those legitimate targets (CML apps resolve to
# internal IPs), turning every validation into a 400 "Blocked target" — so
# the guard is intentionally narrow.
_METADATA_IPS = {"169.254.169.254", "fd00:ec2::254"}


def _is_blocked_host(host: str) -> tuple[bool, Optional[str]]:
    """Minimal SSRF guard — blocks ONLY the cloud-metadata endpoint.

    Returns ``(blocked, reason)``. A DNS failure is NOT treated as blocked:
    we let the HTTP client attempt the request so the user gets a real
    connection error instead of a misleading 400. Internal / private hosts
    are allowed (see ``_METADATA_IPS`` rationale).
    """
    candidate = (host or "").strip().lower()
    if not candidate:
        return True, "URL has no hostname"
    if candidate in _METADATA_IPS:
        return True, "The cloud metadata endpoint (169.254.169.254) is not an allowed target"
    # Resolve and block only if it maps to the metadata address (defends
    # against a hostname pointing at metadata). Any resolution failure is
    # left for the HTTP client to report.
    try:
        infos = _socket.getaddrinfo(candidate, None)
    except _socket.gaierror:
        return False, None
    for info in infos:
        ip_str = info[4][0]
        if ip_str in _METADATA_IPS:
            return True, (
                f"Host {candidate} resolves to the cloud metadata endpoint, "
                "which is not an allowed target"
            )
    return False, None


async def _execute_api_check_http(
    *,
    url: str,
    method: str,
    headers: Dict[str, str],
    body_text: Optional[str],
    body_is_json: bool,
    timeout_seconds: int,
) -> Dict[str, Any]:
    """Single, shared executor for both the legacy verification proxy and
    the persisted ApplicationApiCheck.run endpoint.

    Returns a dict with the same shape both call sites need (status, body,
    headers, duration, error). The caller decides what to do with it —
    return it directly (legacy proxy) or persist a run row (api check).
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError("URL must be a valid http/https URL")
    blocked, reason = _is_blocked_host(parsed.hostname or "")
    if blocked:
        raise ValidationError(f"Blocked target: {reason}")

    norm_method = (method or "GET").strip().upper()
    norm_headers = _normalize_verification_headers(headers)
    payload, use_json = _resolve_verification_payload(body_text if body_is_json or not isinstance(body_text, str) else body_text)
    timeout = max(1.0, min(float(timeout_seconds or 15), 60.0))
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, verify=False) as client:
            kwargs: Dict[str, Any] = {"headers": norm_headers}
            if norm_method not in _METHODS_WITHOUT_BODY:
                if use_json:
                    kwargs["json"] = payload
                else:
                    kwargs["content"] = payload if payload is not None else ""
            response = await client.request(norm_method, url, **kwargs)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        text = response.text or ""
        if len(text) > MAX_VERIFICATION_RESPONSE_BODY:
            text = text[:MAX_VERIFICATION_RESPONSE_BODY] + "\n...(truncated)"
        return {
            "ok": response.is_success,
            "status_code": response.status_code,
            "duration_ms": elapsed_ms,
            "response_headers": dict(response.headers),
            "response_body": text,
            "error": None,
        }
    except httpx.RequestError as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        error_msg = str(exc)
        hint = _hostname_hint(url, error_msg)
        if hint:
            error_msg = f"{error_msg} — {hint}"
        return {
            "ok": False,
            "status_code": None,
            "duration_ms": elapsed_ms,
            "response_headers": {},
            "response_body": "",
            "error": error_msg,
        }


def _serialize_api_check(check: ApplicationApiCheck) -> ApplicationApiCheckResponse:
    return ApplicationApiCheckResponse(
        id=check.id,
        application_id=check.application_id,
        collection_name=check.collection_name or "",
        name=check.name or "",
        method=check.method or "GET",
        url=check.url or "",
        headers=dict(check.headers or {}),
        body=check.body or "",
        body_is_json=bool(check.body_is_json),
        timeout_seconds=int(check.timeout_seconds or 15),
        expected_status=check.expected_status,
        expected_body_contains=check.expected_body_contains,
        source=check.source or "manual",
        source_ref=check.source_ref,
        is_active=bool(check.is_active),
        sort_order=int(check.sort_order or 0),
        last_run_at=check.last_run_at,
        last_run_ok=check.last_run_ok,
        last_run_status_code=check.last_run_status_code,
        last_run_duration_ms=check.last_run_duration_ms,
        created_at=check.created_at,
        updated_at=check.updated_at,
        created_by=check.created_by,
        updated_by=check.updated_by,
    )


def _evaluate_assertions(
    check: ApplicationApiCheck,
    run_result: Dict[str, Any],
) -> tuple[Optional[bool], Optional[str]]:
    """Apply the (optional) expected_status / expected_body_contains
    assertions to a run result. Returns ``(passed, reason)`` — both
    ``None`` when the check has no assertions configured."""
    has_status_check = check.expected_status is not None
    body_token = (check.expected_body_contains or "").strip()
    if not has_status_check and not body_token:
        return None, None
    if run_result.get("error"):
        return False, f"Request failed: {run_result['error']}"
    if has_status_check and run_result.get("status_code") != check.expected_status:
        return False, (
            f"Expected status {check.expected_status}, got {run_result.get('status_code')}"
        )
    if body_token and body_token not in (run_result.get("response_body") or ""):
        return False, f"Response body did not contain '{body_token}'"
    return True, "Assertions passed"


@router.get(
    "/api/apps/{app_id}/api-checks",
    response_model=List[ApplicationApiCheckResponse],
)
def list_application_api_checks(
    app_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Return every persisted API check for an app, ordered by
    collection_name then sort_order then id. Read-only access for any
    authenticated user — write ops gate on BusinessOwnerOrAdmin."""
    if session.query(Application).filter(Application.id == app_id).first() is None:
        raise NotFoundError("Application not found")
    rows = (
        session.query(ApplicationApiCheck)
        .filter(ApplicationApiCheck.application_id == app_id)
        .order_by(
            ApplicationApiCheck.collection_name.asc(),
            ApplicationApiCheck.sort_order.asc(),
            ApplicationApiCheck.id.asc(),
        )
        .all()
    )
    return [_serialize_api_check(r) for r in rows]


@router.post(
    "/api/apps/{app_id}/api-checks",
    response_model=ApplicationApiCheckResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_application_api_check(
    app_id: int,
    body: ApplicationApiCheckCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    if session.query(Application).filter(Application.id == app_id).first() is None:
        raise NotFoundError("Application not found")
    check = ApplicationApiCheck(
        application_id=app_id,
        collection_name=(body.collection_name or "").strip(),
        name=body.name.strip(),
        method=body.method.upper(),
        url=body.url.strip(),
        headers=dict(body.headers or {}),
        body=body.body or "",
        body_is_json=bool(body.body_is_json),
        timeout_seconds=int(body.timeout_seconds),
        expected_status=body.expected_status,
        expected_body_contains=body.expected_body_contains,
        source=body.source,
        source_ref=body.source_ref,
        is_active=bool(body.is_active),
        sort_order=int(body.sort_order or 0),
        created_by=current_user.user_id,
        updated_by=current_user.user_id,
    )
    session.add(check)
    session.commit()
    session.refresh(check)
    log_audit(
        user_id=current_user.user_id,
        action="create",
        entity_type="application_api_check",
        entity_id=check.id,
        new_value={
            "application_id": app_id,
            "name": check.name,
            "method": check.method,
            "url": check.url,
        },
    )
    return _serialize_api_check(check)


@router.put(
    "/api/api-checks/{check_id}",
    response_model=ApplicationApiCheckResponse,
)
def update_application_api_check(
    check_id: int,
    body: ApplicationApiCheckUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    check = session.query(ApplicationApiCheck).filter(ApplicationApiCheck.id == check_id).first()
    if check is None:
        raise NotFoundError("API check not found")
    old_snapshot = {
        "name": check.name, "method": check.method, "url": check.url,
        "is_active": bool(check.is_active),
    }
    data = body.model_dump(exclude_unset=True)
    if "method" in data and data["method"]:
        data["method"] = data["method"].upper()
    if "url" in data and data["url"]:
        data["url"] = data["url"].strip()
    if "name" in data and data["name"]:
        data["name"] = data["name"].strip()
    if "collection_name" in data and data["collection_name"] is not None:
        data["collection_name"] = data["collection_name"].strip()
    for field, value in data.items():
        setattr(check, field, value)
    check.updated_by = current_user.user_id
    check.updated_at = datetime.utcnow()
    session.commit()
    session.refresh(check)
    log_audit(
        user_id=current_user.user_id,
        action="update",
        entity_type="application_api_check",
        entity_id=check.id,
        old_value=old_snapshot,
        new_value={"name": check.name, "method": check.method, "url": check.url,
                   "is_active": bool(check.is_active)},
    )
    return _serialize_api_check(check)


@router.delete(
    "/api/api-checks/{check_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_application_api_check(
    check_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    check = session.query(ApplicationApiCheck).filter(ApplicationApiCheck.id == check_id).first()
    if check is None:
        raise NotFoundError("API check not found")
    snapshot = {"name": check.name, "method": check.method, "url": check.url}
    session.delete(check)
    session.commit()
    log_audit(
        user_id=current_user.user_id,
        action="delete",
        entity_type="application_api_check",
        entity_id=check_id,
        old_value=snapshot,
    )


@router.post(
    "/api/api-checks/{check_id}/run",
    response_model=ApplicationApiCheckRunResult,
)
async def run_application_api_check(
    check_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Fire the persisted request and store the outcome.

    Authentication-level access is enough — running a saved check doesn't
    mutate the check definition itself, only appends to its run history.
    SSRF guard is enforced inside ``_execute_api_check_http``.
    """
    check = session.query(ApplicationApiCheck).filter(ApplicationApiCheck.id == check_id).first()
    if check is None:
        raise NotFoundError("API check not found")

    run_result = await _execute_api_check_http(
        url=check.url or "",
        method=check.method or "GET",
        headers=dict(check.headers or {}),
        body_text=check.body or "",
        body_is_json=bool(check.body_is_json),
        timeout_seconds=int(check.timeout_seconds or 15),
    )
    assertion_passed, assertion_reason = _evaluate_assertions(check, run_result)

    # Persist run row + update cached summary on the check.
    run_row = ApplicationApiCheckRun(
        api_check_id=check.id,
        ran_at=datetime.utcnow(),
        ran_by=current_user.user_id,
        ok=bool(run_result["ok"]) and (assertion_passed is not False),
        status_code=run_result["status_code"],
        duration_ms=int(run_result["duration_ms"]),
        response_headers=run_result["response_headers"],
        response_body=run_result["response_body"],
        error=run_result["error"] if run_result["error"] else (
            None if assertion_passed is not False else assertion_reason
        ),
        request_url=check.url,
        request_method=check.method,
    )
    session.add(run_row)

    check.last_run_at = run_row.ran_at
    check.last_run_ok = run_row.ok
    check.last_run_status_code = run_row.status_code
    check.last_run_duration_ms = run_row.duration_ms

    # Rolling retention: keep latest N rows per check. Old rows get
    # deleted in the same transaction so the table stays small.
    surplus_ids = [
        row.id for row in (
            session.query(ApplicationApiCheckRun.id)
            .filter(ApplicationApiCheckRun.api_check_id == check.id)
            .order_by(ApplicationApiCheckRun.ran_at.desc())
            .offset(_API_CHECK_RUN_HISTORY_PER_CHECK)
            .all()
        )
    ]
    if surplus_ids:
        session.query(ApplicationApiCheckRun).filter(
            ApplicationApiCheckRun.id.in_(surplus_ids)
        ).delete(synchronize_session=False)

    session.commit()
    session.refresh(run_row)
    return ApplicationApiCheckRunResult(
        run=ApplicationApiCheckRunResponse.model_validate(run_row),
        assertion_passed=assertion_passed,
        assertion_reason=assertion_reason,
    )


@router.get(
    "/api/api-checks/{check_id}/runs",
    response_model=List[ApplicationApiCheckRunResponse],
)
def list_application_api_check_runs(
    check_id: int,
    limit: int = Query(default=20, ge=1, le=_API_CHECK_RUN_HISTORY_PER_CHECK),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if session.query(ApplicationApiCheck).filter(ApplicationApiCheck.id == check_id).first() is None:
        raise NotFoundError("API check not found")
    rows = (
        session.query(ApplicationApiCheckRun)
        .filter(ApplicationApiCheckRun.api_check_id == check_id)
        .order_by(ApplicationApiCheckRun.ran_at.desc())
        .limit(limit)
        .all()
    )
    return [ApplicationApiCheckRunResponse.model_validate(r) for r in rows]
