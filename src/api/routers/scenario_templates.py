"""Scenario-template routes — a workspace-wide library of reusable Job/App
scenario drafts (including the owner-email template).

CRUD is simple enough to live in the router: the ``payload`` blob is opaque
to the server (the frontend scenario form owns its shape), so there's no
domain logic worth a dedicated service. Read is open to any authenticated
user; create/update/delete require the same role gate as scenario CRUD.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.deps.rbac import BusinessOwnerOrAdmin
from api.schema import (
    ScenarioTemplateCreate,
    ScenarioTemplateResponse,
    ScenarioTemplateUpdate,
)
from core.exceptions import NotFoundError
from core.models.entities import ScenarioTemplate

router = APIRouter(tags=["scenario-templates"])


@router.get("/api/scenario-templates", response_model=List[ScenarioTemplateResponse])
def list_scenario_templates(
    asset_kind: Optional[str] = Query(default=None, pattern="^(job|app)$"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List saved scenario templates, optionally filtered by asset kind.

    Most-recently-updated first so freshly saved templates surface at the
    top of the picker.
    """
    q = session.query(ScenarioTemplate).filter(ScenarioTemplate.is_active.is_(True))
    if asset_kind:
        q = q.filter(ScenarioTemplate.asset_kind == asset_kind)
    rows = q.order_by(
        ScenarioTemplate.updated_at.desc(), ScenarioTemplate.id.desc()
    ).all()
    return [ScenarioTemplateResponse.model_validate(r) for r in rows]


@router.post(
    "/api/scenario-templates",
    response_model=ScenarioTemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_scenario_template(
    body: ScenarioTemplateCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    template = ScenarioTemplate(
        asset_kind=body.asset_kind,
        name=body.name.strip(),
        scenario_type=body.scenario_type or "",
        description=body.description or "",
        payload=body.payload or {},
        created_by=current_user.user_id,
    )
    session.add(template)
    session.flush()
    return ScenarioTemplateResponse.model_validate(template)


@router.put(
    "/api/scenario-templates/{template_id}",
    response_model=ScenarioTemplateResponse,
)
def update_scenario_template(
    template_id: int,
    body: ScenarioTemplateUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    template = (
        session.query(ScenarioTemplate)
        .filter(ScenarioTemplate.id == template_id)
        .first()
    )
    if template is None:
        raise NotFoundError("Scenario template not found")

    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        template.name = data["name"].strip()
    if "scenario_type" in data and data["scenario_type"] is not None:
        template.scenario_type = data["scenario_type"]
    if "description" in data and data["description"] is not None:
        template.description = data["description"]
    if "payload" in data and data["payload"] is not None:
        template.payload = data["payload"]
    if "is_active" in data and data["is_active"] is not None:
        template.is_active = data["is_active"]
    session.flush()
    return ScenarioTemplateResponse.model_validate(template)


@router.delete(
    "/api/scenario-templates/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_scenario_template(
    template_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    template = (
        session.query(ScenarioTemplate)
        .filter(ScenarioTemplate.id == template_id)
        .first()
    )
    if template is None:
        raise NotFoundError("Scenario template not found")
    session.delete(template)
