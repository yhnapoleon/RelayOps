"""Schedule (duty assignment) routes."""

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.deps.rbac import OpsMemberOrAdmin
from api.schema import ScheduleCreate, ScheduleResponse, ScheduleUpdate
from core.exceptions import ConflictError, NotFoundError, ValidationError
from core.logging import get_logger
from core.models.entities import Notification, Schedule
from core.models.user import User
from core.services.audit_service import log_audit, serialize_schedule
from core.services.user_service import get_user_by_id

logger = get_logger(__name__)
router = APIRouter(prefix="/api/schedules", tags=["schedules"])


def _enrich_schedule(session: Session, schedule: Schedule) -> dict:
    from core.models.database import get_db

    assignee = get_user_by_id(get_db(), schedule.assignee_id)
    return {
        "id": schedule.id,
        "start_time": schedule.start_time,
        "end_time": schedule.end_time,
        "assignee_id": schedule.assignee_id,
        "assignee_username": assignee.username if assignee else None,
        "assignee_display_name": assignee.display_name if assignee else None,
        "created_by": schedule.created_by,
        "duty_role": schedule.duty_role,
        "note": schedule.note or "",
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
    }


def _add_duty_notification(session: Session, *, user_id: int, start_time: datetime, end_time: datetime) -> None:
    notif = Notification(
        user_id=user_id,
        title=f"On-Duty Assignment: {start_time.strftime('%Y-%m-%d %H:%M')}",
        message=(
            f"You have been assigned as the on-duty Ops support from "
            f"{start_time.strftime('%Y-%m-%d %H:%M')} to {end_time.strftime('%Y-%m-%d %H:%M')}."
        ),
        type="duty_assigned",
        related_entity_type="schedule",
    )
    session.add(notif)


def _apply_backdated_reassign(session: Session, schedule: Schedule, *, actor_user_id: int) -> None:
    """Hand the scheduled member any operational Issues already raised inside
    their (possibly back-dated) shift window.

    A no-op for purely-future shifts — the window ``[start, min(now, end)]`` is
    empty then, so nothing is reassigned. Failures are logged but never bubble
    up: the schedule write is the primary action and must still succeed even if
    the retroactive reassignment hits a snag.
    """
    from core.issue_management.issue_engine import reassign_open_ops_issues_in_window

    try:
        reassigned = reassign_open_ops_issues_in_window(
            session,
            assignee_id=schedule.assignee_id,
            start_time=schedule.start_time,
            end_time=schedule.end_time,
            actor_user_id=actor_user_id,
        )
        if reassigned:
            logger.info(
                "Schedule {} back-date reassigned {} issue(s) to assignee_id={}",
                schedule.id, len(reassigned), schedule.assignee_id,
            )
    except Exception:
        logger.opt(exception=True).error(
            "Back-dated reassign failed for schedule {}", schedule.id
        )


@router.get("/eligible-users")
def list_eligible_users(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(OpsMemberOrAdmin),
):
    users = (
        session.query(User)
        .filter(User.role.in_(["admin", "relayops_member"]))
        .order_by(User.username)
        .all()
    )
    return [
        {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role}
        for u in users
    ]


