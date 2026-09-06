"""Mock CML Platform service (port 9000).

Speaks the CML v2 monitor-only contract documented in CML/job.md and
CML/app.md. All v2 endpoints are GET-only and require an
``Authorization: Bearer <token>`` header (any non-empty token is accepted
by the mock; the goal is to exercise Ops's auth-injection path, not to
simulate token validation).

v2 endpoints (CML contract):
    GET /api/v2/projectnames
    GET /api/v2/projects                                    (search by name)
    GET /api/v2/projects/{project_id}
    GET /api/v2/projects/{project_id}/jobs                  (search by name)
    GET /api/v2/projects/{project_id}/jobs/{job_id}
    GET /api/v2/projects/{project_id}/jobs/{job_id}/runs    (sort/filter)
    GET /api/v2/projects/{project_id}/jobs/{job_id}/runs/{run_id}
    GET /api/v2/projects/{project_id}/applications          (search by name/subdomain/status)
    GET /api/v2/projects/{project_id}/applications/{application_id}
    GET /api/v2/runtimes
    GET /api/v2/runtimeaddons
    GET /api/v2/workloadstatus, /api/v2/workloadtypes

Mock-only paths (NOT part of the real CML v2 contract, retained because
the dashboard needs a way to flip status during local testing):
    GET /control/status                    - dashboard read
    PUT /control/assets/{asset_id}         - flip job/app state
    GET /                                  - dashboard UI

Status enums (matching CML):
    job runs       : ENGINE_{SCHEDULING,STARTING,RUNNING,STOPPING,STOPPED,
                             SUCCEEDED,FAILED,TIMEOUT,SKIPPED,UNKNOWN}
    applications   : APPLICATION_{STARTING,RUNNING,STOPPING,STOPPED,
                                  FAILED,UNKNOWN}
"""

from __future__ import annotations

import json
import secrets
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_STATUSES = [
    "ENGINE_SCHEDULING",
    "ENGINE_STARTING",
    "ENGINE_RUNNING",
    "ENGINE_STOPPING",
    "ENGINE_STOPPED",
    "ENGINE_SUCCEEDED",
    "ENGINE_FAILED",
    "ENGINE_TIMEOUT",
    "ENGINE_SKIPPED",
    "ENGINE_UNKNOWN",
]
TERMINAL_JOB_STATUSES = {
    "ENGINE_STOPPED",
    "ENGINE_SUCCEEDED",
    "ENGINE_FAILED",
    "ENGINE_TIMEOUT",
    "ENGINE_SKIPPED",
}
APP_STATUSES = [
    "APPLICATION_STARTING",
    "APPLICATION_RUNNING",
    "APPLICATION_STOPPING",
    "APPLICATION_STOPPED",
    "APPLICATION_FAILED",
    "APPLICATION_UNKNOWN",
]

DEFAULT_RUNTIME = (
    "python:3.12-slim"
)
DEFAULT_ADDON = "hadoop-cli-7.1.7-1000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO-8601 timestamp in UTC with trailing 'Z' (matches CML samples)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hex_id() -> str:
    """`xxxx-xxxx-xxxx-xxxx` style id (matches CML samples like `tx7v-h867-v4db-1ug2`)."""
    h = secrets.token_hex(8)
    return f"{h[0:4]}-{h[4:8]}-{h[8:12]}-{h[12:16]}"


def _run_id() -> str:
    """16-char lowercase alphanumeric run id (matches CML samples)."""
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# State models
# ---------------------------------------------------------------------------


@dataclass
class CmlProject:
    id: str
    name: str
    slug: str
    visibility: str = "private"
    default_engine_type: str = "ml_runtime"
    creation_status: str = "CREATED"
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    permissions: dict = field(
        default_factory=lambda: {
            "read": True,
            "write": True,
            "admin": True,
            "business_user": True,
            "operator": True,
            "inherit": False,
        }
    )
    environment: dict = field(default_factory=dict)
    shared_memory_limit: int = 0
    ephemeral_storage_request: int = 0
    ephemeral_storage_limit: int = 0
    owner: dict = field(
        default_factory=lambda: {"username": "relayops-demo", "email": "relayops-demo@example.com"}
    )
    creator: dict = field(
        default_factory=lambda: {"username": "relayops-demo", "email": "relayops-demo@example.com"}
    )


@dataclass
class CmlJobRun:
    id: str
    project_id: str
    job_id: str
    status: str
    created_at: str
    scheduling_at: Optional[str] = None
    starting_at: Optional[str] = None
    running_at: Optional[str] = None
    finished_at: Optional[str] = None
    cpu: int = 4
    memory: int = 32
    nvidia_gpu: int = 0
    arguments: str = ""
    environment: str = '{"CDSW_APP_POLLING_ENDPOINT":"\\/\\/"}'
    runtime_identifier: str = DEFAULT_RUNTIME
    failure_reason: Optional[str] = None

    def to_api(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "scheduling_at": self.scheduling_at,
            "starting_at": self.starting_at,
            "running_at": self.running_at,
            "finished_at": self.finished_at,
            "cpu": self.cpu,
            "memory": self.memory,
            "nvidia_gpu": self.nvidia_gpu,
            "arguments": self.arguments,
            "environment": self.environment,
            "runtime_identifier": self.runtime_identifier,
            "failure_reason": self.failure_reason,
        }


