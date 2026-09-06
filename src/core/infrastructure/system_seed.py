"""
Seed system-managed mock monitoring resources, demo support groups, and a
walkthrough-ready demo product.

Creates:
- a read-only system project/product/jobs used for Mock Control-M checks
- demo support groups for Demo group-routing walkthroughs
- a non-system demo project/product with complete Job/App runbooks and scenarios
  so issue handling can be demonstrated immediately after startup
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from core.config import get_config
from core.models.database import get_db
from core.models.user import User, UserRole
from core.models.entities import (
    Application,
    ApplicationRecoveryScenario,
    ApplicationRecoveryScenarioType,
    FallbackOwnerType,
    IssueType,
    Job,
    JobFailureScenario,
    JobFailureScenarioType,
    Product,
    ProductVersion,
    ProductStatus,
    Project,
    ProjectMember,
    ProjectSupportGroup,
    Schedule,
    SupportGroup,
    SupportGroupSourceType,
    UserIssuePreference,
)
from core.logging import get_logger
from core.services.product_version_service import ensure_product_baseline_version, refresh_product_version_artifacts
from core.services.support_group_service import SEEDED_SUPPORT_GROUPS, normalize_group_key

logger = get_logger(__name__)

SYSTEM_PROJECT_NAME = "__system_mock_monitoring__"
SYSTEM_PRODUCT_NAME = "mock-cml-observer"
SYSTEM_JOB_NAMES = ("daily-model-training", "hourly-data-sync", "daily-etl-pipeline", "weekly-model-retrain")
# CML v2 project name used by every system/demo Job and Application; matches
# the seed in mock_services/cml_platform.py so the monitoring loop has a
# resolvable binding out of the box.
SYSTEM_CML_PROJECT_NAME = "relayops-demo"
DEMO_PROJECT_NAME = "Demo Ops Walkthrough Demo"
DEMO_PRODUCT_NAME = "Demo Inventory Health Demo"
DEMO_JOB_NAME = "relayops-inventory-risk-aggregation"
# The demo job rides on top of one of the seeded CML jobs so it is monitorable
# end-to-end with no extra setup. The user can rebind to their own job after.
DEMO_CML_JOB_NAME = "daily-model-training"
DEMO_APP_URL = "http://localhost:9001"
DEMO_APP_HEALTH_URL = "http://mock-app-fastapi:9001/health"
# CML v2 binding for the demo FastAPI app — matches a seeded Application in
# mock_services/cml_platform.py.
DEMO_CML_APP_NAME = "inventory-health-api"
DEMO_CML_APP_SUBDOMAIN = "inventorydemo"
DEMO_CML_APP_TYPE = "fastapi"
DEMO_CML_APP_SERVING_URL = "http://mock-app-fastapi:9001"
DEMO_OWNER_GROUP_KEY = "relayops-demo-shared-ops"
DEMO_JOB_SUPPORT_GROUP_KEY = "relayops-data-platform"
DEMO_APP_SUPPORT_GROUP_KEY = "relayops-app-ops"


def _running_on_cml() -> bool:
    """RelayOps  running on cml."""
    return bool(os.environ.get("CDSW_APP_PORT"))


def _purge_system_mock_monitoring(session) -> int:
    """Remove the seeded system project/product/jobs if present.

    Called on CML to clean up rows that earlier builds (with the old
    ``is_production``-only gate) left behind. Idempotent: returns the
    number of system Projects removed.
    """
    seeded_projects = (
        session.query(Project)
        .filter(Project.name == SYSTEM_PROJECT_NAME, Project.is_system == 1)
        .all()
    )
    if not seeded_projects:
        return 0
    # CASCADE on Project → Product → Job/Application takes care of the rest.
    for project in seeded_projects:
        session.delete(project)
    return len(seeded_projects)


def seed_system_mock_monitoring() -> None:
    """Idempotently create system-owned mock monitoring resources.

    On CML, the seed is skipped AND any previously-seeded rows are
    purged so prod environments don't carry a stale demo project.
    """
    if _running_on_cml():
        db = get_db()
        session = db.get_session()
        try:
            removed = _purge_system_mock_monitoring(session)
            if removed:
                session.commit()
                logger.info(
                    "Purged {} pre-existing system mock monitoring project(s) "
                    "(CML environment — demo seed is not applicable here)",
                    removed,
                )
            else:
                logger.info(
                    "Skipping system mock monitoring seed on CML environment"
                )
        except Exception:
            session.rollback()
            logger.opt(exception=True).warning(
                "Failed to purge pre-existing system mock monitoring resources"
            )
        finally:
            session.close()
        return

    db = get_db()
    session = db.get_session()
    try:
        # Pick admin owner if available, otherwise any user.
        admin = session.query(User).filter(User.role == UserRole.ADMIN).order_by(User.id.asc()).first()
        owner = admin or session.query(User).order_by(User.id.asc()).first()
        if owner is None:
            logger.warning("Skipping system seed: no users exist yet")
            return

        project = (
            session.query(Project)
            .filter(Project.name == SYSTEM_PROJECT_NAME, Project.is_system == 1)
            .first()
        )
        if project is None:
            project = Project(
                name=SYSTEM_PROJECT_NAME,
                description="System-managed read-only project for mock monitoring",
                owner_id=owner.id,
                is_system=1,
            )
            session.add(project)
            session.flush()
            logger.info("Created system project: {} (id={})", SYSTEM_PROJECT_NAME, project.id)

        product = (
            session.query(Product)
            .filter(
                Product.project_id == project.id,
                Product.name == SYSTEM_PRODUCT_NAME,
                Product.is_system == 1,
            )
            .first()
        )
        if product is None:
            product = Product(
                project_id=project.id,
                name=SYSTEM_PRODUCT_NAME,
                status=ProductStatus.ACTIVE,
                latest_version_number="1",
                is_system=1,
            )
            session.add(product)
            session.flush()
            logger.info("Created system product: {} (id={})", SYSTEM_PRODUCT_NAME, product.id)
        elif product.status != ProductStatus.ACTIVE:
            product.status = ProductStatus.ACTIVE
            product.updated_at = datetime.utcnow()

        # Resolve CML project once for the whole system seed batch (best-effort:
        # if CML is unreachable the system jobs persist with NULL ids and the
        # monitoring loop just skips them until ids are filled in later).
        from core.services.cml_binding_resolver import (
            build_control_interface,
            resolve_job_id as _resolve_cml_job_id,
            resolve_project_id as _resolve_cml_project_id,
        )

        cml_iface = build_control_interface()
        cml_project_id, cml_project_err = _resolve_cml_project_id(cml_iface, SYSTEM_CML_PROJECT_NAME)

        for job_name in SYSTEM_JOB_NAMES:
            existing = (
                session.query(Job)
                .filter(
                    Job.product_id == product.id,
                    Job.control_m_job_name == job_name,
                    Job.is_system == 1,
                )
                .first()
            )
            if cml_project_id:
                cml_job_id, cml_job_err = _resolve_cml_job_id(
                    cml_iface, cml_project_id, job_name
                )
            else:
                cml_job_id, cml_job_err = None, cml_project_err
            cml_binding_error = cml_job_err
            if existing is None:
                job = Job(
                    product_id=product.id,
                    mmp_project_id="",
                    mmp_model_id="",
                    control_m_job_name=job_name,
                    control_m_cron="",
                    cml_project_name=SYSTEM_CML_PROJECT_NAME,
                    cml_job_name=job_name,
                    cml_project_id=cml_project_id,
                    cml_job_id=cml_job_id,
                    cml_binding_error=cml_binding_error,
                    schedule_cron="",
                    description=f"System-managed CML v2 watcher for '{job_name}'",
                    dependencies=None,
                    is_system=1,
                )
                session.add(job)
                logger.info(
                    "Created system job watcher: {} (cml_project_id={}, cml_job_id={})",
                    job_name, cml_project_id, cml_job_id,
                )
            else:
                # Refresh cached binding on every startup so an offline-then-online
                # CML doesn't leave system jobs permanently unresolved.
                changed = False
                if existing.cml_project_name != SYSTEM_CML_PROJECT_NAME:
                    existing.cml_project_name = SYSTEM_CML_PROJECT_NAME
                    changed = True
                if existing.cml_job_name != job_name:
                    existing.cml_job_name = job_name
                    changed = True
                if cml_project_id and existing.cml_project_id != cml_project_id:
                    existing.cml_project_id = cml_project_id
                    changed = True
                if cml_job_id and existing.cml_job_id != cml_job_id:
                    existing.cml_job_id = cml_job_id
                    changed = True
                if existing.cml_binding_error != cml_binding_error:
                    existing.cml_binding_error = cml_binding_error
                    changed = True
                if changed:
                    existing.updated_at = datetime.utcnow()

        session.commit()
    except Exception:
        session.rollback()
        logger.opt(exception=True).warning("Failed to seed system mock monitoring resources")
    finally:
        session.close()


def seed_demo_support_groups() -> None:
    """Ensure seeded/demo support groups exist locally for selector-based flows."""
    db = get_db()
    session = db.get_session()
    try:
        admin = session.query(User).filter(User.role == UserRole.ADMIN).order_by(User.id.asc()).first()
        created_by = admin.id if admin else None

        for item in SEEDED_SUPPORT_GROUPS:
            group_key = normalize_group_key(item.get("group_key") or item.get("group_name"))
            if not group_key:
                continue
            existing = session.query(SupportGroup).filter(SupportGroup.group_key == group_key).first()
            if existing is None:
                session.add(
                    SupportGroup(
                        group_key=group_key,
                        group_name=item.get("group_name") or group_key,
                        description=item.get("description") or "",
                        source_type=item.get("source_type") or SupportGroupSourceType.SEEDED,
                        external_ref=item.get("external_ref") or "",
                        sync_status="seeded",
                        is_active=True,
                        created_by=created_by,
                    )
                )
                logger.info("Seeded support group: {}", group_key)
                continue

            changed = False
            next_name = item.get("group_name") or existing.group_name
            next_desc = item.get("description") or existing.description
            next_external_ref = item.get("external_ref") or existing.external_ref
            next_source_type = item.get("source_type") or existing.source_type
            if existing.group_name != next_name:
                existing.group_name = next_name
                changed = True
            if existing.description != next_desc:
                existing.description = next_desc
                changed = True
            if existing.external_ref != next_external_ref:
                existing.external_ref = next_external_ref
                changed = True
            if existing.source_type != next_source_type:
                existing.source_type = next_source_type
                changed = True
            if existing.sync_status != "seeded":
                existing.sync_status = "seeded"
                changed = True
            if not existing.is_active:
                existing.is_active = True
                changed = True
            if changed:
                existing.updated_at = datetime.utcnow()

        session.commit()
    except Exception:
        session.rollback()
        logger.opt(exception=True).warning("Failed to seed demo support groups")
    finally:
        session.close()


def _find_support_group(session, group_key: str) -> SupportGroup | None:
    normalized = normalize_group_key(group_key)
    if not normalized:
        return None
    return session.query(SupportGroup).filter(SupportGroup.group_key == normalized).first()


def _upsert_job_scenario(session, job: Job, scenario_type: str, **payload) -> None:
    scenario = (
        session.query(JobFailureScenario)
        .filter(JobFailureScenario.job_id == job.id, JobFailureScenario.scenario_type == scenario_type)
        .first()
    )
    if scenario is None:
        scenario = JobFailureScenario(job_id=job.id, scenario_type=scenario_type)
        session.add(scenario)

    scenario.scenario_name = payload.get("scenario_name") or scenario.scenario_name or "Scenario"
    scenario.condition_description = payload.get("condition_description") or ""
    scenario.detection_source = payload.get("detection_source") or ""
    scenario.diagnostic_steps = payload.get("diagnostic_steps") or []
    scenario.action_steps = payload.get("action_steps") or []
    scenario.verification_steps = payload.get("verification_steps") or []
    scenario.escalation_target = payload.get("escalation_target") or ""
    scenario.fallback_owner_type = payload.get("fallback_owner_type") or FallbackOwnerType.CASE_BY_CASE
    scenario.is_active = bool(payload.get("is_active", True))
    scenario.updated_at = datetime.utcnow()


def _upsert_app_scenario(session, app: Application, scenario_type: str, **payload) -> None:
    scenario = (
        session.query(ApplicationRecoveryScenario)
        .filter(ApplicationRecoveryScenario.application_id == app.id, ApplicationRecoveryScenario.scenario_type == scenario_type)
        .first()
    )
    if scenario is None:
        scenario = ApplicationRecoveryScenario(application_id=app.id, scenario_type=scenario_type)
        session.add(scenario)

    scenario.scenario_name = payload.get("scenario_name") or scenario.scenario_name or "Scenario"
    scenario.condition_description = payload.get("condition_description") or ""
    scenario.action_steps = payload.get("action_steps") or []
    scenario.verification_steps = payload.get("verification_steps") or []
    scenario.escalation_target = payload.get("escalation_target") or ""
    scenario.fallback_owner_type = payload.get("fallback_owner_type") or FallbackOwnerType.CASE_BY_CASE
    scenario.is_active = bool(payload.get("is_active", True))
    scenario.updated_at = datetime.utcnow()


def seed_demo_walkthrough_resources() -> None:
    """Create a walkthrough-ready demo product with complete runbooks and scenarios."""
    cfg = get_config()
    if cfg.is_production:
        logger.info("Skipping walkthrough demo seed in production environment")
        return

    db = get_db()
    session = db.get_session()
    try:
        owner = session.query(User).filter(User.username == "testuser").first()
        admin = session.query(User).filter(User.role == UserRole.ADMIN).order_by(User.id.asc()).first()
        relayops_member = session.query(User).filter(User.username == "relayopsmember1").first()
        if owner is None:
            logger.warning("Skipping walkthrough demo seed: testuser not found")
            return

        owner_group = _find_support_group(session, DEMO_OWNER_GROUP_KEY)
        job_support_group = _find_support_group(session, DEMO_JOB_SUPPORT_GROUP_KEY)
        app_support_group = _find_support_group(session, DEMO_APP_SUPPORT_GROUP_KEY)

        project = session.query(Project).filter(Project.name == DEMO_PROJECT_NAME, Project.is_system == 0).first()
        if project is None:
            project = Project(
                name=DEMO_PROJECT_NAME,
                description="Walkthrough-ready Ops demo covering group sharing, runbook completeness, and issue handling.",
                owner_id=owner.id,
                owner_group_id=owner_group.id if owner_group else None,
                owner_group_name_snapshot=owner_group.group_name if owner_group else "",
                is_system=0,
            )
            session.add(project)
            session.flush()
            logger.info("Created walkthrough demo project: {} (id={})", DEMO_PROJECT_NAME, project.id)
        else:
            project.owner_id = owner.id
            project.description = "Walkthrough-ready Ops demo covering group sharing, runbook completeness, and issue handling."
            project.owner_group_id = owner_group.id if owner_group else None
            project.owner_group_name_snapshot = owner_group.group_name if owner_group else ""
            project.updated_at = datetime.utcnow()

        if owner_group is not None:
            binding = (
                session.query(ProjectSupportGroup)
                .filter(
                    ProjectSupportGroup.project_id == project.id,
                    ProjectSupportGroup.support_group_id == owner_group.id,
                )
                .first()
            )
            if binding is None:
                session.add(
                    ProjectSupportGroup(
                        project_id=project.id,
                        support_group_id=owner_group.id,
                        support_group_name_snapshot=owner_group.group_name,
                        created_by=admin.id if admin else owner.id,
                    )
                )

        product = (
            session.query(Product)
            .filter(Product.project_id == project.id, Product.name == DEMO_PRODUCT_NAME, Product.is_system == 0)
            .first()
        )
        if product is None:
            product = Product(
                project_id=project.id,
                name=DEMO_PRODUCT_NAME,
                status=ProductStatus.ACTIVE,
                latest_version_number="1",
                is_system=0,
            )
            session.add(product)
            session.flush()
            logger.info("Created walkthrough demo product: {} (id={})", DEMO_PRODUCT_NAME, product.id)
        else:
            product.name = DEMO_PRODUCT_NAME
            product.status = ProductStatus.ACTIVE
            product.latest_version_number = product.latest_version_number or "1"
            product.updated_at = datetime.utcnow()

        # Resolve CML binding for the demo job (best-effort).
        from core.services.cml_binding_resolver import (
            build_control_interface,
            resolve_application_id as _resolve_cml_app_id,
            resolve_job_id as _resolve_cml_job_id,
            resolve_project_id as _resolve_cml_project_id,
        )

        cml_iface = build_control_interface()
        demo_cml_project_id, demo_cml_project_err = _resolve_cml_project_id(
            cml_iface, SYSTEM_CML_PROJECT_NAME
        )
        if demo_cml_project_id:
            demo_cml_job_id, demo_cml_job_err = _resolve_cml_job_id(
                cml_iface, demo_cml_project_id, DEMO_CML_JOB_NAME
            )
        else:
            demo_cml_job_id, demo_cml_job_err = None, demo_cml_project_err

        job = (
            session.query(Job)
            .filter(Job.product_id == product.id, Job.control_m_job_name == DEMO_JOB_NAME, Job.is_system == 0)
            .first()
        )
        if job is None:
            job = Job(product_id=product.id, control_m_job_name=DEMO_JOB_NAME, is_system=0)
            session.add(job)
            session.flush()

        job.has_mmp_dependency = True
        job.mmp_project_id = "inventory-risk-demo"
        job.mmp_model_id = "agg-v2"
        job.control_m_cron = "0 */2 * * *"
        job.cml_project_name = SYSTEM_CML_PROJECT_NAME
        job.cml_job_name = DEMO_CML_JOB_NAME
        job.cml_project_id = demo_cml_project_id
        job.cml_job_id = demo_cml_job_id
        job.cml_binding_error = demo_cml_job_err
        job.schedule_cron = "0 */2 * * *"
        job.description = "Aggregates payment risk signals for the Demo walkthrough demo."
        job.dependencies = {
            "upstream_jobs": ["raw-payment-ingest", "fraud-feature-materialization"],
            "external_systems": ["Control-M", "Starburst"],
        }
        job.failure_strategy_summary = (
            "If the job never triggers, verify Control-M scheduling and alert the platform scheduler owner. "
            "If it triggers but fails, inspect Starburst and application logs, then retry once if the failure is transient."
        )
        job.dependency_notes = (
            "This job depends on raw-payment-ingest and fraud-feature-materialization. "
            "If either upstream job failed, confirm upstream recovery before rerunning this aggregation."
        )
        job.owner_contact = "inventory-platform@example.com"
        job.support_group_id = job_support_group.id if job_support_group else None
        job.support_group_name_snapshot = job_support_group.group_name if job_support_group else ""
        job.support_group = job_support_group.group_name if job_support_group else "Ops Data Platform"
        job.runbook_required = True
        job.updated_at = datetime.utcnow()

        _upsert_job_scenario(
            session,
            job,
            JobFailureScenarioType.NOT_TRIGGERED,
            scenario_name="Control-M never triggered the expected run",
            condition_description="The expected schedule window passed and the latest CML poll did not observe a fresh run.",
            detection_source="RelayOps CML polling cycle, Mock Services dashboard, Control-M schedule view",
            diagnostic_steps=[
                "Open Control-M and confirm whether the expected run instance exists.",
                "Check the latest polled execution snapshot for the Ops Job ID.",
                "Review upstream dependency status before attempting any rerun.",
            ],
            action_steps=[
                "If Control-M never launched the run, notify the scheduling owner and request a rerun.",
                "If dependencies are incomplete, wait for upstream recovery before proceeding.",
            ],
            verification_steps=[
                "Confirm a fresh successful execution snapshot is recorded for the current schedule window.",
                "Verify the latest job execution is marked successful in RelayOps.",
            ],
            escalation_target="Control-M scheduler owner, inventory-platform@example.com",
            fallback_owner_type=FallbackOwnerType.TECH_OWNER,
        )
        _upsert_job_scenario(
            session,
            job,
            JobFailureScenarioType.TRIGGERED_BUT_FAILED,
            scenario_name="Job triggered but failed during execution",
            condition_description="Control-M shows the run started, but the polled execution status is failed.",
            detection_source="RelayOps CML polling, Control-M job status, Starburst query history",
            diagnostic_steps=[
                "Inspect the failing task step in Control-M and copy the failure details.",
                "Check Starburst / downstream warehouse connectivity.",
                "Validate whether the failure is data-related or platform-related.",
            ],
            action_steps=[
                "Retry once if the failure was caused by a transient infrastructure issue.",
                "If the failure is reproducible, escalate to the inventory platform owner with captured logs.",
            ],
            verification_steps=[
                "Confirm the rerun completes successfully.",
                "Verify downstream payment-risk aggregates have refreshed.",
            ],
            escalation_target="inventory-platform@example.com and data-warehouse-oncall@example.com",
            fallback_owner_type=FallbackOwnerType.PRODUCT_OWNER,
        )
        _upsert_job_scenario(
            session,
            job,
            JobFailureScenarioType.DEPENDENCY_FAILED,
            scenario_name="Upstream dependency failed before aggregation",
            condition_description="The aggregation job is blocked because a required upstream ingest or feature job failed.",
            detection_source="Dependency dashboard, upstream Control-M jobs, Ops issue context",
            diagnostic_steps=[
                "Identify which dependency failed and capture the upstream job ID.",
                "Check whether upstream teams already have an open issue for the same failure.",
            ],
            action_steps=[
                "Do not rerun aggregation until upstream recovery completes.",
                "Notify the upstream owner and track progress in the issue timeline.",
            ],
            verification_steps=[
                "Confirm the dependency succeeded on its rerun.",
                "Run or request the aggregation rerun and verify completion.",
            ],
            escalation_target="Upstream data ingestion owner and inventory-platform@example.com",
            fallback_owner_type=FallbackOwnerType.CASE_BY_CASE,
        )

        if demo_cml_project_id:
            demo_cml_app_id, demo_cml_app_err = _resolve_cml_app_id(
                cml_iface, demo_cml_project_id,
                name=DEMO_CML_APP_NAME, subdomain=DEMO_CML_APP_SUBDOMAIN,
            )
        else:
            demo_cml_app_id, demo_cml_app_err = None, demo_cml_project_err

        app = session.query(Application).filter(Application.product_id == product.id, Application.application_url == DEMO_APP_URL).first()
        if app is None:
            app = Application(product_id=product.id, application_url=DEMO_APP_URL, is_system=0)
            session.add(app)
            session.flush()

        app.application_url = DEMO_APP_URL
        app.health_check_url = DEMO_APP_HEALTH_URL
        app.cml_project_name = SYSTEM_CML_PROJECT_NAME
        app.cml_application_name = DEMO_CML_APP_NAME
        app.cml_subdomain = DEMO_CML_APP_SUBDOMAIN
        app.cml_app_type = DEMO_CML_APP_TYPE
        app.cml_serving_url = DEMO_CML_APP_SERVING_URL
        app.cml_project_id = demo_cml_project_id
        app.cml_application_id = demo_cml_app_id
        app.cml_binding_error = demo_cml_app_err
        app.description = "Demo FastAPI service used to simulate healthy/unhealthy transitions and Ops recovery handling."
        app.restart_supported = True
        app.restart_summary = (
            "If health checks fail repeatedly, use the Mock Services dashboard to restore the mock app to healthy mode, "
            "then re-run the app health check from RelayOps or wait for the scheduler retry."
        )
        app.owner_contact = "inventory-api@example.com"
        app.support_group_id = app_support_group.id if app_support_group else None
        app.support_group_name_snapshot = app_support_group.group_name if app_support_group else ""
        app.support_group = app_support_group.group_name if app_support_group else "Ops App Ops"
        app.updated_at = datetime.utcnow()

        _upsert_app_scenario(
            session,
            app,
            ApplicationRecoveryScenarioType.OFFLINE,
            scenario_name="Application health endpoint is offline",
            condition_description="RelayOps cannot reach the health check endpoint or receives repeated failures.",
            action_steps=[
                "Confirm whether the mock app was intentionally set to unhealthy in the Mock Services dashboard.",
                "If restart is supported, restore the service to healthy mode and retry the health check.",
            ],
            verification_steps=[
                "Verify the health endpoint returns HTTP 200 with a healthy payload.",
                "Confirm the app_offline issue auto-closes or can be resolved with evidence attached.",
            ],
            escalation_target="inventory-api@example.com",
            fallback_owner_type=FallbackOwnerType.TECH_OWNER,
        )
        _upsert_app_scenario(
            session,
            app,
            ApplicationRecoveryScenarioType.HEALTHCHECK_FAILED,
            scenario_name="Health check returns an unhealthy payload",
            condition_description="The endpoint responds, but readiness or dependency checks report unhealthy state.",
            action_steps=[
                "Open the health payload and identify the failing dependency.",
                "Coordinate with the dependency owner or restore the mock app if the issue is simulated.",
            ],
            verification_steps=[
                "Re-run the health check until it reports healthy.",
                "Confirm user-facing access to the application is restored.",
            ],
            escalation_target="inventory-api@example.com and dependency owners listed in the health payload",
            fallback_owner_type=FallbackOwnerType.CASE_BY_CASE,
        )
        _upsert_app_scenario(
            session,
            app,
            ApplicationRecoveryScenarioType.RESTART_REQUIRED,
            scenario_name="Application requires restart or rollout recovery",
            condition_description="The app remains unhealthy until a restart or redeploy action is performed.",
            action_steps=[
                "Use the documented restart path in the Mock Services dashboard or deployment script.",
                "Capture the restart time and operator in the issue timeline.",
            ],
            verification_steps=[
                "Confirm the health endpoint returns healthy after restart.",
                "Check one functional URL to confirm the app is serving requests again.",
            ],
            escalation_target="inventory-api@example.com",
            fallback_owner_type=FallbackOwnerType.TECH_OWNER,
        )

        approved_version = ensure_product_baseline_version(session, product)
        if product.current_approved_version_id:
            approved = session.query(ProductVersion).filter(ProductVersion.id == product.current_approved_version_id).first()
            if approved is not None:
                refresh_product_version_artifacts(session, approved)
        elif approved_version is not None:
            refresh_product_version_artifacts(session, approved_version)

        if relayops_member is not None:
            for issue_type in (IssueType.JOB_FAILED, IssueType.JOB_NOT_TRIGGERED, IssueType.APP_OFFLINE):
                pref = (
                    session.query(UserIssuePreference)
                    .filter(UserIssuePreference.user_id == relayops_member.id, UserIssuePreference.issue_type == issue_type)
                    .first()
                )
                if pref is None:
                    session.add(UserIssuePreference(user_id=relayops_member.id, issue_type=issue_type))

            active_schedule = (
                session.query(Schedule)
                .filter(Schedule.assignee_id == relayops_member.id, Schedule.start_time <= datetime.utcnow(), Schedule.end_time >= datetime.utcnow())
                .first()
            )
            any_current_primary = (
                session.query(Schedule)
                .filter(Schedule.duty_role == "Primary", Schedule.start_time <= datetime.utcnow(), Schedule.end_time >= datetime.utcnow())
                .first()
            )
            if active_schedule is None and any_current_primary is None:
                now = datetime.utcnow().replace(microsecond=0)
                session.add(
                    Schedule(
                        start_time=now,
                        end_time=now + timedelta(days=7),
                        assignee_id=relayops_member.id,
                        created_by=admin.id if admin else owner.id,
                        duty_role="Primary",
                        note="Seeded demo schedule so walkthrough issues route to relayopsmember1.",
                    )
                )

        session.commit()
        logger.info("Walkthrough demo resources ready: project_id={} product_id={} job_id={} app_id={}", project.id, product.id, job.id, app.id)
    except Exception:
        session.rollback()
        logger.opt(exception=True).warning("Failed to seed walkthrough demo resources")
    finally:
        session.close()


def ensure_project_owner_members() -> None:
    """Backfill: every project's owner must appear as a ProjectMember row
    with role='business_owner'.

    Pre-refactor the project owner was implicit (``Project.owner_id``) and
    deliberately excluded from the members list. Post-refactor the owner
    *is* a member — same as any other collaborator, just with the
    business_owner project-role. This function reconciles legacy data:

    * For each project with an owner_id, ensure exactly one ProjectMember
      row exists for (project_id, owner_id) with role='business_owner'.
    * Any other rows that mistakenly claim role='business_owner' for the
      same project (shouldn't happen, but be defensive) are demoted to
      'relayops_member'.

    Idempotent — safe to call every startup. Logs but never raises so a
    misconfigured DB doesn't take the app down.
    """
    db = get_db()
    session = db.get_session()
    try:
        projects = session.query(Project).filter(Project.owner_id.isnot(None)).all()
        promoted = 0
        inserted = 0
        demoted = 0
        for project in projects:
            owner_row = (
                session.query(ProjectMember)
                .filter(
                    ProjectMember.project_id == project.id,
                    ProjectMember.user_id == project.owner_id,
                )
                .first()
            )
            if owner_row is None:
                session.add(ProjectMember(
                    project_id=project.id,
                    user_id=project.owner_id,
                    role="business_owner",
                    added_by=project.owner_id,
                ))
                inserted += 1
            elif owner_row.role != "business_owner":
                owner_row.role = "business_owner"
                promoted += 1
            # Defensive: any *other* row claiming bizowner gets demoted.
            stragglers = (
                session.query(ProjectMember)
                .filter(
                    ProjectMember.project_id == project.id,
                    ProjectMember.role == "business_owner",
                    ProjectMember.user_id != project.owner_id,
                )
                .all()
            )
            for row in stragglers:
                row.role = "relayops_member"
                demoted += 1
        session.commit()
        if inserted or promoted or demoted:
            logger.info(
                "Project owner member backfill: inserted={} promoted={} demoted={}",
                inserted, promoted, demoted,
            )
    except Exception:
        session.rollback()
        logger.opt(exception=True).warning("ensure_project_owner_members failed")
    finally:
        session.close()