@router.get("/my", response_model=List[ScheduleResponse])
def list_my_schedules(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    schedules = (
        session.query(Schedule)
        .filter(Schedule.assignee_id == current_user.user_id)
        .order_by(Schedule.start_time.desc())
        .all()
    )
    return [_enrich_schedule(session, s) for s in schedules]


@router.post("", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
def create_schedule(
    body: ScheduleCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(OpsMemberOrAdmin),
):
    if body.end_time <= body.start_time:
        raise ValidationError("end_time must be after start_time")

    from core.models.database import get_db
    assignee = get_user_by_id(get_db(), body.assignee_id)
    if not assignee:
        raise NotFoundError(f"User with id={body.assignee_id} not found")
    if assignee.role not in ("admin", "relayops_member"):
        raise ValidationError(
            f"User '{assignee.username}' is a {assignee.role}. Only admin or relayops_member can be assigned on-duty."
        )

    duplicate = (
        session.query(Schedule)
        .filter(
            Schedule.assignee_id == body.assignee_id,
            Schedule.start_time < body.end_time,
            Schedule.end_time > body.start_time,
        )
        .first()
    )
    if duplicate:
        raise ConflictError(
            f"User already has an overlapping duty from {duplicate.start_time} to {duplicate.end_time}."
        )

    if body.duty_role == "Primary":
        primary_conflict = (
            session.query(Schedule)
            .filter(
                Schedule.duty_role == "Primary",
                Schedule.start_time < body.end_time,
                Schedule.end_time > body.start_time,
            )
            .first()
        )
        if primary_conflict:
            raise ConflictError(
                f"A Primary duty already exists in this time range (assigned to user_id={primary_conflict.assignee_id}, "
                f"{primary_conflict.start_time} ~ {primary_conflict.end_time}). Use Secondary or Shadow instead."
            )

    schedule = Schedule(
        start_time=body.start_time,
        end_time=body.end_time,
        assignee_id=body.assignee_id,
        created_by=current_user.user_id,
        duty_role=body.duty_role,
        note=body.note or "",
    )
    session.add(schedule)
    session.flush()
    session.refresh(schedule)

    logger.info(
        "Schedule created: start={} end={} assignee_id={} by user_id={}",
        body.start_time, body.end_time, body.assignee_id, current_user.user_id,
    )
    log_audit(
        user_id=current_user.user_id,
        action="create",
        entity_type="schedule",
        entity_id=schedule.id,
        new_value=serialize_schedule(schedule),
    )
    _add_duty_notification(
        session,
        user_id=body.assignee_id,
        start_time=body.start_time,
        end_time=body.end_time,
    )
    _apply_backdated_reassign(session, schedule, actor_user_id=current_user.user_id)
    return _enrich_schedule(session, schedule)


@router.get("", response_model=List[ScheduleResponse])
def list_schedules(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(OpsMemberOrAdmin),
):
    schedules = session.query(Schedule).order_by(Schedule.start_time.desc()).all()
    return [_enrich_schedule(session, s) for s in schedules]


@router.get("/current", response_model=List[ScheduleResponse])
def get_current_schedules(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    now = datetime.utcnow()
    schedules = (
        session.query(Schedule)
        .filter(Schedule.start_time <= now, Schedule.end_time >= now)
        .all()
    )
    return [_enrich_schedule(session, s) for s in schedules]


@router.get("/{schedule_id}", response_model=ScheduleResponse)
def get_schedule(
    schedule_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(OpsMemberOrAdmin),
):
    schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise NotFoundError(f"Schedule {schedule_id} not found")
    return _enrich_schedule(session, schedule)


@router.put("/{schedule_id}", response_model=ScheduleResponse)
def update_schedule(
    schedule_id: int,
    body: ScheduleUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(OpsMemberOrAdmin),
):
    schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise NotFoundError(f"Schedule {schedule_id} not found")

    old_val = serialize_schedule(schedule)
    if body.start_time is not None:
        schedule.start_time = body.start_time
    if body.end_time is not None:
        schedule.end_time = body.end_time
    if schedule.end_time <= schedule.start_time:
        raise ValidationError("end_time must be after start_time")

    if body.assignee_id is not None:
        from core.models.database import get_db
        assignee = get_user_by_id(get_db(), body.assignee_id)
        if not assignee:
            raise NotFoundError(f"User with id={body.assignee_id} not found")
        if assignee.role not in ("admin", "relayops_member"):
            raise ValidationError(
                f"User '{assignee.username}' is a {assignee.role}. Only admin or relayops_member can be assigned on-duty."
            )
        schedule.assignee_id = body.assignee_id

    if body.duty_role is not None:
        schedule.duty_role = body.duty_role

    if schedule.duty_role == "Primary":
        primary_conflict = (
            session.query(Schedule)
            .filter(
                Schedule.id != schedule_id,
                Schedule.duty_role == "Primary",
                Schedule.start_time < schedule.end_time,
                Schedule.end_time > schedule.start_time,
            )
            .first()
        )
        if primary_conflict:
            raise ConflictError(
                f"A Primary duty already exists in this time range (user_id={primary_conflict.assignee_id})."
            )

    assignee_conflict = (
        session.query(Schedule)
        .filter(
            Schedule.id != schedule_id,
            Schedule.assignee_id == schedule.assignee_id,
            Schedule.start_time < schedule.end_time,
            Schedule.end_time > schedule.start_time,
        )
        .first()
    )
    if assignee_conflict:
        raise ConflictError(
            f"This user already has an overlapping duty ({assignee_conflict.start_time} ~ {assignee_conflict.end_time})."
        )

    if body.note is not None:
        schedule.note = body.note
    schedule.updated_at = datetime.utcnow()
    session.flush()
    session.refresh(schedule)

    logger.info("Schedule {} updated by user_id={}", schedule_id, current_user.user_id)
    log_audit(
        user_id=current_user.user_id,
        action="update",
        entity_type="schedule",
        entity_id=schedule.id,
        old_value=old_val,
        new_value=serialize_schedule(schedule),
    )
    if body.assignee_id is not None and body.assignee_id != old_val.get("assignee_id"):
        _add_duty_notification(
            session,
            user_id=body.assignee_id,
            start_time=schedule.start_time,
            end_time=schedule.end_time,
        )
    _apply_backdated_reassign(session, schedule, actor_user_id=current_user.user_id)
    return _enrich_schedule(session, schedule)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(
    schedule_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(OpsMemberOrAdmin),
):
    schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise NotFoundError(f"Schedule {schedule_id} not found")
    old_val = serialize_schedule(schedule)
    session.delete(schedule)
    logger.info("Schedule {} deleted by user_id={}", schedule_id, current_user.user_id)
    log_audit(
        user_id=current_user.user_id,
        action="delete",
        entity_type="schedule",
        entity_id=schedule_id,
        old_value=old_val,
    )