@dataclass
class CmlJob:
    id: str
    project_id: str
    name: str
    script: str
    arguments: str = ""
    environment: dict = field(default_factory=dict)
    cpu: int = 4
    memory: int = 32
    nvidia_gpu: int = 0
    timeout: str = "0"
    timezone_name: str = "Asia/Singapore"
    runtime_identifier: str = DEFAULT_RUNTIME
    runtime_addon_identifiers: list = field(default_factory=lambda: [DEFAULT_ADDON])
    run_as: int = 0
    accelerator_label_id: str = "0"
    paused: bool = False
    schedule: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    # Mock-only: MMP linkage so Phase-1 Ops can still drive its drift gate.
    mmp_project_id: Optional[str] = None
    mmp_model_id: Optional[str] = None
    has_mmp_dependency: bool = False
    # Mock-only: status used when control panel appends a synthetic run.
    next_run_status: str = "ENGINE_SUCCEEDED"
    runs: list = field(default_factory=list)

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "script": self.script,
            "arguments": self.arguments,
            "cpu": self.cpu,
            "memory": self.memory,
            "nvidia_gpu": self.nvidia_gpu,
            "runtime_identifier": self.runtime_identifier,
            "runtime_addon_identifiers": self.runtime_addon_identifiers,
            "paused": self.paused,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_detail(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "script": self.script,
            "arguments": self.arguments,
            "environment": self.environment,
            "cpu": self.cpu,
            "memory": self.memory,
            "nvidia_gpu": self.nvidia_gpu,
            "timeout": self.timeout,
            "timezone": self.timezone_name,
            "schedule": self.schedule,
            "runtime_identifier": self.runtime_identifier,
            "runtime_addon_identifiers": self.runtime_addon_identifiers,
            "run_as": self.run_as,
            "accelerator_label_id": self.accelerator_label_id,
            "paused": self.paused,
        }


@dataclass
class CmlApplication:
    id: str
    project_id: str
    name: str
    subdomain: str
    script: str
    description: str = "No description for the app"
    status: str = "APPLICATION_RUNNING"
    cpu: int = 4
    memory: int = 32
    nvidia_gpu: int = 0
    bypass_authentication: bool = True
    runtime_identifier: str = DEFAULT_RUNTIME
    runtime_addon_identifiers: list = field(default_factory=lambda: [DEFAULT_ADDON])
    run_as: int = 0
    accelerator_label_id: str = "0"
    cdv_app: bool = False
    environment: str = '{"CDSW_APP_POLLING_ENDPOINT":"\\/\\/"}'
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    starting_at: Optional[str] = field(default_factory=_now_iso)
    running_at: Optional[str] = field(default_factory=_now_iso)
    stopped_at: Optional[str] = None
    # Mock-only: serving-URL routing for the dashboard health toggle.
    app_type: str = "generic"   # fastapi | runtime | ray | generic
    serving_host: str = ""
    serving_port: int = 0
    healthy: bool = True

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "script": self.script,
            "subdomain": self.subdomain,
            "status": self.status,
            "cpu": self.cpu,
            "memory": self.memory,
            "nvidia_gpu": self.nvidia_gpu,
            "bypass_authentication": self.bypass_authentication,
            "runtime_identifier": self.runtime_identifier,
            "runtime_addon_identifiers": self.runtime_addon_identifiers,
        }

    def to_detail(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "script": self.script,
            "subdomain": self.subdomain,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "starting_at": self.starting_at,
            "running_at": self.running_at,
            "stopped_at": self.stopped_at,
            "cpu": self.cpu,
            "memory": self.memory,
            "nvidia_gpu": self.nvidia_gpu,
            "bypass_authentication": self.bypass_authentication,
            "environment": self.environment,
            "runtime_identifier": self.runtime_identifier,
            "runtime_addon_identifiers": self.runtime_addon_identifiers,
            "run_as": self.run_as,
            "cdv_app": self.cdv_app,
            "accelerator_label_id": self.accelerator_label_id,
        }


@dataclass
class CmlPlatformState:
    projects: dict[str, CmlProject] = field(default_factory=dict)
    project_name_to_id: dict[str, str] = field(default_factory=dict)
    jobs: dict[str, CmlJob] = field(default_factory=dict)
    applications: dict[str, CmlApplication] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------


