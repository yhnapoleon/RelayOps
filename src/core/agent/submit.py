"""Submit pipeline — turn a reviewed draft payload into real entities.

Consumes ONLY the validated draft payload (never chat context). Creation goes
through the existing service layer so CML binding resolution, access checks
and field semantics are exactly what manual creation gets.

Transaction shape: ``project_service.create_project`` manages its own session
(it commits), so the pipeline is project-first, then everything else in one
session. If that second half fails, the session is rolled back AND the
freshly created project row is deleted (FK ondelete=CASCADE clears children),
so a failed submit leaves no half-onboarded project behind.
"""

from __future__ import annotations

from typing import Optional

from api.schemas.app_schemas import AppCreate, ApplicationRecoveryScenarioCreate
from api.schemas.job_schemas import JobCreate, JobFailureScenarioCreate
from core.auth.jwt import CurrentUser
from core.logging import get_logger
from core.models.database import Database
from core.models.entities import Project
from core.services import app_service, job_service, product_service
from core.services import project_service
from core.agent.schemas import OnboardingDraftPayload

logger = get_logger(__name__)


def _scenario_type_or_other(value: str) -> str:
    return value.strip() or "other"


def submit_payload(db: Database, *, payload: OnboardingDraftPayload, actor: CurrentUser) -> dict:
    """Create Project → Products → Jobs/Apps (+ scenarios). Returns a report:
    ``{project_id, products: [...], bindings: [...]}``. Raises on failure
    after compensating (deleting the partially created project)."""
    project = project_service.create_project(
        db,
        name=payload.project.name.strip(),
        description=payload.project.description or "",
        owner_id=actor.user_id,
        owner_group_id=None,
        cml_project_name=payload.project.cml_project_name or None,
        mmp_project_id=payload.project.mmp_project_id or None,
        prod_stat_url=payload.project.prod_stat_url or None,
    )
    project_id = project.id

    report: dict = {"project_id": project_id, "products": [], "bindings": []}
    session = db.get_session()
    try:
        for product_draft in payload.products:
            product = product_service.create(
                session, project_id=project_id, name=product_draft.name.strip(), actor=actor
            )
            session.flush()
            product_report = {"product_id": product.id, "name": product.name, "jobs": [], "apps": []}

            for job_draft in product_draft.jobs:
                job = job_service.create(
                    session,
                    product_id=product.id,
                    body=JobCreate(
                        mmp_project_id=job_draft.mmp_project_id,
                        mmp_model_id=job_draft.mmp_model_id,
                        control_m_job_name=job_draft.control_m_job_name,
                        control_m_cron=job_draft.control_m_cron,
                        cml_project_name=job_draft.cml_project_name,
                        cml_job_name=job_draft.cml_job_name,
                        schedule_cron=job_draft.schedule_cron,
                        description=job_draft.description,
                        dependency_notes=job_draft.dependency_notes,
                        owner_contact=job_draft.owner_contact,
                    ),
                    actor=actor,
                )
                session.flush()
                for sc in job_draft.scenarios:
                    job_service.create_scenario(
                        session,
                        job_id=job.id,
                        body=JobFailureScenarioCreate(
                            scenario_type=_scenario_type_or_other(sc.scenario_type),
                            scenario_name=sc.scenario_name,
                            condition_description=sc.condition_description,
                            diagnostic_steps=sc.diagnostic_steps,
                            action_steps=sc.action_steps,
                            verification_steps=sc.verification_steps,
                            escalation_target=sc.escalation_target,
                            email_template=(
                                None if sc.email_template.is_empty
                                else sc.email_template.model_dump()
                            ),
                        ),
                        actor=actor,
                    )
                product_report["jobs"].append({"job_id": job.id, "cml_job_name": job.cml_job_name})
                report["bindings"].append(_job_binding(job))

            for app_draft in product_draft.apps:
                app = app_service.create(
                    session,
                    product_id=product.id,
                    body=AppCreate(
                        application_url=app_draft.application_url,
                        health_check_url=app_draft.health_check_url,
                        description=app_draft.description,
                        owner_contact=app_draft.owner_contact,
                        cml_project_name=app_draft.cml_project_name,
                        cml_application_name=app_draft.cml_application_name,
                        cml_subdomain=app_draft.cml_subdomain,
                        cml_app_type=app_draft.cml_app_type or "generic",
                    ),
                    actor=actor,
                )
                session.flush()
                for sc in app_draft.scenarios:
                    app_service.create_scenario(
                        session,
                        app_id=app.id,
                        body=ApplicationRecoveryScenarioCreate(
                            scenario_type=_scenario_type_or_other(sc.scenario_type),
                            scenario_name=sc.scenario_name,
                            condition_description=sc.condition_description,
                            action_steps=sc.action_steps,
                            verification_steps=sc.verification_steps,
                            escalation_target=sc.escalation_target,
                            email_template=(
                                None if sc.email_template.is_empty
                                else sc.email_template.model_dump()
                            ),
                        ),
                        actor=actor,
                    )
                product_report["apps"].append(
                    {"app_id": app.id, "cml_application_name": app.cml_application_name}
                )
                report["bindings"].append(_app_binding(app))

            report["products"].append(product_report)
        session.commit()
        return report
    except Exception:
        session.rollback()
        _compensate_delete_project(db, project_id)
        raise
    finally:
        session.close()


def _job_binding(job) -> dict:
    has_cml = bool((job.cml_job_name or "").strip())
    return {
        "entity": "job",
        "id": job.id,
        "name": job.cml_job_name or job.control_m_job_name or f"job-{job.id}",
        "applicable": has_cml,
        "bound": bool(job.cml_job_id) if has_cml else None,
        "error": job.cml_binding_error or "",
    }


def _app_binding(app) -> dict:
    return {
        "entity": "app",
        "id": app.id,
        "name": app.cml_application_name or f"app-{app.id}",
        "applicable": True,
        "bound": bool(app.cml_application_id),
        "error": app.cml_binding_error or "",
    }


def _compensate_delete_project(db: Database, project_id: Optional[int]) -> None:
    """Best-effort cleanup of the project committed before the failure."""
    if not project_id:
        return
    session = db.get_session()
    try:
        project = session.query(Project).filter(Project.id == project_id).first()
        if project is not None:
            session.delete(project)
            session.commit()
            logger.warning("Onboarding submit failed — compensated by deleting project {}", project_id)
    except Exception:
        session.rollback()
        logger.opt(exception=True).error(
            "Onboarding submit failed AND compensation delete of project {} failed — manual cleanup needed",
            project_id,
        )
    finally:
        session.close()
