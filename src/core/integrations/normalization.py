from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


class NormalizationError(ValueError):
    pass


CONTROL_M_STATUS_MAP = {
    # Legacy short statuses (pre-Phase-2 mock + various vendor profiles).
    "completed": "completed",
    "success": "completed",
    "succeeded": "completed",
    "ok": "completed",
    "ended ok": "completed",
    "failed": "failed",
    "fail": "failed",
    "error": "failed",
    "ended not ok": "failed",
    "not ok": "failed",
    "running": "running",
    "executing": "running",
    "in progress": "running",
    "waiting": "waiting",
    "queued": "waiting",
    "held": "waiting",
    # CML v2 ENGINE_* statuses.
    "engine_succeeded": "completed",
    "engine_failed": "failed",
    "engine_running": "running",
    "engine_starting": "running",
    "engine_scheduling": "waiting",
    "engine_stopping": "running",
    "engine_stopped": "stopped",
    "engine_timeout": "failed",
    "engine_skipped": "skipped",
    "engine_unknown": "unknown",
}


# CML v2 application status -> Ops lifecycle bucket.
APPLICATION_STATUS_MAP = {
    "application_starting": "starting",
    "application_running": "running",
    "application_stopping": "stopping",
    "application_stopped": "stopped",
    "application_failed": "failed",
    "application_unknown": "unknown",
}


@dataclass
class CanonicalAppHealthEvent:
    source_type: str = "app_health"
    source_name: str = "unknown"
    integration_mode: str = "unknown"
    healthy: bool = False
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    http_status: Optional[int] = None
    raw_payload: Any = None
    normalized_metadata: Dict[str, Any] = field(default_factory=dict)
    # CML v2 fields (populated when the caller passes ``cml_status``).
    cml_status: Optional[str] = None      # raw "APPLICATION_RUNNING" etc.
    relayops_health: Optional[str] = None      # healthy / degraded / unhealthy /
                                          # starting / stopping / stopped /
                                          # failed / unknown.

    def to_preview(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_name": self.source_name,
            "integration_mode": self.integration_mode,
            "healthy": self.healthy,
            "occurred_at": self.occurred_at.isoformat(),
            "http_status": self.http_status,
            "raw_payload": self.raw_payload,
            "normalized_metadata": self.normalized_metadata,
            "cml_status": self.cml_status,
            "relayops_health": self.relayops_health,
        }


@dataclass
class CanonicalControlMJobStatus:
    job_name: str
    normalized_status: str
    last_run: Optional[str] = None
    cron: Optional[str] = None
    duration_seconds: Optional[int] = None
    external_id: Optional[str] = None
    raw_record: Dict[str, Any] = field(default_factory=dict)
    normalized_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_preview(self) -> Dict[str, Any]:
        return {
            "job_name": self.job_name,
            "normalized_status": self.normalized_status,
            "last_run": self.last_run,
            "cron": self.cron,
            "duration_seconds": self.duration_seconds,
            "external_id": self.external_id,
            "raw_record": self.raw_record,
            "normalized_metadata": self.normalized_metadata,
        }


@dataclass
class CanonicalControlMBatchEvent:
    source_type: str = "controlm_jobs"
    source_name: str = "unknown"
    integration_mode: str = "unknown"
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    jobs: List[CanonicalControlMJobStatus] = field(default_factory=list)
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    normalized_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_preview(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_name": self.source_name,
            "integration_mode": self.integration_mode,
            "occurred_at": self.occurred_at.isoformat(),
            "jobs": [job.to_preview() for job in self.jobs],
            "raw_payload": self.raw_payload,
            "normalized_metadata": self.normalized_metadata,
        }


def _parse_datetime(value: Any) -> datetime:
    if value is None or value == "":
        return datetime.utcnow()
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    raise NormalizationError(f"Unsupported datetime value: {value!r}")


def _normalize_controlm_status(value: Any) -> str:
    if value is None:
        raise NormalizationError("Missing Control-M job status")
    normalized = CONTROL_M_STATUS_MAP.get(str(value).strip().lower())
    if normalized is None:
        raise NormalizationError(f"Unsupported Control-M status value: {value!r}")
    return normalized


