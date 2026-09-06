"""Product CRUD routes — thin HTTP layer.

Business logic, access control, and audit logging live in
core/services/product_service.py and core/services/handover_service.py.
"""

from typing import List

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.deps.rbac import BusinessOwnerOrAdmin
from api.schema import (
    EmailTemplateApplyRequest,
    EmailTemplateApplyResponse,
    HandoverCompletenessResponse,
    ProductCreate,
    ProductResponse,
    ProductUpdate,
)
from core.logging import get_logger
from core.services import product_service

logger = get_logger(__name__)
router = APIRouter(tags=["products"])


# ── Nested under project ──────────────────────────────────────────────


@router.post(
    "/api/projects/{project_id}/products",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_product(
    project_id: int,
    body: ProductCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    product = product_service.create(
        session,
        project_id=project_id,
        name=body.name,
        actor=current_user,
    )
    return ProductResponse.model_validate(product)


@router.get("/api/projects/{project_id}/products", response_model=List[ProductResponse])
def list_products(
    project_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    products = product_service.list_for_project(session, project_id=project_id, actor=current_user)
    return [ProductResponse.model_validate(p) for p in products]


# ── Flat operations ───────────────────────────────────────────────────


@router.put("/api/products/{product_id}", response_model=ProductResponse)
def update_product(
    product_id: int,
    body: ProductUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    product = product_service.update(
        session,
        product_id=product_id,
        name=body.name,
        actor=current_user,
    )
    return ProductResponse.model_validate(product)


@router.post(
    "/api/products/{product_id}/copy",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
)
def copy_product(
    product_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    """Duplicate a product (and every job/app/scenario it owns) inside
    the same project. Name suffix bumps from "(copy)" on collision; all
    asset configuration (including CML bindings) is carried over."""
    product = product_service.copy(
        session,
        product_id=product_id,
        actor=current_user,
    )
    return ProductResponse.model_validate(product)


@router.delete("/api/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(
    product_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    product_service.delete(session, product_id=product_id, actor=current_user)


@router.get("/api/products/{product_id}", response_model=ProductResponse)
def get_product(
    product_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    product = product_service.get(session, product_id=product_id, actor=current_user)
    return ProductResponse.model_validate(product)


@router.post("/api/products/{product_id}/check-now")
def check_product_now(
    product_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Force a one-shot job/app/MMP check for this product (user-triggered)."""
    return product_service.check_now(session, product_id=product_id)


@router.post(
    "/api/products/{product_id}/email-template/apply",
    response_model=EmailTemplateApplyResponse,
)
def apply_email_template(
    product_id: int,
    body: EmailTemplateApplyRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    """Copy one send-email step's owner-email template onto many scenarios.

    ``mode='fill_empty'`` sets it only where no template exists yet ("set as
    default"); ``mode='overwrite'`` replaces every target ("replace all").
    ``scope`` is the whole product or a single job/app.
    """
    result = product_service.apply_email_template(
        session,
        product_id=product_id,
        template=body.template,
        mode=body.mode,
        scope=body.scope,
        job_id=body.job_id,
        application_id=body.application_id,
        actor=current_user,
    )
    return EmailTemplateApplyResponse.model_validate(result)


@router.get(
    "/api/products/{product_id}/handover-completeness",
    response_model=HandoverCompletenessResponse,
)
def get_handover_completeness(
    product_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    result = product_service.get_handover_completeness(
        session, product_id=product_id, actor=current_user
    )
    return HandoverCompletenessResponse.model_validate(result)
