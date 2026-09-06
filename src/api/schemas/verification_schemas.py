"""Verification-report schemas.

Surfaces ``VerificationReport`` (Phase-2 of the Verification tab) to
the API. The model stores per-check outcomes in a denormalised JSON
column; these schemas just shape the dict and split the list view
(no ``details``) from the detail view (full ``details``) so the list
endpoint stays cheap to render.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class VerificationReportGenerateRequest(BaseModel):
    """Optional caller-supplied metadata for a new report.

    The endpoint runs every active API check regardless of payload —
    ``notes`` is the only knob and is a free-text label the user can
    attach (e.g. ``"CML 2025-05-21 downtime sign-off"``).
    """

    notes: Optional[str] = Field(default=None, max_length=1024)


class VerificationReportEntry(BaseModel):
    """One row inside a report's ``details`` JSON."""

    project_id: Optional[int] = None
    project_name: str = ""
    product_id: Optional[int] = None
    product_name: str = ""
    application_id: Optional[int] = None
    check_id: int
    check_name: str = ""
    method: str = "GET"
    url: str = ""
    status: str = "fail"  # "pass" | "fail"
    status_code: Optional[int] = None
    duration_ms: int = 0
    error: Optional[str] = None
    assertion_reason: Optional[str] = None
    run_id: Optional[int] = None


class VerificationReportSummaryResponse(BaseModel):
    """List-view row — counts only, no ``details``.

    The Analytics 'Verification Report' section renders this directly;
    the Detail dialog fetches ``VerificationReportDetailResponse`` only
    when the user expands a row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    generated_at: datetime
    generated_by: Optional[int] = None
    generated_by_name: Optional[str] = None
    total_count: int = 0
    pass_count: int = 0
    fail_count: int = 0
    duration_ms: int = 0
    notes: Optional[str] = ""


class VerificationReportDetailResponse(VerificationReportSummaryResponse):
    """Detail view — same fields as the summary plus the full breakdown."""

    details: List[VerificationReportEntry] = Field(default_factory=list)
