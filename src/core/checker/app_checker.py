"""APP Checker — binary up/down app health policy.

App health is a two-state model driven purely by the CML application lifecycle
(see AppInterface): an app is either **up** or **down**. There is no
"degraded"/"unhealthy"/"transient" tier — a serving-URL probe that returns the
"wrong" body, or that the checker simply can't reach, no longer marks an app
unhealthy.

  relayops_health (from AppInterface)        | CheckResult
  --------------------------------------+----------------------------------
  up (CML running/starting/stopping)    | HEALTHY (reset counter)
  down (CML failed/stopped)             | bump counter; ANOMALY at threshold
  unknown (CML unreadable)              | INCONCLUSIVE (no counter bump)

``down`` is debounced through ``failure_threshold`` consecutive reads so a
single blip while an app cycles through stopped→starting→running during a
redeploy doesn't page anyone.

Apps without a resolved CML binding (``cml_project_id`` / ``cml_application_id``
empty) are silently skipped — the service layer is responsible for resolving
names to ids; an unresolved row should not block the rest of the cycle.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from sqlalchemy import and_

from core.checker.base import AnomalyEvent, AnomalyType, CheckResult, CheckStatus
from core.integrations.app_interface import AppTarget
from core.logging import get_logger
from core.models.app_entities import ApplicationHealthCheck
from core.models.entities import Application, Product, ProductStatus

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from core.integrations.app_interface import AppInterface
    from core.integrations.normalization import CanonicalAppHealthEvent

logger = get_logger(__name__)


class AppChecker:
    """APP branch — runs one health probe per app target each cycle and
    decides counter / anomaly purely from the AppInterface event."""

    def __init__(self, app_interface: "AppInterface", failure_threshold: int = 3) -> None:
        self._app_interface = app_interface
        self._failure_threshold = failure_threshold
        self._counters: dict[int, int] = {}

    def check_app(
        self, target: AppTarget, *, session: "Optional[Session]" = None
    ) -> CheckResult:
        """Probe one app and decide. Mutates the internal counter.

        When ``session`` is provided, the latest CML status / relayops_health /
        timestamp / error is persisted onto the Application row so the UI
        can surface it without waiting for an Issue. The caller owns the
        transaction (this method only ``flush()``-es).
        """
        try:
            event = self._app_interface.check_health(target)
        except Exception as exc:
            logger.warning(
                "AppChecker: unexpected error checking app {}: {}",
                target.app_id, exc,
            )
            if session is not None:
                _persist_app_health(
                    session, target.app_id,
                    cml_status=None, relayops_health="unknown",
                    error=f"checker exception: {exc}",
                )
            return CheckResult(
                status=CheckStatus.INCONCLUSIVE,
                reason=f"Exception during health check for app {target.app_id}: {exc}",
            )

        if session is not None:
            _persist_app_health(
                session, target.app_id,
                cml_status=event.cml_status,
                relayops_health=(event.relayops_health or "unknown").lower(),
                error=(event.normalized_metadata or {}).get("error")
                      or (event.normalized_metadata or {}).get("transport_error"),
            )

        relayops_health = (event.relayops_health or "unknown").lower()

        # Up: reset counter, return HEALTHY.
        if relayops_health == "up":
            self._counters.pop(target.app_id, None)
            return CheckResult(
                status=CheckStatus.HEALTHY,
                reason=f"App {target.app_id} up (CML={event.cml_status})",
            )

        # Unknown (CML unreadable / unmapped): a monitoring gap, not an app
        # outage — leave the counter alone and stay inconclusive.
        if relayops_health != "down":
            return CheckResult(
                status=CheckStatus.INCONCLUSIVE,
                reason=(
                    f"App {target.app_id} relayops_health={relayops_health} "
                    f"(CML={event.cml_status}); inconclusive"
                ),
            )

        # Down (CML terminal failed/stopped): debounce through the threshold so
        # a one-tick blip during a redeploy doesn't page.
        self._counters[target.app_id] = self._counters.get(target.app_id, 0) + 1
        current = self._counters[target.app_id]

        if current >= self._failure_threshold:
            return CheckResult(
                status=CheckStatus.ANOMALY,
                anomaly_event=AnomalyEvent(
                    anomaly_type=AnomalyType.APP_OFFLINE,
                    title=f"Application down: CML status {event.cml_status}",
                    description=(
                        f"CML reports the application lifecycle is "
                        f"{event.cml_status} for {current} consecutive checks.\n"
                        f"Ops Application ID: {target.app_id}\n"
                        f"Product ID: {target.product_id}\n"
                        f"CML Project: {target.cml_project_id}\n"
                        f"CML Application: {target.cml_application_id}\n"
                        f"App Type: {target.app_type}\n"
                        f"Serving URL: {target.serving_url}\n"
                        f"Failure Threshold: {self._failure_threshold}"
                    ),
                    product_id=target.product_id,
                    app_id=target.app_id,
                    metadata={
                        "cml_status": event.cml_status,
                        "relayops_health": relayops_health,
                        "consecutive_failures": current,
                        "threshold": self._failure_threshold,
                        "cml_project_id": target.cml_project_id,
                        "cml_application_id": target.cml_application_id,
                        "app_type": target.app_type,
                    },
                ),
                reason=(
                    f"App {target.app_id} down {current} consecutive checks "
                    f"(CML={event.cml_status})"
                ),
            )

        return CheckResult(
            status=CheckStatus.HEALTHY,
            reason=(
                f"App {target.app_id} down "
                f"({current}/{self._failure_threshold} before alerting)"
            ),
        )

    def run_cycle(self, session: "Session", on_anomaly: Callable[[AnomalyEvent], None]) -> None:
        # Monitor pre-approval products too (DRAFT / PENDING_REVIEW) so owners
        # see asset health before submitting for review. Only ARCHIVED is
        # excluded — those are decommissioned, anomalies on them are noise.
        apps = (
            session.query(Application)
            .join(Product, Application.product_id == Product.id)
            .filter(
                and_(
                    Product.status.in_(
                        [
                            ProductStatus.DRAFT,
                            ProductStatus.PENDING_REVIEW,
                            ProductStatus.ACTIVE,
                        ]
                    ),
                    Application.cml_project_id.isnot(None),
                    Application.cml_project_id != "",
                    Application.cml_application_id.isnot(None),
                    Application.cml_application_id != "",
                )
            )
            .all()
        )
        if not apps:
            logger.debug("AppChecker: no active apps with resolved CML binding")
            return

        for app in apps:
            target = AppTarget(
                app_id=app.id,
                product_id=app.product_id,
                cml_project_id=app.cml_project_id,
                cml_application_id=app.cml_application_id,
                app_type=(app.cml_app_type or "generic").lower(),
                serving_url=app.cml_serving_url or "",
            )
            result = self.check_app(target, session=session)
            if result.status == CheckStatus.ANOMALY and result.anomaly_event:
                on_anomaly(result.anomaly_event)


def _persist_app_health(
    session: "Session",
    app_id: int,
    *,
    cml_status: Optional[str],
    relayops_health: Optional[str],
    error: Optional[str],
) -> None:
    """Upsert the latest health snapshot onto the Application row.

    Caller owns the transaction — we ``flush()`` so other queries in the
    same session see the update, but never ``commit()``.
    """
    app = session.query(Application).filter(Application.id == app_id).first()
    if app is None:
        return
    now = datetime.utcnow()
    app.last_cml_status = cml_status
    app.last_relayops_health = relayops_health
    app.last_checked_at = now
    app.last_check_error = error or None
    session.add(ApplicationHealthCheck(
        application_id=app_id,
        checked_at=now,
        relayops_health=(relayops_health or "unknown"),
        cml_status=cml_status,
        error=error or None,
    ))
    session.flush()
