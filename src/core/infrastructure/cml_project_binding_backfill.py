"""Backfill Ops Project.cml_project_name from existing Job/App rows.

Phase 2B moved the CML project binding from per-Job/per-Application to
per-Project. Existing rows still carry the old per-asset cml_project_name,
so on first startup after the upgrade we lift the value up to the owning
Project — picking the first non-empty value found among its descendants —
so monitoring continues to work without each user manually re-binding.

Idempotent: skips Projects that already have cml_project_name set.
"""

from __future__ import annotations

from typing import Optional

from core.logging import get_logger
from core.models.database import get_db
from core.models.entities import Application, Job, Product, Project

logger = get_logger(__name__)


def _first_non_empty_cml_project_name(session, project_id: int) -> Optional[str]:
    """Return the first non-blank cml_project_name found in this project's children."""
    products = session.query(Product).filter(Product.project_id == project_id).all()
    if not products:
        return None
    product_ids = [p.id for p in products]

    job = (
        session.query(Job)
        .filter(Job.product_id.in_(product_ids), Job.cml_project_name.isnot(None))
        .filter(Job.cml_project_name != "")
        .order_by(Job.id.asc())
        .first()
    )
    if job and (job.cml_project_name or "").strip():
        return job.cml_project_name.strip()

    app = (
        session.query(Application)
        .filter(
            Application.product_id.in_(product_ids),
            Application.cml_project_name.isnot(None),
        )
        .filter(Application.cml_project_name != "")
        .order_by(Application.id.asc())
        .first()
    )
    if app and (app.cml_project_name or "").strip():
        return app.cml_project_name.strip()

    return None


def backfill_project_cml_binding() -> dict:
    """Lift cml_project_name from Job/App rows up to the owning Project.

    Returns counts for telemetry.
    """
    db = get_db()
    session = db.get_session()
    stats = {"projects_scanned": 0, "projects_backfilled": 0, "resolved_ids": 0}
    try:
        from core.services.cml_binding_resolver import (
            build_control_interface,
            resolve_project_id as _resolve_cml_project_id,
        )
        # Lazy: only build the control interface if at least one project needs it.
        control = None

        projects = session.query(Project).order_by(Project.id.asc()).all()
        for project in projects:
            stats["projects_scanned"] += 1
            if (project.cml_project_name or "").strip():
                continue  # Already bound.
            inherited = _first_non_empty_cml_project_name(session, project.id)
            if not inherited:
                continue
            project.cml_project_name = inherited
            stats["projects_backfilled"] += 1

            if control is None:
                control = build_control_interface()
            pid, perr = _resolve_cml_project_id(control, inherited)
            project.cml_project_id = pid
            project.cml_binding_error = perr
            if pid:
                stats["resolved_ids"] += 1

            logger.info(
                "Backfilled CML project binding on project {} -> name={!r}, id={}, err={}",
                project.id, inherited, pid, perr,
            )

        session.commit()
    except Exception:
        session.rollback()
        logger.opt(exception=True).warning("Failed to backfill CML project bindings")
        raise
    finally:
        session.close()

    if stats["projects_backfilled"]:
        logger.info("CML project binding backfill: {}", stats)
    return stats
