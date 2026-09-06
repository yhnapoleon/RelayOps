"""App Interface — CML application monitoring (binary up/down model).

Health is derived **solely** from the CML application lifecycle status read via
``ControlInterface.get_application(project_id, application_id)``:

  * APPLICATION_RUNNING / STARTING / STOPPING -> ``relayops_health = "up"``
  * APPLICATION_FAILED  / STOPPED             -> ``relayops_health = "down"``
  * APPLICATION_UNKNOWN / CML unreadable      -> ``relayops_health = "unknown"``

The serving-URL probe (and the per-app-type FastAPI/Runtime/Ray/Generic health
contracts) was removed on purpose: an app that CML reports RUNNING is "up"
regardless of what its own health endpoint returns. That probe was the source
of false negatives — the checker, lacking the browser's SSO session / trusted
CA, could fail to reach a serving URL that is actually fine, and a
schema-strict body check flagged perfectly-alive apps as "degraded". There is
no longer any "degraded" / "unhealthy" / "transient" tier.

The interface still returns a :class:`CanonicalAppHealthEvent` for every
outcome (including CML transport failures) so the AppChecker can make its
decision purely on ``event.relayops_health``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from core.integrations.control_interface import CmlApiError, ControlInterface
from core.integrations.normalization import (
    CanonicalAppHealthEvent,
    resolve_app_relayops_health,
)

logger = logging.getLogger(__name__)


@dataclass
class AppTarget:
    """Resolved binding for one Ops Application row.

    Attributes
    ----------
    app_id              : Ops primary key — used for log lines and counter scoping.
    product_id          : Ops product foreign key — propagated into AnomalyEvents.
    cml_project_id      : Resolved CML project id (Job/App's own binding takes
                          priority over the controller's default).
    cml_application_id  : Resolved CML application id.
    app_type            : "fastapi" | "runtime" | "ray" | "generic" — retained for
                          display / logging only; it no longer selects a health
                          contract because health is decided from the CML
                          lifecycle alone.
    serving_url         : Base serving URL (no trailing slash). Retained for
                          display / deep-links; no longer probed.
    """

    app_id: int
    product_id: int
    cml_project_id: str
    cml_application_id: str
    app_type: str
    serving_url: str


class AppInterface:
    """Single-phase health read: CML application lifecycle -> relayops_health."""

    def __init__(
        self,
        control_interface: ControlInterface,
        *,
        timeout: float = 10.0,
        verify_ssl: bool = True,
        ca_bundle: Optional[str] = None,
    ):
        self._control = control_interface
        # timeout / verify_ssl / ca_bundle are kept on the constructor for
        # backward compatibility with existing call sites; they were used by the
        # (now removed) serving-URL probe and no longer affect the CML read,
        # which goes through ControlInterface's own client.
        self.timeout = timeout
        if ca_bundle:
            self._verify: Any = ca_bundle
        else:
            self._verify = bool(verify_ssl)

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def check_health(self, target: AppTarget) -> CanonicalAppHealthEvent:
        """Read the CML lifecycle status and map it to a binary health verdict.

        Never raises — a CML read failure is reflected as ``relayops_health =
        "unknown"`` (a monitoring gap, not an app outage) so the AppChecker
        treats it as inconclusive rather than paging.
        """
        try:
            cml_app = self._control.get_application(
                target.cml_project_id, target.cml_application_id
            )
        except CmlApiError as exc:
            logger.warning(
                "AppInterface: CML get_application failed for app %s "
                "(project=%s, app_id=%s): %s",
                target.app_id,
                target.cml_project_id,
                target.cml_application_id,
                exc,
            )
            event = CanonicalAppHealthEvent(
                source_name=f"app_{target.app_id}",
                integration_mode="cml_unreachable",
                healthy=False,
                http_status=exc.status_code,
                normalized_metadata={
                    "error": "cml_get_application_failed",
                    "detail": exc.message,
                },
            )
            event.relayops_health = "unknown"
            return event

        cml_status = str(cml_app.get("status") or "APPLICATION_UNKNOWN")
        relayops_health = resolve_app_relayops_health(cml_status)
        return CanonicalAppHealthEvent(
            source_name=f"app_{target.app_id}",
            integration_mode=f"cml_{target.app_type or 'generic'}",
            healthy=(relayops_health == "up"),
            http_status=None,
            raw_payload=None,
            cml_status=cml_status,
            relayops_health=relayops_health,
            normalized_metadata={
                "cml_subdomain": cml_app.get("subdomain"),
                "cml_running_at": cml_app.get("running_at"),
                "cml_stopped_at": cml_app.get("stopped_at"),
                "decided_from": "cml_lifecycle",
            },
        )
