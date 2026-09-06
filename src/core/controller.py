"""Monitoring Controller — single owner of the monitoring subsystem.

Owns its own daemon thread + timer. Each tick:
  1. refreshes (and logs) the current Ops on-duty member
  2. asks every sub-checker to run one cycle (each checker loads its
     own targets and dispatches anomalies via the AssignModule)
  3. fires the periodic SLA / duty-start checks

The Controller does NOT know about jobs, apps, or MMP — that knowledge
lives in the corresponding sub-checker's `run_cycle`.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from core.checker import AppChecker, CmlChecker, MmpChecker
from core.integrations import AppInterface, CmlApiError, ControlInterface, MmpInterface
from core.issue_management import AssignModule
from core.logging import get_logger
from core.models.database import get_db

if TYPE_CHECKING:
    from core.config import Config

logger = get_logger(__name__)


class Controller:
    """Single monitoring controller — composes checkers + AssignModule, runs the timer loop."""

    def __init__(self, config: "Config") -> None:
        self._config = config

        self.control_iface = ControlInterface(
            base_url=config.cml_platform_base_url,
            api_key=config.cml_platform_api_key or None,
            timeout=float(config.cml_platform_timeout_seconds),
            verify_ssl=config.cml_platform_verify_ssl,
            ca_bundle=config.cml_platform_ca_bundle_path or None,
            default_project_name=config.cml_platform_default_project_name or None,
        )
        # Resolved at startup (or first reachability), used as a fallback
        # for Job/Application rows that didn't record their own cml_project_id.
        self.default_cml_project_id: Optional[str] = None
        self._resolve_default_project()

        control_iface = self.control_iface
        app_iface = AppInterface(
            control_interface=control_iface,
            timeout=float(config.cml_platform_timeout_seconds),
            verify_ssl=config.cml_platform_verify_ssl,
            ca_bundle=config.cml_platform_ca_bundle_path or None,
        )
        mmp_iface = MmpInterface(
            base_url=config.mmp_base_url,
            bearer_token=config.mmp_bearer_token,
            refresh_token=config.mmp_refresh_token,
            verify_ssl=config.mmp_verify_ssl,
            ca_bundle=config.mmp_ca_bundle_path or None,
            timeout=float(config.mmp_timeout_seconds),
        )

        self.app_checker = AppChecker(
            app_interface=app_iface,
            failure_threshold=config.cml_platform_health_failure_threshold,
        )
        self.cml_checker = CmlChecker(
            control_interface=control_iface,
            staleness_threshold_minutes=config.cml_platform_job_staleness_threshold_minutes,
        )
        self.mmp_checker = MmpChecker(
            mmp_interface=mmp_iface,
            check_interval_seconds=config.mmp_check_interval_seconds,
        )

        self.assign_module = AssignModule()
        self.current_on_duty_user_id: Optional[int] = None

        self._interval = int(config.cml_platform_check_interval_seconds)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_tick_at: Optional[datetime] = None
        # Gate on-duty logging so the line is emitted on the first refresh and
        # then only when the on-duty member actually changes (not every tick).
        self._on_duty_announced: bool = False

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            logger.warning("Controller already running")
            return
        self._refresh_on_duty()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="monitoring-controller")
        self._thread.start()
        logger.info("Controller started (interval={}s)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        logger.info("Controller stopping...")

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            result = "success"
            try:
                self._refresh_on_duty()
                self.tick()
                self.last_tick_at = datetime.utcnow()
            except Exception:
                result = "error"
                logger.opt(exception=True).error("Controller: tick raised unexpectedly")
            finally:
                from core.metrics.metrics import record_controller_tick
                record_controller_tick(time.monotonic() - started, result)
            self._stop.wait(self._interval)
        logger.info("Controller stopped")

    def tick(self) -> None:
        """One full check cycle — every sub-check, isolated failures.

        Checkers may write rows on the shared session (e.g. CmlChecker
        persists JobExecution snapshots) AND dispatch anomalies via
        `assign_module.handle_anomaly`. Both paths reuse this session so
        that checker writes + Issue creation land in one atomic commit.
        """
        tick_started = time.monotonic()
        session = get_db().get_session()

        created_issues: list = []

        def on_anomaly(event):
            issue = self.assign_module.handle_anomaly(event, session=session)
            if issue is not None:
                created_issues.append(issue)

        def on_recovery(event):
            # A monitored signal read healthy again — auto-close its open Issue
            # on the shared tick session so the close commits atomically with
            # the rest of this tick.
            from core.issue_management.issue_engine import auto_close_open_issues
            try:
                auto_close_open_issues(
                    session,
                    issue_type=event.issue_type,
                    job_id=event.job_id,
                    product_id=event.product_id,
                    resolution_description=event.reason,
                    created_before=event.close_created_before,
                )
            except Exception:
                logger.opt(exception=True).error(
                    "Controller: auto-close failed for issue_type={} job_id={}",
                    event.issue_type, event.job_id,
                )

        try:
            for checker in (self.cml_checker, self.app_checker, self.mmp_checker):
                try:
                    if checker is self.mmp_checker:
                        checker.run_cycle(session, on_anomaly, on_recovery)
                    else:
                        checker.run_cycle(session, on_anomaly)
                except Exception:
                    logger.opt(exception=True).error(
                        "Controller: {} run_cycle failed", type(checker).__name__
                    )
            try:
                session.commit()
                # Reload so the detached instances keep their column values
                # after the session closes (email dispatch reads them).
                for issue in created_issues:
                    try:
                        session.refresh(issue)
                    except Exception:
                        pass
            except Exception:
                session.rollback()
                created_issues = []  # don't email about rolled-back issues
                logger.opt(exception=True).error("Controller: tick commit failed")
        finally:
            session.close()

        # Email the on-duty Ops AFTER the tick transaction committed and the
        # connection was released — the bounded send never holds the tick's DB
        # connection (which previously could stall the whole monitoring loop).
        if created_issues:
            from core.issue_management.email_dispatch import dispatch_issue_email
            for issue in created_issues:
                try:
                    dispatch_issue_email(issue)
                except Exception:
                    logger.opt(exception=True).error(
                        "Controller: on-duty email dispatch failed for an issue"
                    )
        try:
            self.assign_module.check_sla_overdue()
            self.assign_module.check_duty_start()
        except Exception:
            logger.opt(exception=True).error("Controller: SLA/duty checks failed")
        try:
            from core.report.duty_report import maybe_generate_duty_report
            maybe_generate_duty_report()
        except Exception:
            logger.opt(exception=True).error("Controller: duty report check failed")

        # One concise summary per cycle instead of a line per HTTP probe. The
        # individual probes are silent on success (httpx is pinned to WARNING)
        # and only log when a call fails / returns non-2xx, so the per-tick
        # log is now: this summary + whatever genuinely went wrong.
        logger.info(
            "Monitoring tick complete in {:.1f}s (issues_created={})",
            time.monotonic() - tick_started,
            len(created_issues),
        )
        from core.metrics.metrics import record_issues_created
        record_issues_created(len(created_issues))

    def _resolve_default_project(self) -> None:
        """Resolve the configured ``cml_platform.default_project_name`` to a
        project_id and cache it on the Controller.

        Used as a fallback when a Job / Application row doesn't have its own
        ``cml_project_id`` populated (e.g. legacy rows or rows whose binding
        couldn't be resolved at create-time). Failures here are logged but
        non-fatal — checkers will simply skip rows that don't have a usable
        binding, so the monitoring loop still starts.
        """
        name = (self._config.cml_platform_default_project_name or "").strip()
        if not name:
            logger.info(
                "Controller: cml_platform.default_project_name not configured; "
                "Job/Application rows must each carry their own cml_project_id."
            )
            return
        try:
            self.default_cml_project_id = self.control_iface.resolve_project_id(name)
            logger.info(
                "Controller: default CML project resolved {} -> {}",
                name, self.default_cml_project_id,
            )
        except CmlApiError as exc:
            self.default_cml_project_id = None
            logger.warning(
                "Controller: failed to resolve default CML project '{}' at startup "
                "(status={}); rows without their own cml_project_id will be skipped: {}",
                name, exc.status_code, exc.message,
            )
        except Exception:
            self.default_cml_project_id = None
            logger.opt(exception=True).warning(
                "Controller: unexpected error resolving default CML project '{}'", name
            )

    def _refresh_on_duty(self) -> None:
        """Refresh the current Ops on-duty member, logging only on change.

        Runs every tick, but the on-duty member rarely changes, so logging it
        each time is pure noise. We log on the first refresh and thereafter
        only when the resolved member actually changes.

        Anomaly assignment in `AssignModule` still queries the schedule
        table directly; this cache is surfaced in logs / debug API only.
        """
        from core.issue_management.issue_engine import (
            get_current_on_duty_members,
            get_fallback_admin,
        )

        db = get_db()
        try:
            members = get_current_on_duty_members(db)
            if members:
                new_id, message, is_warning = members[0].id, "Ops on-duty: user_id={}", False
            else:
                admin = get_fallback_admin(db)
                if admin:
                    new_id, message, is_warning = (
                        admin.id, "Ops on-duty: admin fallback user_id={}", False
                    )
                else:
                    new_id, message, is_warning = (
                        None, "Ops on-duty: no on-duty member and no admin configured", True
                    )
        except Exception:
            logger.opt(exception=True).error("Ops on-duty refresh failed")
            return

        changed = new_id != self.current_on_duty_user_id or not self._on_duty_announced
        self.current_on_duty_user_id = new_id
        self._on_duty_announced = True
        if not changed:
            return
        if is_warning:
            logger.warning(message)
        else:
            logger.info(message, new_id)


# ── module-level singleton wiring (used by lifespan + manual check-now) ──


_instance: Optional[Controller] = None


def start() -> Controller:
    """Build + start the Controller (idempotent)."""
    global _instance
    if _instance is not None and _instance.is_running:
        return _instance
    from core.config import get_config

    cfg = get_config()
    _instance = Controller(cfg)
    _instance.start()
    return _instance


def stop() -> None:
    if _instance is not None:
        _instance.stop()


def get_instance() -> Optional[Controller]:
    return _instance


def get_default_cml_project_id() -> Optional[str]:
    """Convenience helper for the service layer.

    Returns the Controller-resolved default CML project_id, or None if the
    Controller has not been started or default resolution failed.
    """
    return _instance.default_cml_project_id if _instance is not None else None
