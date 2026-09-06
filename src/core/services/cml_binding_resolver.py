"""Helpers for resolving CML v2 names (project / job / application) into ids.

Used by ``job_service`` and ``app_service`` at create/update time so the
monitoring loop and "check now" paths can later look up CML resources by id
without re-querying. Resolution is best-effort — if CML is unreachable or
the name doesn't exist, the helper returns ``None`` and the binding stays
unresolved. The checker layer is built to silently skip rows without
resolved ids, so a temporary CML outage during a write does not block the
write itself.

Single entry point for constructing the ControlInterface so the API key /
CA bundle / verify_ssl knobs come from one place.
"""

from __future__ import annotations

from typing import Optional

from core.config import get_config
from core.integrations.control_interface import CmlApiError, ControlInterface
from core.logging import get_logger

logger = get_logger(__name__)


def build_control_interface(*, timeout: Optional[float] = None) -> ControlInterface:
    cfg = get_config()
    return ControlInterface(
        base_url=cfg.cml_platform_base_url,
        api_key=cfg.cml_platform_api_key or None,
        timeout=float(timeout if timeout is not None else cfg.cml_platform_timeout_seconds),
        verify_ssl=cfg.cml_platform_verify_ssl,
        ca_bundle=cfg.cml_platform_ca_bundle_path or None,
        default_project_name=cfg.cml_platform_default_project_name or None,
    )


ResolveResult = tuple[Optional[str], Optional[str]]
"""``(resolved_id, error_message)``.

* ``(id, None)``        — CML returned the id.
* ``(None, None)``      — nothing to resolve (caller passed empty name).
* ``(None, "...")``     — CML was reached but failed; message is human-readable.
"""


def resolve_project_id(
    control: ControlInterface, project_name: Optional[str]
) -> ResolveResult:
    """Resolve a project name to id. Returns (id, error) tuple."""
    if not project_name:
        return None, None
    try:
        return control.resolve_project_id(project_name), None
    except CmlApiError as exc:
        msg = f"CML {exc.status_code or 'error'}: {exc.message}"
        logger.warning(
            "CML resolve_project_id('{}') failed (status={}): {}",
            project_name, exc.status_code, exc.message,
        )
        return None, msg
    except Exception as exc:
        logger.opt(exception=True).warning(
            "CML resolve_project_id('{}') raised unexpectedly", project_name
        )
        return None, f"unexpected error: {exc}"


def resolve_job_id(
    control: ControlInterface, project_id: Optional[str], job_name: Optional[str]
) -> ResolveResult:
    if not project_id or not job_name:
        return None, None
    try:
        return control.resolve_job_id(project_id, job_name), None
    except CmlApiError as exc:
        msg = f"CML {exc.status_code or 'error'}: {exc.message}"
        logger.warning(
            "CML resolve_job_id(project={}, name={}) failed (status={}): {}",
            project_id, job_name, exc.status_code, exc.message,
        )
        return None, msg
    except Exception as exc:
        logger.opt(exception=True).warning(
            "CML resolve_job_id(project={}, name={}) raised unexpectedly",
            project_id, job_name,
        )
        return None, f"unexpected error: {exc}"


def resolve_application_id(
    control: ControlInterface,
    project_id: Optional[str],
    *,
    name: Optional[str] = None,
    subdomain: Optional[str] = None,
) -> ResolveResult:
    if not project_id or not (name or subdomain):
        return None, None
    try:
        return (
            control.resolve_application_id(
                project_id, name=name or None, subdomain=subdomain or None
            ),
            None,
        )
    except CmlApiError as exc:
        msg = f"CML {exc.status_code or 'error'}: {exc.message}"
        logger.warning(
            "CML resolve_application_id(project={}, name={}, subdomain={}) failed "
            "(status={}): {}",
            project_id, name, subdomain, exc.status_code, exc.message,
        )
        return None, msg
    except Exception as exc:
        logger.opt(exception=True).warning(
            "CML resolve_application_id(project={}, name={}, subdomain={}) "
            "raised unexpectedly",
            project_id, name, subdomain,
        )
        return None, f"unexpected error: {exc}"


def build_serving_url(application_detail: Optional[dict], *, fallback: str = "") -> str:
    """Best-effort construction of the app serving URL.

    The CML doc (§1) gives the pattern
    ``https://<application-subdomain>.<cml-workspace-domain>/<path>`` but
    the workspace domain isn't carried in the application detail payload.
    Real production: derive ``<workspace>`` from ``cml_platform.base_url``
    by stripping the API path. For the local mock topology the user can
    just pass an explicit ``cml_serving_url`` and we honor it as-is.

    Returns the fallback unchanged if no detail is available.
    """
    if not application_detail:
        return fallback
    # When Ops is talking to real CML, the caller can override fallback with
    # a freshly-computed value; we simply return what we know without
    # silently overwriting an explicit user setting.
    return fallback or ""