def _init_platform_state() -> CmlPlatformState:
    state = CmlPlatformState()

    project = CmlProject(
        id="proj-relayops-demo-0001",
        name="relayops-demo",
        slug="relayops-demo",
    )
    state.projects[project.id] = project
    state.project_name_to_id[project.name] = project.id

    base_t = datetime.now(timezone.utc)

    def _run(job_id: str, status: str, hours_ago: int) -> CmlJobRun:
        ts = (base_t - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")
        terminal = status in TERMINAL_JOB_STATUSES
        return CmlJobRun(
            id=_run_id(),
            project_id=project.id,
            job_id=job_id,
            status=status,
            created_at=ts,
            scheduling_at=ts,
            starting_at=ts,
            running_at=ts,
            finished_at=ts if terminal else None,
            failure_reason=(
                "Job failed during execution" if status == "ENGINE_FAILED" else None
            ),
        )

    def _add_job(
        name: str,
        script: str,
        schedule: str,
        mmp_project_id: Optional[str] = None,
        mmp_model_id: Optional[str] = None,
    ) -> CmlJob:
        job = CmlJob(
            id=_hex_id(),
            project_id=project.id,
            name=name,
            script=script,
            schedule=schedule,
            mmp_project_id=mmp_project_id,
            mmp_model_id=mmp_model_id,
            has_mmp_dependency=mmp_project_id is not None,
        )
        # Seed each job with one historical successful run so Ops's
        # last-run/staleness logic has something to anchor to.
        job.runs.append(_run(job.id, "ENGINE_SUCCEEDED", hours_ago=1))
        state.jobs[job.id] = job
        return job

    _add_job("hourly-data-sync", "scripts/hourly_sync.py", "0 * * * *")
    _add_job("daily-etl-pipeline", "scripts/daily_etl.py", "0 3 * * *")
    _add_job(
        "daily-model-training",
        "scripts/daily_train.py",
        "0 2 * * *",
        mmp_project_id="mmp-project-001",
        mmp_model_id="model-001",
    )
    _add_job(
        "weekly-model-retrain",
        "scripts/weekly_retrain.py",
        "0 4 * * 0",
        mmp_project_id="mmp-project-002",
        mmp_model_id="model-002",
    )

    def _add_app(
        name: str,
        subdomain: str,
        script: str,
        app_type: str,
        host: str,
        port: int,
    ) -> CmlApplication:
        app_obj = CmlApplication(
            id=_hex_id(),
            project_id=project.id,
            name=name,
            subdomain=subdomain,
            script=script,
            app_type=app_type,
            serving_host=host,
            serving_port=port,
        )
        state.applications[app_obj.id] = app_obj
        return app_obj

    _add_app("inventory-health-api", "inventorydemo", "app_fastapi.py",
             "fastapi", "mock-app-fastapi", 9001)
    _add_app("feature-store-cluster", "runtimetest", "app_runtime.py",
             "runtime", "mock-app-runtime", 9002)
    _add_app("model-serving-ray", "raytest", "app_ray.py",
             "ray", "mock-app-ray", 9003)

    return state


platform_state: CmlPlatformState = _init_platform_state()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Mock CML Platform (v2)",
    description=(
        "Speaks the CML v2 monitor-only contract for projects/jobs/applications. "
        "All /api/v2/* endpoints require an Authorization: Bearer header."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response capture (mock-only debugging surface)
# ---------------------------------------------------------------------------
#
# Records every CML-contract request (anything under /api/v2/* or
# /api/cml/*) along with its query parameters, the request headers we care
# about (Authorization is masked), the response status, and a truncated
# response body. The dashboard exposes the most recent ~50 entries so a
# developer can see exactly what Ops pulled and what CML returned.

RECENT_REQUESTS_CAP = 50
MAX_CAPTURED_BODY_CHARS = 4000

_CAPTURED_PATH_PREFIXES = ("/api/v2/", "/api/cml/")


@dataclass
class RequestCapture:
    """One captured request/response pair surfaced via /control/recent-requests."""

    sequence: int
    occurred_at: str
    method: str
    path: str
    query: dict
    headers: dict             # Authorization is masked to "Bearer ***"
    status_code: int
    duration_ms: float
    response_body: str        # truncated to MAX_CAPTURED_BODY_CHARS

    def to_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "method": self.method,
            "path": self.path,
            "query": self.query,
            "headers": self.headers,
            "status_code": self.status_code,
            "duration_ms": self.duration_ms,
            "response_body": self.response_body,
        }


_recent_requests: Deque[RequestCapture] = deque(maxlen=RECENT_REQUESTS_CAP)
_capture_seq: int = 0


def _should_capture(path: str) -> bool:
    return any(path.startswith(p) for p in _CAPTURED_PATH_PREFIXES)


def _mask_auth(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    if value.lower().startswith("bearer "):
        token = value.split(" ", 1)[1]
        if not token:
            return "Bearer (empty)"
        # Show last 4 chars so the developer can sanity-check token rotation
        # without leaking the full secret in the dashboard.
        suffix = token[-4:] if len(token) >= 4 else "***"
        return f"Bearer ***{suffix}"
    return "***"


_HEADER_DISPLAY_NAMES = {
    "authorization": "Authorization",
    "accept": "Accept",
    "content-type": "Content-Type",
    "user-agent": "User-Agent",
}


def _filtered_headers(raw: Any) -> dict:
    """Keep only headers a CML developer cares about; mask Authorization.

    Starlette/httpx surface header keys lowercase, so we normalize to the
    canonical display form before returning so the dashboard reads
    naturally (e.g. ``Authorization`` not ``authorization``).
    """
    out: dict = {}
    try:
        for key, value in raw.items():
            lk = key.lower()
            display = _HEADER_DISPLAY_NAMES.get(lk)
            if display is None:
                continue
            out[display] = _mask_auth(value) if lk == "authorization" else value
    except Exception:
        return {}
    return out


def _truncate(text: str, limit: int = MAX_CAPTURED_BODY_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...(truncated)"


@app.middleware("http")
async def capture_cml_requests(request: Request, call_next):
    """Record every /api/v2/* and /api/cml/* exchange for the dashboard."""
    global _capture_seq

    capture_this = _should_capture(request.url.path)
    started = datetime.now(timezone.utc)

    if not capture_this:
        return await call_next(request)

    response = await call_next(request)

    # Drain the streaming response body so we can both forward it AND keep
    # a copy. Without this, the body would already be consumed by FastAPI
    # before we got a chance to inspect it.
    body_chunks: list[bytes] = []
    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
        body_chunks.append(chunk)
    body_bytes = b"".join(body_chunks)
    try:
        body_text = body_bytes.decode("utf-8", errors="replace")
    except Exception:
        body_text = repr(body_bytes)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0

    _capture_seq += 1
    _recent_requests.append(
        RequestCapture(
            sequence=_capture_seq,
            occurred_at=started.isoformat().replace("+00:00", "Z"),
            method=request.method,
            path=request.url.path,
            query=dict(request.query_params),
            headers=_filtered_headers(request.headers),
            status_code=response.status_code,
            duration_ms=round(elapsed, 2),
            response_body=_truncate(body_text),
        )
    )

    return Response(
        content=body_bytes,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )


@app.get("/ping")
async def ping():
    """Liveness check for the mock itself (no auth required)."""
    return {"status": "ok", "service": "mock-cml-platform-v2"}


@app.get("/control/recent-requests")
async def control_recent_requests(limit: int = Query(default=20, ge=1, le=RECENT_REQUESTS_CAP)):
    """Return the most recent CML requests + responses captured by the mock.

    Mock-only debugging surface; not part of the real CML contract. Useful
    when the user clicks Ops's "Check Now" and wants to see exactly which
    CML calls were issued with what params, and what the mock returned.
    """
    captured = list(_recent_requests)
    # Most recent first, mirroring the dashboard layout.
    captured.reverse()
    return {
        "total_captured": len(captured),
        "max_buffer": RECENT_REQUESTS_CAP,
        "requests": [c.to_dict() for c in captured[:limit]],
    }


@app.delete("/control/recent-requests")
async def control_recent_requests_clear():
    """Clear the capture buffer (useful between test runs)."""
    _recent_requests.clear()
    return {"status": "ok", "cleared": True}


# ---------------------------------------------------------------------------
# v2 auth dependency + small helpers
# ---------------------------------------------------------------------------


def _require_bearer(authorization: Optional[str] = Header(None, alias="Authorization")) -> str:
    """Reject missing/blank Bearer tokens (mock accepts any non-empty token)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header (Bearer expected)",
        )
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty bearer token")
    return token


def _parse_filter(search_filter: Optional[str]) -> dict:
    if not search_filter:
        return {}
    try:
        parsed = json.loads(search_filter)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid search_filter (must be a JSON object): {exc}",
        )
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail="search_filter must be a JSON object",
        )
    return parsed


def _validate_id(value: str, kind: str) -> None:
    """Loose ID format check — surface 400 for empty/whitespace IDs (CML doc §9)."""
    if not value or value != value.strip() or any(c.isspace() for c in value):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {kind} ID format: {value!r}",
        )


# ===========================================================================
# CML v2 contract endpoints
# ===========================================================================


@app.get("/api/v2/projectnames")
async def list_project_names(
    search_filter: Optional[str] = Query(default=None),
    sort: Optional[str] = Query(default=None),
    page_size: int = Query(default=20),
    page_token: Optional[str] = Query(default=None),
    _token: str = Depends(_require_bearer),
):
    flt = _parse_filter(search_filter)
    name_filter = (flt.get("name") or "").lower()
    names = [
        p.name
        for p in platform_state.projects.values()
        if not name_filter or name_filter in p.name.lower()
    ]
    if sort == "name":
        names.sort()
    return {"project_names": names, "next_page_token": ""}


@app.get("/api/v2/projects")
async def list_projects(
    search_filter: Optional[str] = Query(default=None),
    sort: Optional[str] = Query(default=None),
    page_size: int = Query(default=20),
    page_token: Optional[str] = Query(default=None),
    include_public_projects: bool = Query(default=False),
    include_all_projects: bool = Query(default=False),
    _token: str = Depends(_require_bearer),
):
    flt = _parse_filter(search_filter)
    name_filter = (flt.get("name") or "").lower()
    items = [
        p
        for p in platform_state.projects.values()
        if not name_filter or name_filter in p.name.lower()
    ]
    if sort == "name":
        items.sort(key=lambda p: p.name)
    return {
        "projects": [
            {
                "id": p.id,
                "name": p.name,
                "slug": p.slug,
                "visibility": p.visibility,
                "default_engine_type": p.default_engine_type,
                "created_at": p.created_at,
                "updated_at": p.updated_at,
                "owner": p.owner,
                "creator": p.creator,
                "permissions": p.permissions,
            }
            for p in items
        ],
        "next_page_token": "",
    }


@app.get("/api/v2/projects/{project_id}")
async def get_project(project_id: str, _token: str = Depends(_require_bearer)):
    _validate_id(project_id, "project")
    p = platform_state.projects.get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return {
        "id": p.id,
        "name": p.name,
        "slug": p.slug,
        "visibility": p.visibility,
        "default_engine_type": p.default_engine_type,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "creation_status": p.creation_status,
        "owner": p.owner,
        "creator": p.creator,
        "permissions": p.permissions,
        "shared_memory_limit": p.shared_memory_limit,
        "environment": p.environment,
        "ephemeral_storage_request": p.ephemeral_storage_request,
        "ephemeral_storage_limit": p.ephemeral_storage_limit,
    }


@app.get("/api/v2/projects/{project_id}/jobs")
async def list_jobs(
    project_id: str,
    search_filter: Optional[str] = Query(default=None),
    sort: Optional[str] = Query(default=None),
    page_size: int = Query(default=20),
    page_token: Optional[str] = Query(default=None),
    _token: str = Depends(_require_bearer),
):
    _validate_id(project_id, "project")
    if project_id not in platform_state.projects:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    flt = _parse_filter(search_filter)
    name_filter = (flt.get("name") or "").lower()
    items = [
        j
        for j in platform_state.jobs.values()
        if j.project_id == project_id
        and (not name_filter or name_filter in j.name.lower())
    ]
    if sort == "name":
        items.sort(key=lambda j: j.name)
    return {"jobs": [j.to_summary() for j in items], "next_page_token": ""}


@app.get("/api/v2/projects/{project_id}/jobs/{job_id}")
async def get_job(project_id: str, job_id: str, _token: str = Depends(_require_bearer)):
    _validate_id(project_id, "project")
    _validate_id(job_id, "job")
    job = platform_state.jobs.get(job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' not found in project '{project_id}'",
        )
    return job.to_detail()


@app.get("/api/v2/projects/{project_id}/jobs/{job_id}/runs")
async def list_job_runs(
    project_id: str,
    job_id: str,
    search_filter: Optional[str] = Query(default=None),
    sort: Optional[str] = Query(default="-created_at"),
    page_size: int = Query(default=20),
    page_token: Optional[str] = Query(default=None),
    _token: str = Depends(_require_bearer),
):
    _validate_id(project_id, "project")
    _validate_id(job_id, "job")
    job = platform_state.jobs.get(job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' not found in project '{project_id}'",
        )

    flt = _parse_filter(search_filter)
    runs = list(job.runs)

    status_filter = flt.get("status")
    if status_filter:
        upper = str(status_filter).upper()
        if not upper.startswith("ENGINE_"):
            upper = "ENGINE_" + upper
        runs = [r for r in runs if r.status == upper]

    runs.sort(key=lambda r: r.created_at, reverse=(sort == "-created_at"))

    return {"job_runs": [r.to_api() for r in runs], "next_page_token": ""}


@app.get("/api/v2/projects/{project_id}/jobs/{job_id}/runs/{run_id}")
async def get_job_run(
    project_id: str,
    job_id: str,
    run_id: str,
    _token: str = Depends(_require_bearer),
):
    _validate_id(project_id, "project")
    _validate_id(job_id, "job")
    _validate_id(run_id, "run")
    job = platform_state.jobs.get(job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    for r in job.runs:
        if r.id == run_id:
            return r.to_api()
    raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")


@app.get("/api/v2/projects/{project_id}/applications")
async def list_applications(
    project_id: str,
    search_filter: Optional[str] = Query(default=None),
    sort: Optional[str] = Query(default=None),
    page_size: int = Query(default=20),
    page_token: Optional[str] = Query(default=None),
    _token: str = Depends(_require_bearer),
):
    _validate_id(project_id, "project")
    if project_id not in platform_state.projects:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    flt = _parse_filter(search_filter)
    name_filter = (flt.get("name") or "").lower()
    sub_filter = (flt.get("subdomain") or "").lower()
    status_filter = (flt.get("status") or "").lower()

    items: list[CmlApplication] = []
    for a in platform_state.applications.values():
        if a.project_id != project_id:
            continue
        if name_filter and name_filter not in a.name.lower():
            continue
        if sub_filter and sub_filter not in a.subdomain.lower():
            continue
        if status_filter:
            # CML doc §7 says status filter uses short values like "running"
            if not a.status.lower().endswith(status_filter):
                continue
        items.append(a)
    if sort == "name":
        items.sort(key=lambda a: a.name)

    return {"applications": [a.to_summary() for a in items], "next_page_token": ""}


@app.get("/api/v2/projects/{project_id}/applications/{application_id}")
async def get_application(
    project_id: str,
    application_id: str,
    _token: str = Depends(_require_bearer),
):
    _validate_id(project_id, "project")
    _validate_id(application_id, "application")
    a = platform_state.applications.get(application_id)
    if a is None or a.project_id != project_id:
        raise HTTPException(
            status_code=404,
            detail=f"Application '{application_id}' not found in project '{project_id}'",
        )
    return a.to_detail()


@app.get("/api/v2/runtimes")
async def list_runtimes(_token: str = Depends(_require_bearer)):
    return {
        "runtimes": [
            {
                "image_identifier": DEFAULT_RUNTIME,
                "editor": "openvscode",
                "kernel": "Python 3.10",
                "edition": "Standard",
                "description": "Demo standard ML runtime",
                "full_version": "3.7.0",
                "status": "ENABLED",
            }
        ],
        "next_page_token": "",
    }


@app.get("/api/v2/runtimeaddons")
async def list_runtime_addons(_token: str = Depends(_require_bearer)):
    return {
        "runtime_addons": [
            {
                "identifier": DEFAULT_ADDON,
                "name": "Hadoop CLI",
                "description": "Hadoop client tooling",
                "status": "ENABLED",
            }
        ],
        "next_page_token": "",
    }


@app.get("/api/v2/workloadstatus")
async def list_workload_status(_token: str = Depends(_require_bearer)):
    return {"workload_status": JOB_STATUSES + APP_STATUSES}


@app.get("/api/v2/workloadtypes")
async def list_workload_types(_token: str = Depends(_require_bearer)):
    return {"workload_type": ["application", "job", "session", "experiment"]}


# ===========================================================================
# Mock-only paths (NOT part of the real CML v2 contract)
# ===========================================================================


# ---------------------------------------------------------------------------
# Control panel (drives the dashboard)
# ---------------------------------------------------------------------------


class AssetStatusUpdate(BaseModel):
    status: Optional[str] = None
    healthy: Optional[bool] = None


@app.get("/control/status")
async def control_status():
    """Snapshot of all toggleable assets, surfaced for the dashboard."""
    assets: list[dict] = []
    for j in platform_state.jobs.values():
        last_run_status = j.runs[-1].status if j.runs else None
        assets.append(
            {
                "id": j.id,
                "type": "job",
                "name": j.name,
                "schedule": j.schedule,
                "next_run_status": j.next_run_status,
                "last_run_status": last_run_status,
                "has_mmp_dependency": j.has_mmp_dependency,
                "mmp_project_id": j.mmp_project_id,
                "mmp_model_id": j.mmp_model_id,
            }
        )
    for a in platform_state.applications.values():
        assets.append(
            {
                "id": a.id,
                "type": "app",
                "name": a.name,
                "app_type": a.app_type,
                "subdomain": a.subdomain,
                "host": a.serving_host,
                "port": a.serving_port,
                "status": a.status,
                "healthy": a.healthy,
            }
        )
    return {"status": "ok", "assets": assets, "total": len(assets)}


@app.put("/control/assets/{asset_id}")
async def control_update_asset(asset_id: str, body: AssetStatusUpdate):
    """Flip a job's next-run status (and append a run) or an app's healthy flag."""
    if asset_id in platform_state.jobs:
        job = platform_state.jobs[asset_id]
        if body.status is None:
            raise HTTPException(status_code=400, detail="status is required for jobs")
        normalized = str(body.status).upper()
        if not normalized.startswith("ENGINE_"):
            normalized = "ENGINE_" + normalized
        if normalized not in JOB_STATUSES:
            raise HTTPException(
                status_code=400, detail=f"unsupported status {body.status!r}"
            )
        ts = _now_iso()
        terminal = normalized in TERMINAL_JOB_STATUSES
        run = CmlJobRun(
            id=_run_id(),
            project_id=job.project_id,
            job_id=job.id,
            status=normalized,
            created_at=ts,
            scheduling_at=ts,
            starting_at=ts,
            running_at=ts,
            finished_at=ts if terminal else None,
            failure_reason=(
                "Job failed during execution" if normalized == "ENGINE_FAILED" else None
            ),
        )
        job.runs.append(run)
        job.next_run_status = normalized
        job.updated_at = ts
        return {
            "status": "ok",
            "asset_id": asset_id,
            "next_run_status": job.next_run_status,
            "run_id": run.id,
        }

    if asset_id in platform_state.applications:
        a = platform_state.applications[asset_id]
        if body.healthy is not None:
            new_healthy = body.healthy
        elif body.status is not None:
            new_healthy = (
                body.status.upper().endswith("RUNNING")
                or body.status.lower() in ("healthy", "active")
            )
        else:
            new_healthy = not a.healthy

        ts = _now_iso()
        a.healthy = new_healthy
        a.status = "APPLICATION_RUNNING" if new_healthy else "APPLICATION_FAILED"
        a.updated_at = ts
        if new_healthy:
            a.running_at = ts
            a.stopped_at = None
        else:
            a.stopped_at = ts

        # Best-effort: keep the underlying app's /control/health in sync so
        # Ops's serving-URL probe sees the same outcome.
        if a.serving_host and a.serving_port:
            url = f"http://{a.serving_host}:{a.serving_port}/control/health"
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.put(url, json={"healthy": new_healthy})
            except (httpx.HTTPError, httpx.ConnectError):
                pass

        return {
            "status": "ok",
            "asset_id": asset_id,
            "cml_status": a.status,
            "healthy": a.healthy,
        }

    raise HTTPException(status_code=404, detail=f"Asset '{asset_id}' not found")


# ---------------------------------------------------------------------------
# Dashboard UI
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return _build_dashboard_html()


def _build_dashboard_html() -> str:
    project = next(iter(platform_state.projects.values()), None)
    project_label = (
        f"{project.name} (<code>{project.id}</code>)" if project else "&mdash;"
    )

    job_rows = ""
    for job in platform_state.jobs.values():
        last_run = job.runs[-1] if job.runs else None
        last_status = last_run.status if last_run else "—"
        last_class = (
            "ok"
            if last_status == "ENGINE_SUCCEEDED"
            else "warn"
            if last_status in ("ENGINE_RUNNING", "ENGINE_SCHEDULING", "ENGINE_STARTING")
            else "danger"
            if last_status in ("ENGINE_FAILED", "ENGINE_TIMEOUT")
            else "neutral"
        )

        relayops_hints = (
            f'<small style="color:var(--muted)">'
            f'CML project_id: <code>{job.project_id}</code><br>'
            f'CML job_id: <code>{job.id}</code><br>'
            f'CML job name: <code>{job.name}</code><br>'
            f'Has MMP dependency: <code>{"Yes" if job.has_mmp_dependency else "No"}</code>'
            f"</small>"
        )
        if job.has_mmp_dependency:
            relayops_hints += (
                f'<br><small style="color:var(--muted)">'
                f"MMP project: <code>{job.mmp_project_id}</code> · "
                f"MMP model: <code>{job.mmp_model_id}</code></small>"
            )

        opts = "".join(
            f'<option value="{s}" {"selected" if job.next_run_status == s else ""}>{s}</option>'
            for s in JOB_STATUSES
        )

        job_rows += f"""
        <tr>
          <td><strong>{job.name}</strong><br><small>{job.id}</small></td>
          <td><span class="badge badge-{last_class}">{last_status}</span></td>
          <td><code>{job.schedule or "—"}</code></td>
          <td>
            <select onchange="setJobStatus('{job.id}', this.value)">{opts}</select>
          </td>
          <td>{relayops_hints}</td>
        </tr>"""

    app_rows = ""
    for a in platform_state.applications.values():
        cml_class = (
            "ok"
            if a.status == "APPLICATION_RUNNING"
            else "warn"
            if a.status in ("APPLICATION_STARTING", "APPLICATION_STOPPING")
            else "danger"
        )
        type_label = a.app_type.upper() if a.app_type else "GENERIC"
        url = f"http://{a.serving_host}:{a.serving_port}/" if a.serving_host else ""

        relayops_hints = (
            f'<small style="color:var(--muted)">'
            f"CML project_id: <code>{a.project_id}</code><br>"
            f"CML application_id: <code>{a.id}</code><br>"
            f"subdomain: <code>{a.subdomain}</code><br>"
            f"app_type: <code>{a.app_type}</code><br>"
            f"serving URL: <code>{url}</code>"
            f"</small>"
        )

        app_rows += f"""
        <tr>
          <td><strong>{a.name}</strong><br><small>{a.id}</small></td>
          <td><span class="badge badge-neutral">{type_label}</span></td>
          <td><span class="badge badge-{cml_class}">{a.status}</span></td>
          <td><a href="{url}" target="_blank"><code>{a.serving_host}:{a.serving_port}</code></a></td>
          <td>
            <button class="btn {"btn-danger" if a.healthy else "btn-ok"}"
              onclick="toggleApp('{a.id}', {str(not a.healthy).lower()})">
              Set {"Unhealthy" if a.healthy else "Healthy"}
            </button>
          </td>
          <td>{relayops_hints}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Mock CML Platform (v2)</title>
<style>
:root {{ --bg:#1a1b2e; --surface:#242640; --surface2:#2d2f4a;
  --text:#e2e4f0; --muted:#8b8fa8; --border:#3a3d5c; --accent:#6c63ff;
  --ok:#22c55e; --ok-bg:rgba(34,197,94,0.15);
  --danger:#ef4444; --danger-bg:rgba(239,68,68,0.15);
  --warn:#f59e0b; --warn-bg:rgba(245,158,11,0.15);
  --neutral-bg:rgba(139,143,168,0.15); }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:var(--bg); color:var(--text); line-height:1.6; padding:24px; }}
.container {{ max-width:1200px; margin:0 auto; }}
header {{ display:flex; justify-content:space-between; align-items:center;
  margin-bottom:32px; padding-bottom:16px; border-bottom:1px solid var(--border); }}
header h1 {{ font-size:24px; font-weight:700; }}
header .subtitle {{ color:var(--muted); font-size:14px; }}
.refresh-btn {{ background:var(--accent); color:#fff; border:none; padding:8px 16px;
  border-radius:8px; cursor:pointer; font-size:14px; font-weight:600; }}
section {{ margin-bottom:32px; }}
section h2 {{ font-size:18px; margin-bottom:16px; padding-bottom:8px;
  border-bottom:1px solid var(--border); }}
table {{ width:100%; border-collapse:collapse; background:var(--surface);
  border-radius:12px; overflow:hidden; }}
th, td {{ padding:12px 16px; text-align:left; vertical-align:top; }}
th {{ background:var(--surface2); font-size:12px; text-transform:uppercase;
  letter-spacing:0.05em; color:var(--muted); font-weight:700; }}
tr {{ border-bottom:1px solid var(--border); }}
tr:last-child {{ border-bottom:none; }}
tr:hover {{ background:var(--surface2); }}
.badge {{ display:inline-block; padding:4px 10px; border-radius:999px;
  font-size:12px; font-weight:700; }}
.badge-ok {{ background:var(--ok-bg); color:var(--ok); }}
.badge-danger {{ background:var(--danger-bg); color:var(--danger); }}
.badge-warn {{ background:var(--warn-bg); color:var(--warn); }}
.badge-neutral {{ background:var(--neutral-bg); color:var(--muted); }}
select {{ background:var(--surface2); color:var(--text); border:1px solid var(--border);
  padding:6px 10px; border-radius:6px; font-size:13px; cursor:pointer; }}
input[type="number"] {{ background:var(--surface2); color:var(--text);
  border:1px solid var(--border); padding:4px 8px; border-radius:6px;
  font-size:13px; width:70px; }}
.btn {{ border:none; padding:6px 12px; border-radius:6px; font-size:13px;
  font-weight:600; cursor:pointer; }}
.btn:hover {{ opacity:0.85; }}
.btn-danger {{ background:var(--danger); color:#fff; }}
.btn-ok {{ background:var(--ok); color:#fff; }}
a {{ color:var(--accent); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
code {{ font-size:12px; color:var(--muted); }}
small {{ color:var(--muted); font-size:11px; }}
.toast {{ position:fixed; bottom:20px; right:20px; padding:12px 20px;
  background:var(--surface2); border:1px solid var(--border);
  border-radius:8px; font-size:14px; opacity:0; transition:opacity 0.3s;
  pointer-events:none; z-index:1000; }}
.toast.show {{ opacity:1; }}
.status-bar {{ display:flex; gap:16px; margin-bottom:24px; flex-wrap:wrap; }}
.stat-card {{ background:var(--surface); border:1px solid var(--border);
  border-radius:10px; padding:12px 18px; min-width:140px; }}
.stat-card .label {{ font-size:12px; color:var(--muted); }}
.stat-card .value {{ font-size:20px; font-weight:700; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>Mock CML Platform (v2)</h1>
      <div class="subtitle">Project: {project_label} · Port 9000 · v2 monitor-only contract</div>
    </div>
    <button class="refresh-btn" onclick="location.reload()">Refresh</button>
  </header>

  <div class="status-bar">
    <div class="stat-card"><div class="label">Projects</div><div class="value">{len(platform_state.projects)}</div></div>
    <div class="stat-card"><div class="label">Jobs</div><div class="value">{len(platform_state.jobs)}</div></div>
    <div class="stat-card"><div class="label">Applications</div><div class="value">{len(platform_state.applications)}</div></div>
  </div>

  <section>
    <h2>Jobs</h2>
    <table>
      <thead><tr>
        <th>Name / id</th><th>Last run</th><th>Cron</th>
        <th>Append run with status / Drift control</th><th>Ops binding hints</th>
      </tr></thead>
      <tbody>{job_rows}</tbody>
    </table>
  </section>

  <section>
    <h2>Applications</h2>
    <table>
      <thead><tr>
        <th>Name / id</th><th>Type</th><th>CML status</th>
        <th>Serving URL</th><th>Toggle</th><th>Ops binding hints</th>
      </tr></thead>
      <tbody>{app_rows}</tbody>
    </table>
  </section>

  <section>
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
      <h2 style="margin:0; padding:0; border:none;">Recent CML Requests</h2>
      <div style="display:flex; gap:8px;">
        <span class="badge badge-neutral" id="capture-badge">0 captured</span>
        <button class="refresh-btn" style="padding:4px 10px; font-size:12px;" onclick="refreshCaptures()">Refresh</button>
        <button class="refresh-btn" style="padding:4px 10px; font-size:12px; background:var(--danger);" onclick="clearCaptures()">Clear</button>
      </div>
    </div>
    <div id="captures" style="display:flex; flex-direction:column; gap:8px;">
      <div style="color:var(--muted); font-size:13px;">Loading...</div>
    </div>
  </section>
</div>

<div class="toast" id="toast"></div>
<script>
function showToast(msg) {{
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}}
async function setJobStatus(assetId, status) {{
  try {{
    const resp = await fetch('/control/assets/' + assetId, {{
      method: 'PUT', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{status: status}})
    }});
    if (resp.ok) {{ showToast('Job ' + assetId + ' → ' + status); setTimeout(() => location.reload(), 500); }}
    else {{ showToast('Error: ' + resp.status); }}
  }} catch(e) {{ showToast('Error: ' + e.message); }}
}}
async function toggleApp(assetId, healthy) {{
  try {{
    const resp = await fetch('/control/assets/' + assetId, {{
      method: 'PUT', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{healthy: healthy}})
    }});
    if (resp.ok) {{ showToast('App ' + assetId + ' → ' + (healthy ? 'healthy' : 'unhealthy')); setTimeout(() => location.reload(), 500); }}
    else {{ showToast('Error: ' + resp.status); }}
  }} catch(e) {{ showToast('Error: ' + e.message); }}
}}
// ── Recent CML Requests panel ───────────────────────────────────────────
function escapeHtml(s) {{
  if (s === null || s === undefined) return '';
  return String(s)
    .replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')
    .replaceAll('"','&quot;').replaceAll("'",'&#39;');
}}
function prettyJson(text) {{
  if (!text) return '';
  try {{ return JSON.stringify(JSON.parse(text), null, 2); }} catch (_) {{ return text; }}
}}
function statusClass(code) {{
  if (code >= 200 && code < 300) return 'ok';
  if (code >= 400 && code < 500) return 'warn';
  return 'danger';
}}
async function refreshCaptures() {{
  try {{
    const resp = await fetch('/control/recent-requests?limit=20');
    if (!resp.ok) {{ showToast('Failed to load captures: ' + resp.status); return; }}
    const data = await resp.json();
    const badge = document.getElementById('capture-badge');
    badge.textContent = data.total_captured + ' captured (max ' + data.max_buffer + ')';
    const container = document.getElementById('captures');
    if (!data.requests.length) {{
      container.innerHTML = '<div style="color:var(--muted); font-size:13px;">No CML requests captured yet — trigger Ops\\'s "Check Now" or wait for the polling tick.</div>';
      return;
    }}
    container.innerHTML = data.requests.map(c => {{
      const queryLine = Object.keys(c.query).length
        ? Object.entries(c.query).map(([k, v]) => escapeHtml(k) + '=' + escapeHtml(v)).join('  ')
        : '(none)';
      const headersHtml = Object.entries(c.headers || {{}})
        .map(([k, v]) => '<div><span style="color:var(--muted)">' + escapeHtml(k) + ':</span> <code>' + escapeHtml(v) + '</code></div>')
        .join('');
      return '<details style="background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:12px;">'
        + '<summary style="cursor:pointer; display:flex; justify-content:space-between; align-items:center; gap:12px;">'
        +   '<div style="display:flex; align-items:center; gap:10px;">'
        +     '<span class="badge badge-' + statusClass(c.status_code) + '">' + c.status_code + '</span>'
        +     '<strong style="color:var(--text)">' + escapeHtml(c.method) + ' ' + escapeHtml(c.path) + '</strong>'
        +   '</div>'
        +   '<div style="display:flex; gap:8px; align-items:center; color:var(--muted); font-size:11px;">'
        +     '<span>#' + c.sequence + '</span>'
        +     '<span>' + c.duration_ms + 'ms</span>'
        +     '<span>' + escapeHtml(c.occurred_at) + '</span>'
        +   '</div>'
        + '</summary>'
        + '<div style="margin-top:12px; display:grid; grid-template-columns:1fr 1fr; gap:14px;">'
        +   '<div>'
        +     '<div style="font-size:11px; text-transform:uppercase; color:var(--muted); margin-bottom:4px;">Request</div>'
        +     '<div style="font-size:12px; line-height:1.6;"><div><span style="color:var(--muted)">query:</span> <code>' + queryLine + '</code></div>' + headersHtml + '</div>'
        +   '</div>'
        +   '<div>'
        +     '<div style="font-size:11px; text-transform:uppercase; color:var(--muted); margin-bottom:4px;">Response body</div>'
        +     '<pre style="margin:0; padding:10px; background:#0f172a; color:#dbeafe; border-radius:8px; font-size:11px; max-height:280px; overflow:auto; white-space:pre-wrap;">' + escapeHtml(prettyJson(c.response_body)) + '</pre>'
        +   '</div>'
        + '</div>'
        + '</details>';
    }}).join('');
  }} catch (e) {{
    showToast('Capture load failed: ' + e.message);
  }}
}}
async function clearCaptures() {{
  try {{
    const resp = await fetch('/control/recent-requests', {{ method: 'DELETE' }});
    if (resp.ok) {{ showToast('Capture buffer cleared'); refreshCaptures(); }}
  }} catch (e) {{ showToast('Clear failed: ' + e.message); }}
}}

// Initial load + auto-refresh every 5s.
refreshCaptures();
setInterval(refreshCaptures, 5000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9000)
