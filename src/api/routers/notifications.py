"""Notification routes: GET/PUT /api/notifications."""

from typing import List

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.schema import NotificationResponse
from core.exceptions import NotFoundError
from core.logging import get_logger
from core.models.entities import Issue, Notification, Product, Project

logger = get_logger(__name__)
router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _enrich_notification(session: Session, notification: Notification) -> dict:
    result = {
        "id": notification.id,
        "user_id": notification.user_id,
        "title": notification.title,
        "message": notification.message or "",
        "type": notification.type,
        "is_read": notification.is_read,
        "related_entity_type": notification.related_entity_type,
        "related_entity_id": notification.related_entity_id,
        "issue_id": None,
        "issue_type": None,
        "issue_title": None,
        "project_id": None,
        "project_name": None,
        "project_is_system": False,
        "product_id": None,
        "product_name": None,
        "product_is_system": False,
        "support_group_id": None,
        "support_group_name": None,
        "owner_group_id": None,
        "owner_group_name": None,
        "created_at": notification.created_at,
    }
    issue = None
    product = None
    if notification.related_entity_type == "issue" and notification.related_entity_id is not None:
        issue = session.query(Issue).filter(Issue.id == notification.related_entity_id).first()
        if issue:
            result["issue_id"] = issue.id
            result["issue_type"] = issue.type
            result["issue_title"] = issue.title
            result["support_group_id"] = issue.support_group_id
            result["support_group_name"] = issue.support_group_name or None
            result["owner_group_id"] = issue.owner_group_id
            result["owner_group_name"] = issue.owner_group_name or None
            if issue.product_id is not None:
                product = session.query(Product).filter(Product.id == issue.product_id).first()
    elif notification.related_entity_type == "product" and notification.related_entity_id is not None:
        product = session.query(Product).filter(Product.id == notification.related_entity_id).first()

    if product:
        result["product_id"] = product.id
        result["product_name"] = product.name
        result["product_is_system"] = bool(product.is_system)
        result["project_id"] = product.project_id
        project = session.query(Project).filter(Project.id == product.project_id).first()
        if project:
            result["project_name"] = project.name
            result["project_is_system"] = bool(project.is_system)
    return result


@router.get("", response_model=List[NotificationResponse])
def list_notifications(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    notifications = (
        session.query(Notification)
        .filter(Notification.user_id == current_user.user_id)
        .order_by(Notification.created_at.desc())
        .limit(50)
        .all()
    )
    return [_enrich_notification(session, n) for n in notifications]


@router.put("/{notification_id}/read", response_model=NotificationResponse)
def mark_read(
    notification_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    notif = (
        session.query(Notification)
        .filter(
            Notification.id == notification_id,
            Notification.user_id == current_user.user_id,
        )
        .first()
    )
    if notif is None:
        raise NotFoundError("Notification not found")
    notif.is_read = 1
    session.flush()
    session.refresh(notif)
    return _enrich_notification(session, notif)


@router.put("/read-all", status_code=status.HTTP_204_NO_CONTENT)
def mark_all_read(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    session.query(Notification).filter(
        Notification.user_id == current_user.user_id,
        Notification.is_read == 0,
    ).update({Notification.is_read: 1})
