"""Project business logic — CRUD operations with access control."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal, Optional

from core.models.database import Database
from core.models.entities import Project, ProjectMember
from core.logging import get_logger
from core.services.support_group_service import is_project_editor, resolve_support_group_snapshot, user_has_project_group_access

logger = get_logger(__name__)

ProjectAccessResult = Literal["ok", "not_found", "forbidden"]
ProjectMutationResult = Literal["ok", "not_found", "forbidden", "system_locked"]


@dataclass
class ProjectFetchResult:
    project: Optional[Project]
    status: ProjectAccessResult


@dataclass
class ProjectMutationResponse:
    project: Optional[Project]
    status: ProjectMutationResult
    old_value: Optional[dict]


def create_project(
    db: Database,
    *,
    name: str,
    description: Optional[str],
    owner_id: int,
    owner_group_id: Optional[int],
    cml_project_name: Optional[str] = None,
    cml_project_id: Optional[str] = None,
    mmp_project_id: Optional[str] = None,
    prod_stat_url: Optional[str] = None,
) -> Project:
    """Create a project and resolve its owner-group snapshot if provided.

    When ``cml_project_name`` is provided, the resolver is best-effort: a
    CML outage at create time leaves ``cml_project_id`` NULL and surfaces
    the failure via ``cml_binding_error`` so the UI can prompt for retry.

    When the caller also supplies ``cml_project_id`` (i.e. the UI picked
    from the CML project list), the name→id resolve step is skipped and the
    id is persisted as-is. This avoids the "duplicate name" ambiguity error
    that the name-only path can hit when the user has multiple CML projects
    sharing the chosen name.
    """
    session = db.get_session()
    try:
        project = Project(
            name=name,
            description=description or "",
            owner_id=owner_id,
            owner_group_id=None,
            owner_group_name_snapshot="",
            is_system=0,
            prod_stat_url=(prod_stat_url or "").strip(),
        )
        if owner_group_id is not None:
            resolved_group_id, owner_group_name = resolve_support_group_snapshot(session, owner_group_id)
            project.owner_group_id = resolved_group_id
            project.owner_group_name_snapshot = owner_group_name

        cml_name = (cml_project_name or "").strip()
        cml_id_hint = (cml_project_id or "").strip()
        project.cml_project_name = cml_name
        if cml_name and cml_id_hint:
            project.cml_project_id = cml_id_hint
            project.cml_binding_error = None
        elif cml_name:
            pid, perr = _resolve_cml_project(cml_name)
            project.cml_project_id = pid
            project.cml_binding_error = perr

        project.mmp_project_id = (mmp_project_id or "").strip()

        session.add(project)
        session.flush()
        # The creator is the project's business owner — mirror that into a
        # ProjectMember row so the Members UI lists them naturally and the
        # "exactly one bizowner per project" invariant has a row to point
        # at. Done in the same transaction as the project insert so a
        # half-state (project without an owner member row) is impossible.
        session.add(ProjectMember(
            project_id=project.id,
            user_id=owner_id,
            role="business_owner",
            added_by=owner_id,
        ))
        # No children yet on a brand-new project, but call the cascade for
        # symmetry — it's a no-op when there are no products.
        _cascade_rebind_children(session, project)
        session.commit()
        session.refresh(project)
        return project
    except Exception:
        session.rollback()
        logger.opt(exception=True).error("Failed to create project")
        raise
    finally:
        session.close()


def _resolve_cml_project(cml_project_name: str) -> tuple[Optional[str], Optional[str]]:
    """Wrapper around the CML binding resolver — keeps service-layer code clean."""
    from core.services.cml_binding_resolver import (
        build_control_interface,
        resolve_project_id as _resolve_cml_project_id,
    )
    control = build_control_interface()
    return _resolve_cml_project_id(control, cml_project_name)


def _cascade_rebind_children(session, project: Project) -> None:
    """When a Project's CML binding changes, refresh every INHERITING
    Job/App under it.

    A row's ``cml_project_name`` field is the per-asset override sentinel:
    empty = inherit from parent, non-empty = override pinned to a different
    CML project. Override rows MUST be skipped here — they live independent
    of the parent's binding, and overwriting them would silently flip them
    back to the parent (destroying the user's explicit choice and
    re-resolving cml_job_id / cml_application_id against the wrong project).

    Inheritor rows are NOT stamped with the parent's name here — their
    ``cml_project_name`` stays empty so the sentinel survives. Only the
    cached ids (cml_project_id / cml_job_id / cml_application_id) are
    refreshed.
    """
    from core.models.entities import Application, Job, Product
    from core.services.cml_binding_resolver import (
        build_control_interface,
        resolve_application_id as _resolve_cml_application_id,
        resolve_job_id as _resolve_cml_job_id,
    )

    products = session.query(Product).filter(Product.project_id == project.id).all()
    if not products:
        return
    product_ids = [p.id for p in products]

    pid = (project.cml_project_id or "").strip()

    jobs = session.query(Job).filter(Job.product_id.in_(product_ids)).all()
    apps = session.query(Application).filter(Application.product_id.in_(product_ids)).all()

    if not pid:
        # Project no longer bound — clear inherited cache on every child so
        # the UI badge truthfully shows "pending" / "unconfigured". Skip
        # override rows: their binding is independent of the parent.
        unresolved_msg = (
            f"Project unresolved: {project.cml_binding_error}"
            if project.cml_binding_error
            else "Owning Ops Project has no CML Project Name set"
        )
        for job in jobs:
            if (job.cml_project_name or "").strip():
                continue
            job.cml_project_id = None
            job.cml_job_id = None
            job.cml_binding_error = unresolved_msg if (job.cml_job_name or job.control_m_job_name) else None
        for app in apps:
            if (app.cml_project_name or "").strip():
                continue
            app.cml_project_id = None
            app.cml_application_id = None
            app.cml_binding_error = unresolved_msg if (app.cml_application_name or app.cml_subdomain) else None
        return

    control = build_control_interface()
    for job in jobs:
        if (job.cml_project_name or "").strip():
            continue
        job.cml_project_id = pid
        job_name = (job.cml_job_name or job.control_m_job_name or "").strip()
        if not job_name:
            job.cml_job_id = None
            job.cml_binding_error = None
            continue
        jid, jerr = _resolve_cml_job_id(control, pid, job_name)
        job.cml_job_id = jid
        job.cml_binding_error = jerr
    for app in apps:
        if (app.cml_project_name or "").strip():
            continue
        app.cml_project_id = pid
        if not (app.cml_application_name or app.cml_subdomain):
            app.cml_application_id = None
            app.cml_binding_error = None
            continue
        aid, aerr = _resolve_cml_application_id(
            control, pid,
            name=app.cml_application_name or None,
            subdomain=app.cml_subdomain or None,
        )
        app.cml_application_id = aid
        app.cml_binding_error = aerr


def list_projects_for_user(
    db: Database,
    *,
    user_id: int,
    is_admin: bool,
    ad_groups: Optional[list],
) -> list[Project]:
    """List projects visible to a user based on role, membership, and group access."""
    session = db.get_session()
    try:
        if is_admin:
            return session.query(Project).order_by(Project.created_at.desc()).all()

        owned = session.query(Project).filter(Project.owner_id == user_id).all()
        system_projects = session.query(Project).filter(Project.is_system == 1).all()

        member_project_ids = [
            row.project_id
            for row in session.query(ProjectMember.project_id)
            .filter(ProjectMember.user_id == user_id)
            .all()
        ]
        member_projects = (
            session.query(Project).filter(Project.id.in_(member_project_ids)).all()
            if member_project_ids
            else []
        )
        group_projects = [
            project
            for project in session.query(Project).all()
            if project.is_system != 1 and user_has_project_group_access(session, project, ad_groups)
        ]

        seen = {project.id for project in owned}
        result = list(owned)
        for project in member_projects:
            if project.id not in seen:
                result.append(project)
                seen.add(project.id)
        for project in group_projects:
            if project.id not in seen:
                result.append(project)
                seen.add(project.id)
        for project in system_projects:
            if project.id not in seen:
                result.append(project)
                seen.add(project.id)

        result.sort(key=lambda project: project.created_at, reverse=True)
        return result
    finally:
        session.close()


def get_project_for_user(
    db: Database,
    *,
    project_id: int,
    user_id: int,
    is_admin: bool,
    ad_groups: Optional[list],
) -> ProjectFetchResult:
    """Fetch a single project with access control evaluation."""
    session = db.get_session()
    try:
        project = session.query(Project).filter(Project.id == project_id).first()
        if project is None:
            return ProjectFetchResult(project=None, status="not_found")
        if project.is_system == 1:
            return ProjectFetchResult(project=project, status="ok")
        if is_admin or project.owner_id == user_id:
            return ProjectFetchResult(project=project, status="ok")

        is_member = (
            session.query(ProjectMember)
            .filter(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
            .first()
            is not None
        )
        if is_member or user_has_project_group_access(session, project, ad_groups):
            return ProjectFetchResult(project=project, status="ok")
        return ProjectFetchResult(project=None, status="forbidden")
    finally:
        session.close()


def update_project(
    db: Database,
    *,
    project_id: int,
    actor_user_id: int,
    is_admin: bool,
    name: Optional[str],
    description: Optional[str],
    owner_group_id: Optional[int],
    cml_project_name: Optional[str] = None,
    cml_project_id: Optional[str] = None,
    mmp_project_id: Optional[str] = None,
    prod_stat_url: Optional[str] = None,
    serializer,
) -> ProjectMutationResponse:
    """Update a project if the caller is allowed to mutate it.

    When ``cml_project_id`` is provided alongside ``cml_project_name`` (i.e.
    the UI picked a project from the search dropdown), the id is persisted
    directly and the name→id resolve step is skipped — matching the
    create-time shortcut.
    """
    session = db.get_session()
    try:
        project = session.query(Project).filter(Project.id == project_id).first()
        if project is None:
            return ProjectMutationResponse(project=None, status="not_found", old_value=None)
        if project.is_system == 1:
            return ProjectMutationResponse(project=None, status="system_locked", old_value=None)
        # product_member is a project editor and may adjust project settings;
        # member management, ownership transfer and project deletion stay
        # owner/admin-only.
        if (
            not is_admin
            and project.owner_id != actor_user_id
            and not is_project_editor(session, project_id, actor_user_id)
        ):
            return ProjectMutationResponse(project=None, status="forbidden", old_value=None)

        old_value = serializer(project)

        if name is not None:
            project.name = name
        if description is not None:
            project.description = description
        if prod_stat_url is not None:
            project.prod_stat_url = prod_stat_url.strip()
        if owner_group_id is not None:
            resolved_group_id, owner_group_name = resolve_support_group_snapshot(session, owner_group_id)
            project.owner_group_id = resolved_group_id
            project.owner_group_name_snapshot = owner_group_name
        cml_changed = False
        if cml_project_name is not None:
            new_name = cml_project_name.strip()
            new_id_hint = (cml_project_id or "").strip()
            if new_name != (project.cml_project_name or ""):
                cml_changed = True
            project.cml_project_name = new_name
            if new_name and new_id_hint:
                if new_id_hint != (project.cml_project_id or "") or project.cml_binding_error:
                    cml_changed = True
                project.cml_project_id = new_id_hint
                project.cml_binding_error = None
            elif new_name:
                pid, perr = _resolve_cml_project(new_name)
                if pid != project.cml_project_id or perr != project.cml_binding_error:
                    cml_changed = True
                project.cml_project_id = pid
                project.cml_binding_error = perr
            else:
                if project.cml_project_id or project.cml_binding_error:
                    cml_changed = True
                project.cml_project_id = None
                project.cml_binding_error = None
        if mmp_project_id is not None:
            project.mmp_project_id = mmp_project_id.strip()
        project.updated_at = datetime.utcnow()
        session.flush()

        if cml_changed:
            _cascade_rebind_children(session, project)

        session.commit()
        session.refresh(project)
        return ProjectMutationResponse(project=project, status="ok", old_value=old_value)
    except Exception:
        session.rollback()
        logger.opt(exception=True).error("Failed to update project {}", project_id)
        raise
    finally:
        session.close()


def resolve_project_binding(
    db: Database,
    *,
    project_id: int,
    actor_user_id: int,
    is_admin: bool,
) -> ProjectMutationResponse:
    """Re-attempt CML name → id resolution for this Project and persist.

    Read-side only — does not require owner role; any user who can see the
    project can trigger a refresh because it doesn't change configuration,
    only the cached lookup id.
    """
    session = db.get_session()
    try:
        project = session.query(Project).filter(Project.id == project_id).first()
        if project is None:
            return ProjectMutationResponse(project=None, status="not_found", old_value=None)
        # System projects are read-only for config but the cache refresh is harmless.
        if not is_admin and project.owner_id != actor_user_id and project.is_system != 1:
            # Defer access to the route layer for non-owner non-admin; the route
            # already gates with get_current_user. Anyone allowed to see it can refresh.
            pass

        cml_name = (project.cml_project_name or "").strip()
        if not cml_name:
            project.cml_project_id = None
            project.cml_binding_error = None
        else:
            pid, perr = _resolve_cml_project(cml_name)
            project.cml_project_id = pid
            project.cml_binding_error = perr
        project.updated_at = datetime.utcnow()
        session.flush()
        # Cascade so children pick up the freshly-resolved (or freshly-failed) state.
        _cascade_rebind_children(session, project)
        session.commit()
        session.refresh(project)
        return ProjectMutationResponse(project=project, status="ok", old_value=None)
    except Exception:
        session.rollback()
        logger.opt(exception=True).error("Failed to resolve project binding {}", project_id)
        raise
    finally:
        session.close()


def delete_project(
    db: Database,
    *,
    project_id: int,
    actor_user_id: int,
    is_admin: bool,
    serializer,
) -> ProjectMutationResponse:
    """Delete a project + all dependents via explicit bottom-up DELETEs.

    Why not ``session.delete(project)`` and let cascade do it: YugabyteDB
    (and PG under some isolation modes) raises ``tuple concurrently
    deleted`` when the DB-level FK ON DELETE CASCADE engine touches the
    same row from multiple cascade paths, regardless of ORM settings.
    Going bottom-up with one DELETE per table makes every step a plain
    delete with no children left to cascade, side-stepping the race.

    Even with no cascade, YugabyteDB occasionally raises
    ``tuple concurrently deleted`` on a plain DELETE — that error's
    semantics literally mean "the row you wanted to delete is already
    gone", which for us is the desired outcome. Each step is therefore
    wrapped in a SAVEPOINT so we can tolerate that specific error and
    keep going without losing the outer transaction.
    """
    import time
    from sqlalchemy import text
    from sqlalchemy.exc import InternalError

    def _exec(sql: str, params: dict, retries: int = 5) -> None:
        """Run one DELETE/UPDATE inside a savepoint; on YugabyteDB's
        ``tuple concurrently deleted`` (an MVCC conflict, *not* a
        "row is already gone" signal — savepoint rollback drops the
        DELETE intent), retry the statement up to ``retries`` times
        with exponential backoff. If we exhaust retries we bubble up
        — far better than silently leaving the row in place.
        """
        last_exc: Optional[InternalError] = None
        for attempt in range(retries):
            sp = session.begin_nested()
            try:
                session.execute(text(sql), params)
                sp.commit()
                return
            except InternalError as exc:
                sp.rollback()
                if "tuple concurrently deleted" not in str(exc).lower():
                    raise
                last_exc = exc
                logger.warning(
                    "YugabyteDB MVCC conflict on attempt {}/{}: {}",
                    attempt + 1, retries, sql.splitlines()[0],
                )
                time.sleep(0.05 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    session = db.get_session()
    try:
        project = session.query(Project).filter(Project.id == project_id).first()
        if project is None:
            return ProjectMutationResponse(project=None, status="not_found", old_value=None)
        if project.is_system == 1:
            return ProjectMutationResponse(project=None, status="system_locked", old_value=None)
        if not is_admin and project.owner_id != actor_user_id:
            return ProjectMutationResponse(project=None, status="forbidden", old_value=None)

        old_value = serializer(project)
        product_ids = [p.id for p in project.products]

        # Drop the ORM-loaded Project from the session so commit() below
        # doesn't try to re-delete it via the unit of work after we
        # already wiped it with raw SQL. Expunge cascades through the
        # products relationship (cascade='all, delete-orphan' implies
        # 'expunge'), so the children come out too — no need to expunge
        # them individually (doing so would raise "not in session").
        session.expunge(project)

        # ── leaf tables first ────────────────────────────────────────
        if product_ids:
            # job-level leaves
            _exec(
                "DELETE FROM job_failure_scenarios WHERE job_id IN "
                "(SELECT id FROM jobs WHERE product_id = ANY(:pids))",
                {"pids": product_ids},
            )
            _exec(
                "DELETE FROM job_executions WHERE job_id IN "
                "(SELECT id FROM jobs WHERE product_id = ANY(:pids))",
                {"pids": product_ids},
            )
            # app-level leaves
            _exec(
                "DELETE FROM application_recovery_scenarios WHERE application_id IN "
                "(SELECT id FROM applications WHERE product_id = ANY(:pids))",
                {"pids": product_ids},
            )
            _exec(
                "DELETE FROM application_health_checks WHERE application_id IN "
                "(SELECT id FROM applications WHERE product_id = ANY(:pids))",
                {"pids": product_ids},
            )
            # issues that reference this project's products/jobs/apps
            _exec(
                "DELETE FROM issues WHERE product_id = ANY(:pids) "
                "OR job_id IN (SELECT id FROM jobs WHERE product_id = ANY(:pids)) "
                "OR app_id IN (SELECT id FROM applications WHERE product_id = ANY(:pids))",
                {"pids": product_ids},
            )

            # Break the Product ↔ ProductVersion FK cycle before we
            # touch product_versions: clear the two product columns
            # that point INTO product_versions (no ondelete on those).
            _exec(
                "UPDATE products SET current_draft_version_id = NULL, "
                "current_approved_version_id = NULL "
                "WHERE project_id = :pid",
                {"pid": project_id},
            )

            # Break the SELF-reference inside product_versions
            # (derived_from_version_id -> product_versions.id, no
            # ondelete). YugabyteDB struggles deleting a batch with
            # interlocking self-refs and surfaces it as the same
            # "tuple concurrently deleted" error.
            _exec(
                "UPDATE product_versions SET derived_from_version_id = NULL "
                "WHERE product_id = ANY(:pids)",
                {"pids": product_ids},
            )

            # Pre-clear inbound SET NULL refs from issues so the DELETE
            # on product_versions doesn't fire an implicit per-row
            # UPDATE on issues. That trigger racing the main DELETE is
            # the most likely cause of the persistent
            # "tuple concurrently deleted" error in the prior attempts.
            _exec(
                "UPDATE issues SET product_version_id = NULL "
                "WHERE product_version_id IN "
                "(SELECT id FROM product_versions WHERE product_id = ANY(:pids))",
                {"pids": product_ids},
            )

            # ── mid tables (children of products) ───────────────────
            _exec(
                "DELETE FROM product_versions WHERE product_id = ANY(:pids)",
                {"pids": product_ids},
            )
            _exec(
                "DELETE FROM jobs WHERE product_id = ANY(:pids)",
                {"pids": product_ids},
            )
            _exec(
                "DELETE FROM applications WHERE product_id = ANY(:pids)",
                {"pids": product_ids},
            )

            # ── products themselves ─────────────────────────────────
            _exec(
                "DELETE FROM products WHERE project_id = :pid",
                {"pid": project_id},
            )

        # ── project-level dependents ────────────────────────────────
        _exec(
            "DELETE FROM project_members WHERE project_id = :pid",
            {"pid": project_id},
        )
        _exec(
            "DELETE FROM project_support_groups WHERE project_id = :pid",
            {"pid": project_id},
        )

        # ── finally the project ─────────────────────────────────────
        _exec(
            "DELETE FROM projects WHERE id = :pid",
            {"pid": project_id},
        )

        # Sanity check before committing — if the project row somehow
        # survived (e.g. all retries failed and we got here via some
        # other code path), refuse to return ok. Better a clean 500
        # than a silent leak with the row still in place.
        still_there = session.execute(
            text("SELECT 1 FROM projects WHERE id = :pid"),
            {"pid": project_id},
        ).scalar()
        if still_there:
            session.rollback()
            raise RuntimeError(
                f"delete_project: project {project_id} row still exists "
                f"after all DELETE statements — aborting to avoid silent leak"
            )

        session.commit()
        return ProjectMutationResponse(project=None, status="ok", old_value=old_value)
    except Exception:
        session.rollback()
        logger.opt(exception=True).error("Failed to delete project {}", project_id)
        raise
    finally:
        session.close()


# ── Copy ─────────────────────────────────────────────────────────────


def next_copy_name(existing_names: Iterable[str], base: str) -> str:
    """Pick the first non-colliding "Foo (copy)" / "Foo (copy 2)" variant.

    Lets the UI duplicate a project/product without forcing the user to
    invent a new name. Suffix bumps only on collision; the most common
    case (first copy of a unique name) lands on plain "(copy)".
    """
    taken = {n for n in existing_names if n}
    candidate = f"{base} (copy)"
    if candidate not in taken:
        return candidate
    n = 2
    while True:
        candidate = f"{base} (copy {n})"
        if candidate not in taken:
            return candidate
        n += 1


def _clone_job(source) -> "object":
    """Build a detached Job row mirroring ``source`` minus runtime state.

    CML binding fields (cml_project_*, cml_job_*, cml_binding_error) are
    preserved per user spec ("绑定的project和其中的assets不变"). Runtime
    polling artifacts (last_checked_at) reset so the new asset looks
    "never polled" until the next check cycle picks it up.
    """
    from core.models.entities import Job

    return Job(
        product_id=None,  # caller sets after the new product is flushed
        mmp_project_id=source.mmp_project_id,
        mmp_model_id=source.mmp_model_id,
        control_m_job_name=source.control_m_job_name,
        control_m_cron=source.control_m_cron,
        cml_project_name=source.cml_project_name,
        cml_job_name=source.cml_job_name,
        cml_project_id=source.cml_project_id,
        cml_job_id=source.cml_job_id,
        cml_binding_error=source.cml_binding_error,
        schedule_cron=source.schedule_cron,
        description=source.description,
        dependencies=source.dependencies,
        failure_strategy_summary=source.failure_strategy_summary,
        dependency_notes=source.dependency_notes,
        owner_contact=source.owner_contact,
        support_group_id=source.support_group_id,
        support_group_name_snapshot=source.support_group_name_snapshot,
        support_group=source.support_group,
        runbook_required=source.runbook_required,
        has_mmp_dependency=source.has_mmp_dependency,
        sla_preset=source.sla_preset,
        sla_custom_minutes=source.sla_custom_minutes,
        is_system=0,
        last_checked_at=None,
    )


def _clone_job_scenario(source) -> "object":
    from core.models.entities import JobFailureScenario

    return JobFailureScenario(
        job_id=None,
        scenario_type=source.scenario_type,
        scenario_name=source.scenario_name,
        condition_description=source.condition_description,
        detection_source=source.detection_source,
        diagnostic_steps=source.diagnostic_steps,
        action_steps=source.action_steps,
        verification_steps=source.verification_steps,
        escalation_target=source.escalation_target,
        email_template=source.email_template,
        fallback_owner_type=source.fallback_owner_type,
        threshold_operator=source.threshold_operator,
        threshold_value=source.threshold_value,
        threshold_feature_list=source.threshold_feature_list,
        is_not_applicable=source.is_not_applicable,
        not_applicable_signoff_by=source.not_applicable_signoff_by,
        not_applicable_signoff_at=source.not_applicable_signoff_at,
        is_active=source.is_active,
    )


def _clone_application(source) -> "object":
    from core.models.entities import Application

    return Application(
        product_id=None,
        application_url=source.application_url,
        health_check_url=source.health_check_url,
        cml_project_name=source.cml_project_name,
        cml_application_name=source.cml_application_name,
        cml_subdomain=source.cml_subdomain,
        cml_app_type=source.cml_app_type,
        cml_project_id=source.cml_project_id,
        cml_application_id=source.cml_application_id,
        cml_serving_url=source.cml_serving_url,
        cml_binding_error=source.cml_binding_error,
        last_cml_status=None,
        last_relayops_health=None,
        last_checked_at=None,
        last_check_error=None,
        description=source.description,
        restart_supported=source.restart_supported,
        restart_summary=source.restart_summary,
        owner_contact=source.owner_contact,
        support_group_id=source.support_group_id,
        support_group_name_snapshot=source.support_group_name_snapshot,
        support_group=source.support_group,
        is_system=0,
    )


def _clone_app_scenario(source) -> "object":
    from core.models.entities import ApplicationRecoveryScenario

    return ApplicationRecoveryScenario(
        application_id=None,
        scenario_type=source.scenario_type,
        scenario_name=source.scenario_name,
        condition_description=source.condition_description,
        action_steps=source.action_steps,
        verification_steps=source.verification_steps,
        escalation_target=source.escalation_target,
        fallback_owner_type=source.fallback_owner_type,
        email_template=source.email_template,
        is_not_applicable=source.is_not_applicable,
        not_applicable_signoff_by=source.not_applicable_signoff_by,
        not_applicable_signoff_at=source.not_applicable_signoff_at,
        is_active=source.is_active,
    )


def _deep_clone_product_assets(session, source_product, new_product) -> None:
    """Copy every job/app (+ scenarios) from ``source_product`` into
    ``new_product``. Caller must have flushed ``new_product`` so its id
    is available for FK assignment."""
    from core.models.entities import Application, Job

    src_jobs = session.query(Job).filter(Job.product_id == source_product.id).all()
    for src_job in src_jobs:
        new_job = _clone_job(src_job)
        new_job.product_id = new_product.id
        session.add(new_job)
        session.flush()
        for scenario in src_job.failure_scenarios:
            cloned = _clone_job_scenario(scenario)
            cloned.job_id = new_job.id
            session.add(cloned)

    src_apps = session.query(Application).filter(Application.product_id == source_product.id).all()
    for src_app in src_apps:
        new_app = _clone_application(src_app)
        new_app.product_id = new_product.id
        session.add(new_app)
        session.flush()
        for scenario in src_app.recovery_scenarios:
            cloned = _clone_app_scenario(scenario)
            cloned.application_id = new_app.id
            session.add(cloned)


def copy_project(
    db: Database,
    *,
    source_project_id: int,
    actor_user_id: int,
    is_admin: bool,
    ad_groups: Optional[list],
) -> ProjectMutationResponse:
    """Duplicate a project + every product/job/app/scenario under it.

    Naming: the new project's name is the source's name with a "(copy)"
    suffix (or "(copy 2)" / "(copy 3)" if that collides). Product names
    are kept verbatim — only the top-level entity that the user clicked
    "Copy" on gets the suffix.

    Ownership: the actor becomes the new project's business owner. The
    source's project_members list is NOT carried over (per user choice
    in the copy flow design).

    Version control: the new project starts at version "1" in draft —
    the source's version chain isn't relevant to a brand-new project.
    """
    from core.models.entities import Application, Job, Product

    session = db.get_session()
    try:
        source = session.query(Project).filter(Project.id == source_project_id).first()
        if source is None:
            return ProjectMutationResponse(project=None, status="not_found", old_value=None)

        # Access check mirrors get_project_for_user: admin / owner /
        # member / group-access can all copy. System projects copy fine —
        # the result is a normal user-owned project.
        is_owner_or_member = (
            is_admin
            or source.owner_id == actor_user_id
            or session.query(ProjectMember)
            .filter(
                ProjectMember.project_id == source_project_id,
                ProjectMember.user_id == actor_user_id,
            )
            .first()
            is not None
            or user_has_project_group_access(session, source, ad_groups)
            or source.is_system == 1
        )
        if not is_owner_or_member:
            return ProjectMutationResponse(project=None, status="forbidden", old_value=None)

        existing_names = [row.name for row in session.query(Project.name).all()]
        new_name = next_copy_name(existing_names, source.name)

        new_project = Project(
            name=new_name,
            description=source.description or "",
            owner_id=actor_user_id,
            owner_group_id=source.owner_group_id,
            owner_group_name_snapshot=source.owner_group_name_snapshot or "",
            is_system=0,
            cml_project_name=source.cml_project_name or "",
            cml_project_id=source.cml_project_id,
            cml_binding_error=source.cml_binding_error,
            prod_stat_url=source.prod_stat_url or "",
            latest_version_number="1",
        )
        session.add(new_project)
        session.flush()
        session.add(ProjectMember(
            project_id=new_project.id,
            user_id=actor_user_id,
            role="business_owner",
            added_by=actor_user_id,
        ))

        source_products = session.query(Product).filter(Product.project_id == source_project_id).all()
        for src_product in source_products:
            new_product = Product(
                project_id=new_project.id,
                name=src_product.name,
                latest_version_number="1",
                is_system=0,
            )
            session.add(new_product)
            session.flush()
            _deep_clone_product_assets(session, src_product, new_product)

        session.commit()
        session.refresh(new_project)
        return ProjectMutationResponse(project=new_project, status="ok", old_value=None)
    except Exception:
        session.rollback()
        logger.opt(exception=True).error("Failed to copy project {}", source_project_id)
        raise
    finally:
        session.close()
