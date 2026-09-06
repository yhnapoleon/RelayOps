"""Duty morning report schemas — responses."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class DutyReportResponse(BaseModel):
    """One generated duty morning report."""

    id: int
    report_date: str
    status: str
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    sections: Optional[Dict[str, Any]] = None
    llm_summary: Optional[Dict[str, Any]] = None
    llm_status: str = "skipped"
    email_status: str = "pending"
    recipient_user_ids: Optional[List[int]] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DutyReportMeta(BaseModel):
    """History-list entry — everything except the heavy sections JSON."""

    id: int
    report_date: str
    status: str
    llm_status: str = "skipped"
    email_status: str = "pending"
    created_at: datetime

    model_config = {"from_attributes": True}


class DutyReportTodayResponse(BaseModel):
    """``/today`` payload: the report (when ready) plus whether the
    calling user should see the morning popup."""

    report: Optional[DutyReportResponse] = None
    show_popup: bool = False
