"""App creation and router inclusion only."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api.middleware.access_log import AccessLogMiddleware
from api.middleware.cors import add_cors
from api.middleware.metrics import PrometheusMiddleware
from api.middleware.safety import SafetyMiddleware
from api.routers import (
    agent,
    agent_actions,
    analytics,
    api_keys,
    apps,
    audit_logs,
    auth,
    chat,
    diagnose,
    duty_report,
    issue_preferences,
    issues,
    jobs,
    members,
    metrics,
    notifications,
    products,
    projects,
    scenario_templates,
    schedules,
    support_groups,
    users,
    verification,
)
from api.static.spa import mount_spa
from core.config import get_config
from core.exceptions import DomainError
from core.logging import get_logger, setup_logging
from core.models.database import get_db
from core.services.user_service import bootstrap_local_accounts

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialize resources on startup, cleanup on shutdown."""
    setup_logging()
    get_db()
    logger.info("Database initialized")

    bootstrap_local_accounts()

    # Seed shared support-group data for local/demo environments.
    try:
        from core.infrastructure.system_seed import (
            ensure_project_owner_members,
            seed_demo_support_groups,
            seed_demo_walkthrough_resources,
            seed_system_mock_monitoring,
        )
        seed_demo_support_groups()
        seed_demo_walkthrough_resources()
        seed_system_mock_monitoring()
        # Backfill ProjectMember owner rows so every project's creator
        # shows up in the Members list (post role-decoupling refactor).
        ensure_project_owner_members()
    except Exception:
        logger.opt(exception=True).warning("System seed failed; continuing startup")

    # Backfill legacy products/issues into ProductVersion semantics before
    # the monitoring Controller and runtime workflows start using version-aware logic.
    try:
        from core.infrastructure.versioning_backfill import backfill_legacy_versioning_state
        backfill_legacy_versioning_state()
    except Exception:
        logger.opt(exception=True).warning("Legacy versioning backfill failed; continuing startup")

    # Backfill CML project name from per-Job rows up to Ops Project (Phase 2
    # B refactor: the binding moved from per-Job to per-Project, so old rows
    # need their owning Project's cml_project_name populated to keep monitoring).
    try:
        from core.infrastructure.cml_project_binding_backfill import (
            backfill_project_cml_binding,
        )
        backfill_project_cml_binding()
    except Exception:
        logger.opt(exception=True).warning(
            "CML project binding backfill failed; continuing startup"
        )

    # Drift-only MMP policy: remove Issues / Notifications / Job scenarios left
    # behind by the three retired non-drift MMP concern signals (fairness risk,
    # run-pending-approval, unapproved-exp-run). Idempotent.
    try:
        from core.infrastructure.mmp_concern_cleanup_backfill import (
            cleanup_disabled_mmp_concerns,
        )
        cleanup_disabled_mmp_concerns()
    except Exception:
        logger.opt(exception=True).warning(
            "MMP concern cleanup failed; continuing startup"
        )

    # Backfill the MMP web deep link onto pre-existing open MMP issues so the
    # "Open in MMP" button can render for issues created before that feature.
    try:
        from core.infrastructure.mmp_external_url_backfill import (
            backfill_mmp_issue_external_urls,
        )
        backfill_mmp_issue_external_urls()
    except Exception:
        logger.opt(exception=True).warning(
            "MMP external_url backfill failed; continuing startup"
        )

    # Rebuild the agent's FTS retrieval sidecar (resolved-issue resolutions +
    # runbook scenarios). Idempotent; incremental upkeep happens on resolve.
    try:
        from core.infrastructure.resolution_fts_backfill import run_backfill

        run_backfill()
    except Exception:
        logger.opt(exception=True).warning(
            "resolution FTS backfill failed; continuing startup"
        )

    # Rebuild the guide KB's FTS index from docs/kb/*.md. Idempotent.
    try:
        from core.infrastructure.kb_fts_backfill import run_backfill as run_kb_backfill

        run_kb_backfill()
    except Exception:
        logger.opt(exception=True).warning(
            "KB FTS backfill failed; continuing startup"
        )

    # Start the monitoring Controller (owns its own timer/thread)
    from core import controller as monitoring_controller
    monitoring_controller.start()
    logger.info("Monitoring Controller started")

    yield

    monitoring_controller.stop()
    logger.info("Monitoring Controller stopped")

    try:
        db = get_db()
        db.engine.dispose()
        logger.info("Database connections disposed")
    except Exception:
        logger.opt(exception=True).warning("Error disposing database engine")
    logger.info("Shutting down")


app = FastAPI(lifespan=lifespan)
# Added last -> outermost, so it observes the final response status. Replaces
# the framework access log (off on the CDSW path) with state-change logging.
app.add_middleware(AccessLogMiddleware)
app.add_middleware(SafetyMiddleware)
# Added last -> outermost, so it times the whole request and sees the final
# status (incl. responses SafetyMiddleware synthesises on unhandled errors).
app.add_middleware(PrometheusMiddleware)


@app.get("/healthz")
def healthz():
    """Liveness probe used by cml-status.sh and external uptime checks.

    Must be defined BEFORE mount_spa(app); otherwise the SPA catch-all
    serves index.html and the check silently 'passes' with HTML 200.
    """
    return {"status": "ok"}


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    """Translate service-layer domain exceptions to HTTP responses."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all handler: log and return 500 so the app never hangs on unhandled errors."""
    logger.opt(exception=True).error("Unhandled exception on {} {}", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


add_cors(app, allow_origins=get_config().cors_allow_origins)

app.include_router(auth.router)
app.include_router(agent.router)
app.include_router(agent_actions.router)
app.include_router(chat.router)
app.include_router(diagnose.router)
app.include_router(projects.router)
app.include_router(support_groups.router)
app.include_router(products.router)
app.include_router(jobs.router)
app.include_router(apps.router)
app.include_router(issues.router)
app.include_router(members.router)
app.include_router(schedules.router)
app.include_router(audit_logs.router)
app.include_router(analytics.router)
app.include_router(api_keys.router)
app.include_router(notifications.router)
app.include_router(duty_report.router)
app.include_router(issue_preferences.router)
app.include_router(verification.router)
app.include_router(scenario_templates.router)
app.include_router(users.router)

# Public Prometheus scrape endpoint. Registered before mount_spa() for the same
# reason as /healthz — otherwise the SPA catch-all would serve index.html.
app.include_router(metrics.router)

# Mount SPA last — catch-all for all non-API paths
mount_spa(app)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