def _resolve_app_health_profile(payload: Any, profile_hint: Optional[str]) -> str:
    if profile_hint:
        return profile_hint
    if isinstance(payload, dict):
        # CML v2 app contracts — keyed by app_type.
        app_type = str(payload.get("app_type") or "").lower()
        if app_type == "fastapi":
            return "cml_fastapi"
        if app_type == "ray":
            return "cml_ray"
        if app_type == "runtime":
            return "cml_runtime"
        # AppInterface composes a {"health": ..., "config": ...} envelope when
        # probing Runtime (because Runtime needs both /health and /runtime/config to
        # pass). Detect that envelope here.
        if (
            isinstance(payload.get("health"), dict)
            and str(payload.get("health", {}).get("app_type") or "").lower() == "runtime"
        ):
            return "cml_runtime"
        if "status" in payload and "service" in payload:
            return "relayops_native"
        if "ok" in payload and "app" in payload:
            return "vendor_a_status"
        if isinstance(payload.get("result"), dict) and "state" in payload.get("result", {}):
            return "vendor_b_probe"
    if isinstance(payload, str):
        return "legacy_text"
    raise NormalizationError("Could not resolve app health profile")


# Binary up/down health model. App health is derived *solely* from the CML
# application lifecycle — the serving-URL probe no longer influences the
# verdict. There is no "degraded"/"unhealthy"/"transient" tier: an app is
# either up (process running / coming up / winding down) or down (CML reports a
# terminal failed/stopped lifecycle). "unknown" is reserved for "we couldn't
# read CML" and never on its own creates an Issue.
_UP_LIFECYCLE_BUCKETS = {"running", "starting", "stopping"}
_DOWN_LIFECYCLE_BUCKETS = {"failed", "stopped"}


def resolve_app_relayops_health(
    cml_status: Optional[str],
    *,
    contract_pass: bool = True,
    transport_failed: bool = False,
) -> str:
    """Map a CML application lifecycle status to the binary Ops health verdict.

    Returns one of ``"up"`` / ``"down"`` / ``"unknown"``:

      * running / starting / stopping        -> ``"up"``   (no Issue)
      * failed / stopped                     -> ``"down"``  (APP_OFFLINE Issue)
      * unreadable / unmapped (``None`` etc.) -> ``"unknown"`` (no Issue)

    ``contract_pass`` / ``transport_failed`` are accepted for backward
    compatibility with older callers but are **ignored** — health is no longer
    a function of the serving-URL probe, so a running app is "up" regardless of
    what (if anything) its health endpoint returns.
    """
    if not cml_status:
        return "unknown"
    bucket = APPLICATION_STATUS_MAP.get(str(cml_status).strip().lower())
    if bucket in _UP_LIFECYCLE_BUCKETS:
        return "up"
    if bucket in _DOWN_LIFECYCLE_BUCKETS:
        return "down"
    return "unknown"


def _attach_relayops_health(
    event: CanonicalAppHealthEvent,
    cml_status: Optional[str],
    status_code: Optional[int],
) -> None:
    """Populate ``event.cml_status`` and ``event.relayops_health``.

    Treats a missing/non-2xx ``status_code`` as a transport failure for the
    RUNNING-but-not-passing case, so it maps to ``unhealthy`` rather than
    ``degraded``.
    """
    if not cml_status:
        return
    event.cml_status = cml_status
    transport_failed = status_code is None or status_code >= 400
    event.relayops_health = resolve_app_relayops_health(
        cml_status,
        contract_pass=event.healthy,
        transport_failed=transport_failed,
    )


