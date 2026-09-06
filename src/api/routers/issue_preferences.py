"""Current-user issue preference routes."""

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.schema import IssuePreferenceResponse, IssuePreferenceUpdateRequest
from core.exceptions import ValidationError
from core.models.entities import IssueType, UserIssuePreference

router = APIRouter(tags=["issue_preferences"])


@router.get("/api/me/issue-preferences", response_model=List[IssuePreferenceResponse])
def get_my_issue_preferences(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    prefs = (
        session.query(UserIssuePreference)
        .filter(UserIssuePreference.user_id == current_user.user_id)
        .order_by(UserIssuePreference.issue_type.asc())
        .all()
    )
    return [{"issue_type": pref.issue_type} for pref in prefs]


@router.put("/api/me/issue-preferences", response_model=List[IssuePreferenceResponse])
def update_my_issue_preferences(
    body: IssuePreferenceUpdateRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    invalid_types = sorted({t for t in body.issue_types if t not in IssueType.ALL})
    if invalid_types:
        raise ValidationError(f"Invalid issue type(s): {', '.join(invalid_types)}")

    normalized = sorted(set(body.issue_types))
    session.query(UserIssuePreference).filter(
        UserIssuePreference.user_id == current_user.user_id
    ).delete()
    for issue_type in normalized:
        session.add(UserIssuePreference(user_id=current_user.user_id, issue_type=issue_type))
    return [{"issue_type": t} for t in normalized]
