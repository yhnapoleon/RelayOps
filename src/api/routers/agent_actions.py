"""Write-action confirm endpoints (propose → confirm).

The assistant's write turn produces a :class:`WriteProposal` and persists it
behind a confirm token. These endpoints let the UI preview that proposal and
confirm it. Confirmation is **deterministic and LLM-free**: it claims the token
(single consume), then dispatches to the existing service write. Because the
token is DB-persisted, any worker/replica can serve the confirm.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from core.agent import write_proposal_store as store
from core.agent.write_schemas import WriteProposal
from core.config import get_config
from core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    SystemLockedError,
    ValidationError,
)
from core.logging import get_logger
from core.models.constants import IssueActionType, IssueStatus
from core.models.user import is_elevated_role
from core.services import issue_service

logger = get_logger(__name__)

router = APIRouter(prefix="/api/agent/action", tags=["agent"])

# take_valid reason → HTTP status for the not-OK cases.
_REASON_STATUS = {
    store.TAKE_NOT_FOUND: 404,
    store.TAKE_FORBIDDEN: 403,
    store.TAKE_EXPIRED: 410,
    store.TAKE_CONSUMED: 409,
}


def _resolve(session: Session, token: str, current_user: CurrentUser) -> WriteProposal:
    proposal, reason = store.take_valid(session, token=token, actor_id=current_user.user_id)
    if reason != store.TAKE_OK:
        raise HTTPException(status_code=_REASON_STATUS.get(reason, 400), detail=reason)
    return proposal


@router.get("/{token}")
def preview_action(
    token: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Render the pending proposal (for the confirm card). Read-only."""
    return _resolve(session, token, current_user).model_dump()