def normalize_app_health_payload(
    payload: Any,
    *,
    status_code: Optional[int] = None,
    profile_hint: Optional[str] = None,
    source_name: Optional[str] = None,
    cml_status: Optional[str] = None,
) -> CanonicalAppHealthEvent:
    profile = _resolve_app_health_profile(payload, profile_hint)
    event = CanonicalAppHealthEvent(
        source_name=source_name or (payload.get("source") if isinstance(payload, dict) else None) or "mock_app",
        integration_mode=profile,
        http_status=status_code,
        raw_payload=payload,
    )

    if profile == "cml_fastapi":
        # FastAPI: status=healthy AND app_type=fastapi.
        is_healthy = (
            isinstance(payload, dict)
            and str(payload.get("status") or "").lower() == "healthy"
            and str(payload.get("app_type") or "").lower() == "fastapi"
        )
        event.healthy = bool(is_healthy)
        if status_code is not None and status_code >= 400:
            event.healthy = False
        if isinstance(payload, dict):
            event.normalized_metadata = {
                "hostname": payload.get("hostname"),
                "timestamp": payload.get("timestamp"),
            }
        _attach_relayops_health(event, cml_status, status_code)
        return event

    if profile == "cml_runtime":
        # Runtime: caller may pass either a single /health response, or an
        # envelope {"health": ..., "config": ...} combining both endpoints.
        health_data: Any
        config_data: Any
        if (
            isinstance(payload, dict)
            and "health" in payload
            and "config" in payload
        ):
            health_data = payload.get("health") or {}
            config_data = payload.get("config") or {}
        else:
            health_data = payload if isinstance(payload, dict) else {}
            config_data = None

        health_ok = (
            isinstance(health_data, dict)
            and str(health_data.get("status") or "").lower() == "healthy"
            and bool(health_data.get("runtime_imported"))
            and bool(health_data.get("config_loaded"))
            and not health_data.get("runtime_import_error")
            and not health_data.get("config_error")
        )
        config_ok = (
            config_data is None  # /runtime/config was not probed; just trust /health
            or (
                isinstance(config_data, dict)
                and str(config_data.get("status") or "").lower() == "ok"
            )
        )
        event.healthy = bool(health_ok and config_ok)
        if status_code is not None and status_code >= 400:
            event.healthy = False
        if isinstance(health_data, dict):
            event.normalized_metadata = {
                "runtime_imported": health_data.get("runtime_imported"),
                "config_loaded": health_data.get("config_loaded"),
                "runtime_import_error": health_data.get("runtime_import_error"),
                "config_error": health_data.get("config_error"),
                "config_probed": config_data is not None,
            }
        _attach_relayops_health(event, cml_status, status_code)
        return event

    if profile == "cml_ray":
        # Ray: full_test=passed, ray imported & initialized, alive_nodes>=1,
        # all five sub-checks (status, task, object_store, parallel_tasks,
        # actor) pass.
        if not isinstance(payload, dict):
            event.healthy = False
            _attach_relayops_health(event, cml_status, status_code)
            return event
        results = payload.get("results") if isinstance(payload.get("results"), dict) else {}
        status_check = results.get("status") if isinstance(results.get("status"), dict) else {}

        full_passed = str(payload.get("full_test") or "").lower() == "passed"
        ray_imported = bool(payload.get("ray_imported"))
        ray_initialized = bool(payload.get("ray_initialized"))
        try:
            alive_nodes = int(status_check.get("alive_nodes_count") or 0)
        except (TypeError, ValueError):
            alive_nodes = 0
        sub_checks_pass = all(
            isinstance(results.get(name), dict)
            and bool(results.get(name, {}).get("passed"))
            for name in ("status", "task", "object_store", "parallel_tasks", "actor")
        )

        event.healthy = bool(
            full_passed
            and ray_imported
            and ray_initialized
            and alive_nodes >= 1
            and sub_checks_pass
        )
        if status_code is not None and status_code >= 400:
            event.healthy = False
        event.normalized_metadata = {
            "ray_version": payload.get("ray_version"),
            "alive_nodes_count": alive_nodes,
            "task_passed": (
                bool(results.get("task", {}).get("passed"))
                if isinstance(results.get("task"), dict)
                else None
            ),
            "actor_final_value": (
                results.get("actor", {}).get("final_value")
                if isinstance(results.get("actor"), dict)
                else None
            ),
            "full_test": payload.get("full_test"),
        }
        _attach_relayops_health(event, cml_status, status_code)
        return event

    if profile == "cml_generic":
        # Generic: any 2xx response with non-empty body is treated as PASS;
        # caller can override via profile_hint when more is known.
        is_2xx = status_code is None or 200 <= status_code < 300
        event.healthy = bool(is_2xx and payload is not None)
        _attach_relayops_health(event, cml_status, status_code)
        return event

    if profile == "relayops_native":
        status_text = str(payload.get("status") or "").strip().lower()
        event.healthy = status_text in {"healthy", "ok", "up"}
        event.normalized_metadata = {"service": payload.get("service")}
        if status_code is not None and status_code >= 400:
            event.healthy = False
        return event

    if profile == "vendor_a_status":
        event.healthy = bool(payload.get("ok"))
        event.occurred_at = _parse_datetime(payload.get("checkedAt")) if payload.get("checkedAt") else datetime.utcnow()
        event.normalized_metadata = {
            "app": payload.get("app"),
            "latency_ms": payload.get("latencyMs"),
            "ignored_fields_present": sorted(set(payload.keys()) - {"ok", "app", "checkedAt", "latencyMs"}),
        }
        if status_code is not None and status_code >= 400:
            event.healthy = False
        return event

    if profile == "vendor_b_probe":
        result = payload.get("result") or {}
        state = str(result.get("state") or "").strip().lower()
        event.healthy = state in {"up", "healthy", "ok"}
        event.normalized_metadata = {
            "system": payload.get("system"),
            "latency_ms": result.get("latencyMs"),
            "reason": result.get("reason"),
        }
        if status_code is not None and status_code >= 400:
            event.healthy = False
        return event

    if profile == "legacy_text":
        text = str(payload).strip().lower()
        event.healthy = text in {"ok", "healthy", "up"}
        event.normalized_metadata = {"raw_text": str(payload)}
        if status_code is not None and status_code >= 400:
            event.healthy = False
        return event

    raise NormalizationError(f"Unsupported app health profile: {profile}")


