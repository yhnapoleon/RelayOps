"""Analytics schemas — product health, monthly/project/product analytics."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from api.schemas.issue_schemas import IssueResponse


class IssueTypeCount(BaseModel):
    """Issue count for a specific type."""

    type: str
    count: int


class AnalyticsBreakdownItem(BaseModel):
    """Named breakdown item used by analytics charts and drill-down selectors."""

    id: Optional[int] = None
    name: str
    count: int


class ProductHealthItemResponse(BaseModel):
    """Product-level health row for anomaly-first analytics views."""

    product_id: int
    product_name: str
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    job_runs_total: int = 0
    job_failures: int = 0
    failure_rate_percent: int = 0
    open_issue_count: int = 0
    max_repeat_failure_streak: int = 0
    anomaly_score: int = 0
    severity: str = "healthy"
    is_anomaly: bool = False
    anomaly_reasons: List[str] = []
    last_failed_at: Optional[datetime] = None
    last_issue_at: Optional[datetime] = None


class ProductHealthAnomalyRulesResponse(BaseModel):
    """Effective anomaly rules applied to product health analytics."""

    min_runs: int = 20
    min_failure_rate_percent: int = 40
    min_repeat_failure_streak: int = 5
    min_open_issues: int = 3
    recent_failure_hours: int = 24


class ProductHealthAnalyticsResponse(BaseModel):
    """Month-scoped product health dashboard payload."""

    period_granularity: str = "month"
    year: int
    month: int
    week_start: Optional[str] = None
    period_label: str
    period_start: datetime
    period_end: datetime
    total_products: int = 0
    anomaly_products: int = 0
    avg_failure_rate_percent: int = 0
    open_high_risk_issues: int = 0
    anomaly_rules: "ProductHealthAnomalyRulesResponse"
    items: List["ProductHealthItemResponse"] = []


class ProductHealthTrendPointResponse(BaseModel):
    """Daily trend point for product failure-rate drill-down."""

    date: str
    job_runs_total: int = 0
    job_failures: int = 0
    failure_rate_percent: int = 0


class ProductHealthJobItemResponse(BaseModel):
    """Job-level metrics under a product drill-down."""

    job_id: int
    job_name: str
    job_runs_total: int = 0
    job_failures: int = 0
    failure_rate_percent: int = 0
    open_issue_count: int = 0
    max_repeat_failure_streak: int = 0
    last_failed_at: Optional[datetime] = None


class ProductHealthDrilldownResponse(BaseModel):
    """Detailed product health payload with trend, jobs, and related issues."""

    period_granularity: str = "month"
    year: int
    month: int
    week_start: Optional[str] = None
    period_label: str
    period_start: datetime
    period_end: datetime
    anomaly_rules: "ProductHealthAnomalyRulesResponse"
    summary: "ProductHealthItemResponse"
    daily_trend: List["ProductHealthTrendPointResponse"] = []
    jobs: List["ProductHealthJobItemResponse"] = []
    issues: List["IssueResponse"] = []


class AnalyticsResponseBase(BaseModel):
    """Shared analytics payload for monthly/project/product views."""

    period_granularity: str = "month"
    year: int
    month: int
    week_start: Optional[str] = None
    period_label: str
    period_start: datetime
    period_end: datetime
    total: int
    by_type: List["IssueTypeCount"] = []
    by_product: List["AnalyticsBreakdownItem"] = []
    by_project: List["AnalyticsBreakdownItem"] = []
    manual_interventions: int = 0
    avg_resolution_minutes: int = 0
    max_resolution_minutes: int = 0
    sla_compliance_rate: int = 0
    sla_compliant_count: int = 0
    sla_total_count: int = 0
    open_count: int = 0
    in_progress_count: int = 0
    resolved_count: int = 0


class MonthlyAnalyticsResponse(AnalyticsResponseBase):
    """Monthly analytics response with absolute numbers."""


class ProductAnalyticsResponse(AnalyticsResponseBase):
    """Per-product analytics response with absolute numbers."""

    product_id: int
    product_name: str
    project_id: Optional[int] = None
    project_name: Optional[str] = None


class ProjectAnalyticsResponse(AnalyticsResponseBase):
    """Per-project analytics response with absolute numbers."""

    project_id: int
    project_name: str