@router.post("/{token}/confirm")
def confirm_action(
    token: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Confirm + commit a pending proposal. Claims the token (single consume)
    then dispatches the deterministic service write. Does not use the LLM."""
    proposal = _resolve(session, token, current_user)
    # Claim first → exactly-once (a concurrent confirm gets 409).
    if not store.consume(session, token=token):
        raise HTTPException(status_code=409, detail=store.TAKE_CONSUMED)

    try:
        result = _commit_proposal(proposal, current_user, session)
    except SystemLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (ValidationError,) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    logger.info("write confirmed: kind={} entity={}#{} by user={}",
                proposal.kind, proposal.entity_type, proposal.entity_id, current_user.user_id)
    return {"status": "confirmed", "kind": proposal.kind, "result": result}


def _commit_proposal(proposal: WriteProposal, actor: CurrentUser, session: Session) -> dict:
    """Deterministic dispatch by kind. The service re-checks RBAC + state machine
    + writes audit (double gate). ``session`` is used by the job path (which goes
    through the project version flow); the issue path uses its own service db."""
    is_admin = actor.username in get_config().platform_owners
    is_elevated = is_elevated_role(actor.role) or is_admin

    if proposal.kind in ("false_positive", "resolve_issue", "update_issue_status"):
        new_status = _target_status(proposal)
        resolution = next(
            (c.rationale for c in proposal.changes if c.field_path == "status"), ""
        )
        issue = issue_service.update_issue(
            issue_id=proposal.entity_id,
            actor_user_id=actor.user_id,
            actor_username=actor.username,
            is_admin=is_admin,
            is_elevated=is_elevated,
            new_status=new_status,
            resolution_description=resolution or None,
        )
        return {"entity": "issue", "issue_id": issue.id, "new_status": issue.status}

    if proposal.kind == "record_step":
        note = next((c.new_value for c in proposal.changes), "")
        issue = issue_service.run_action(
            issue_id=proposal.entity_id,
            action=IssueActionType.RECORD_STEP,
            actor_user_id=actor.user_id,
            actor_username=actor.username,
            is_admin=is_elevated,
            notes=note,
        )
        return {"entity": "issue", "issue_id": issue.id, "action": "record_step"}

    if proposal.kind == "job_sla":
        from api.schemas.job_schemas import JobUpdate
        from core.services import job_service

        fields: dict = {}
        for c in proposal.changes:
            if c.field_path == "sla_custom_minutes":
                fields["sla_custom_minutes"] = int(c.new_value)
            elif c.field_path == "schedule_cron":
                fields["schedule_cron"] = c.new_value
        # Goes through job_service.update → project editable-draft/version flow.
        # ConflictError (handover under review) / SystemLockedError surface as 409.
        job = job_service.update(session, job_id=proposal.entity_id, body=JobUpdate(**fields), actor=actor)
        return {"entity": "job", "job_id": job.id, "commit_path": "version_flow"}

    if proposal.kind == "edit_project":
        return _commit_edit_project(proposal, actor, is_elevated)

    if proposal.kind == "edit_product":
        from core.services import product_service

        name = next((c.new_value for c in proposal.changes if c.field_path == "name"), None)
        product = product_service.update(session, product_id=proposal.entity_id, name=name, actor=actor)
        return {"entity": "product", "product_id": product.id, "commit_path": "version_flow"}

    if proposal.kind == "set_member_role":
        return _commit_set_member_role(proposal, actor, session)

    raise ValidationError(f"Unsupported proposal kind for confirm: {proposal.kind}")


def _commit_edit_project(proposal: WriteProposal, actor: CurrentUser, is_elevated: bool) -> dict:
    """Descriptive-field project edit via the existing service (its own db). The
    service re-checks RBAC + drops anything outside its parameters (double gate)."""
    from core.models.database import get_db
    from core.services import project_service
    from core.services.audit_service import serialize_project

    by_path = {c.field_path: c.new_value for c in proposal.changes}
    response = project_service.update_project(
        get_db(),
        project_id=proposal.entity_id,
        actor_user_id=actor.user_id,
        is_admin=is_elevated,
        name=by_path.get("name"),
        description=by_path.get("description"),
        owner_group_id=None,
        prod_stat_url=by_path.get("prod_stat_url"),
        serializer=serialize_project,
    )
    if response.status == "not_found":
        raise NotFoundError("Project not found")
    if response.status == "system_locked":
        raise SystemLockedError("System-managed project is read-only")
    if response.status == "forbidden":
        raise ForbiddenError("Not authorized to edit this project")
    return {"entity": "project", "project_id": response.project.id}


def _commit_set_member_role(proposal: WriteProposal, actor: CurrentUser, session: Session) -> dict:
    """Deterministic per-project role change. Mirrors the members router gate
    (admin / project owner / self) and the business_owner invariant — member
    management is NOT covered by the relayops_member elevation (Phase R)."""
    from core.models.entities import Project, ProjectMember
    from core.services.audit_service import log_audit

    change = next((c for c in proposal.changes if c.field_path.endswith(".role")), None)
    if change is None:
        raise ValidationError("Member-role proposal has no role change")
    user_id = int(change.field_path.split(".")[1])
    new_role = change.new_value
    if new_role not in ("product_member", "relayops_member"):
        raise ValidationError(f"Unsupported member role: {new_role!r}")

    project = session.query(Project).filter(Project.id == proposal.entity_id).first()
    if project is None:
        raise NotFoundError("Project not found")
    if project.is_system == 1:
        raise SystemLockedError("System-managed project is read-only")
    strict_admin = actor.role == "admin" or actor.username in get_config().platform_owners
    if not (strict_admin or project.owner_id == actor.user_id or user_id == actor.user_id):
        raise ForbiddenError("Not authorized to change this member's role")

    pm = (session.query(ProjectMember)
          .filter(ProjectMember.project_id == proposal.entity_id, ProjectMember.user_id == user_id)
          .first())
    if pm is None:
        raise NotFoundError("Member not found in this project")
    if pm.role == "business_owner":
        raise ValidationError("Cannot change the business owner's role; transfer ownership instead.")
    old_role = pm.role
    if old_role != new_role:
        pm.role = new_role
        session.flush()
        log_audit(user_id=actor.user_id, action="update", entity_type="project_member",
                  entity_id=pm.id, old_value={"role": old_role}, new_value={"role": new_role})
        session.commit()
    return {"entity": "project_member", "project_id": proposal.entity_id, "user_id": user_id,
            "new_role": pm.role}


def _target_status(proposal: WriteProposal) -> str:
    if proposal.kind == "false_positive":
        return IssueStatus.FALSE_POSITIVE
    if proposal.kind == "resolve_issue":
        return IssueStatus.RESOLVED
    # update_issue_status carries the target in the status change's new_value.
    for change in proposal.changes:
        if change.field_path == "status":
            return change.new_value
    raise ValidationError("Proposal has no target status")