def _resolve_controlm_profile(payload: Dict[str, Any], profile_hint: Optional[str]) -> str:
    if profile_hint:
        return profile_hint
    if isinstance(payload.get("jobs"), dict):
        return "relayops_native"
    if isinstance(payload.get("records"), list):
        return "vendor_a_batch"
    if isinstance(payload.get("data"), dict) and isinstance(payload.get("data", {}).get("jobs"), list):
        return "vendor_b_schedule"
    if isinstance(payload.get("jobs"), list):
        # Distinguish between relayops_native (list with "name"/"status" keys) and legacy_list ("job"/"result" keys)
        jobs_list = payload["jobs"]
        if jobs_list and isinstance(jobs_list[0], dict) and "name" in jobs_list[0]:
            return "relayops_native"
        return "legacy_list"
    raise NormalizationError("Could not resolve Control-M profile")


def normalize_controlm_jobs_payload(
    payload: Dict[str, Any],
    *,
    profile_hint: Optional[str] = None,
    source_name: Optional[str] = None,
) -> CanonicalControlMBatchEvent:
    profile = _resolve_controlm_profile(payload, profile_hint)
    event = CanonicalControlMBatchEvent(
        source_name=source_name or payload.get("source") or "mock_controlm",
        integration_mode=profile,
        raw_payload=payload,
    )

    if profile == "relayops_native":
        jobs: List[CanonicalControlMJobStatus] = []
        jobs_payload = payload.get("jobs") or {}
        if isinstance(jobs_payload, dict):
            for job_name, record in jobs_payload.items():
                record = record or {}
                jobs.append(
                    CanonicalControlMJobStatus(
                        job_name=job_name,
                        normalized_status=_normalize_controlm_status(record.get("status") or "waiting"),
                        last_run=record.get("last_run"),
                        cron=record.get("cron"),
                        duration_seconds=record.get("duration_seconds"),
                        raw_record=record,
                    )
                )
        elif isinstance(jobs_payload, list):
            for record in jobs_payload:
                record = record or {}
                jobs.append(
                    CanonicalControlMJobStatus(
                        job_name=record.get("name") or record.get("job_name"),
                        normalized_status=_normalize_controlm_status(record.get("status") or "waiting"),
                        last_run=record.get("last_run"),
                        cron=record.get("cron"),
                        duration_seconds=record.get("duration_seconds"),
                        raw_record=record,
                    )
                )
        event.jobs = jobs
        event.normalized_metadata = {"kind": payload.get("kind")}
        return event

    if profile == "vendor_a_batch":
        jobs = []
        for record in payload.get("records") or []:
            jobs.append(
                CanonicalControlMJobStatus(
                    job_name=record.get("jobName"),
                    normalized_status=_normalize_controlm_status(record.get("executionStatus")),
                    last_run=record.get("lastEndedAt"),
                    cron=record.get("schedule"),
                    duration_seconds=record.get("durationSec"),
                    external_id=record.get("runId"),
                    raw_record=record,
                    normalized_metadata={"queue": record.get("queue"), "owner": record.get("owner")},
                )
            )
        event.jobs = jobs
        event.normalized_metadata = {"record_count": len(jobs)}
        return event

    if profile == "vendor_b_schedule":
        jobs = []
        for record in payload.get("data", {}).get("jobs") or []:
            jobs.append(
                CanonicalControlMJobStatus(
                    job_name=record.get("name"),
                    normalized_status=_normalize_controlm_status(record.get("state")),
                    last_run=record.get("completedAt"),
                    cron=record.get("cronExpr"),
                    duration_seconds=record.get("runtimeSeconds"),
                    external_id=record.get("executionId"),
                    raw_record=record,
                    normalized_metadata={"host": record.get("host"), "group": record.get("group")},
                )
            )
        event.jobs = jobs
        event.normalized_metadata = {"provider": payload.get("provider")}
        return event

    if profile == "legacy_list":
        jobs = []
        for record in payload.get("jobs") or []:
            jobs.append(
                CanonicalControlMJobStatus(
                    job_name=record.get("job"),
                    normalized_status=_normalize_controlm_status(record.get("result")),
                    last_run=record.get("ts"),
                    cron=record.get("cron"),
                    duration_seconds=record.get("duration"),
                    external_id=record.get("id"),
                    raw_record=record,
                )
            )
        event.jobs = jobs
        return event

    raise NormalizationError(f"Unsupported Control-M profile: {profile}")
