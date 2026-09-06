"""Analytics and export routes — RBAC scoping here, computation in
``core.services.analytics_service`` (shared with the agent tool layer)."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.query_filters import parse_filter_values
from api.schema import (
    ExportPayloadResponse,
    IssueResponse,
    MonthlyAnalyticsResponse,
    ProductHealthAnomalyRulesResponse,
    ProductHealthDrilldownResponse,
    ProductHealthAnalyticsResponse,
    ProductHealthJobItemResponse,
    ProductHealthItemResponse,
    ProductHealthTrendPointResponse,
    ProductAnalyticsResponse,
    ProjectAnalyticsResponse,
)
from core.services.audit_service import (
    serialize_app,
    serialize_application_recovery_scenario,
    serialize_job,
    serialize_job_failure_scenario,
    serialize_product,
    serialize_product_version,
    serialize_project,
)
from core.config import get_config
from core.exceptions import ValidationError as CoreValidationError
from core.models.database import Database, get_db
from core.models.user import UserRole, is_elevated_role
from core.services import analytics_service
from core.services.user_service import get_user_by_id
from core.models.entities import (
    Application,
    ApplicationRecoveryScenario,
    Issue,
    IssueStatus,
    Job,
    JobFailureScenario,
    Product,
    ProductVersion,
    Project,
    ProjectMember,
)
from core.logging import get_logger
from core.services.support_group_service import user_has_project_group_access

logger = get_logger(__name__)

router = APIRouter(tags=["analytics"])


def _is_platform_admin(username: str) -> bool:
    config = get_config()
    return username in config.platform_owners


def _is_admin_user(current_user: CurrentUser) -> bool:
    # admin or relayops_member (platform elevation, see-all) OR platform owner.
    return is_elevated_role(current_user.role) or _is_platform_admin(current_user.username)


def _get_accessible_project_ids(session, current_user: CurrentUser) -> list[int]:
    """Return project ids visible to a business owner via ownership or membership."""
    owned_ids = [
        row.id
        for row in session.query(Project.id)
        .filter(Project.owner_id == current_user.user_id)
        .all()
    ]
    member_ids = [
        row.project_id
        for row in session.query(ProjectMember.project_id)
        .filter(ProjectMember.user_id == current_user.user_id)
        .all()
    ]
    group_ids = [
        project.id
        for project in session.query(Project).all()
        if user_has_project_group_access(session, project, current_user.ad_groups)
    ]
    return sorted(set(owned_ids + member_ids + group_ids))


def _get_accessible_product_ids(session, current_user: CurrentUser) -> list[int]:
    project_ids = _get_accessible_project_ids(session, current_user)
    if not project_ids:
        return []
    return [
        row.id
        for row in session.query(Product.id)
        .filter(Product.project_id.in_(project_ids))
        .all()
    ]


def _can_access_project(session, project: Project, current_user: CurrentUser) -> bool:
    """Check whether the user can inspect project-scoped analytics/export data."""
    if is_elevated_role(current_user.role):  # admin or relayops_member: see-all
        return True
    if current_user.role == UserRole.REGULAR_USER:
        return project.id in _get_accessible_project_ids(session, current_user)
    return False


def _can_access_product(session, product: Product, current_user: CurrentUser) -> bool:
    """Check resource-level access for a product analytics/export query."""
    if is_elevated_role(current_user.role):  # admin or relayops_member: see-all
        return True

    project = session.query(Project).filter(Project.id == product.project_id).first()
    if project is None:
        return False

    if current_user.role == UserRole.REGULAR_USER:
        return project.id in _get_accessible_project_ids(session, current_user)

    return False


def _resolve_period(
    *,
    year: Optional[int],
    month: Optional[int],
    week_start: Optional[str],
) -> tuple[str, datetime, datetime, int, int, Optional[str], str]:
    """Thin HTTP adapter over analytics_service.resolve_period."""
    try:
        p = analytics_service.resolve_period(year=year, month=month, week_start=week_start)
    except CoreValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return p.granularity, p.start, p.end, p.year, p.month, p.week_start, p.label


def _get_scoped_products(session, current_user: CurrentUser, is_admin: bool) -> list[Product]:
    query = session.query(Product)
    if is_admin:
        return query.order_by(Product.name.asc(), Product.id.asc()).all()

    if current_user.role == UserRole.REGULAR_USER:
        project_ids = _get_accessible_project_ids(session, current_user)
        if not project_ids:
            return []
        return query.filter(Product.project_id.in_(project_ids)).order_by(Product.name.asc(), Product.id.asc()).all()

    if current_user.role == UserRole.RELAYOPS_MEMBER:
        group_project_ids = {
            project.id
            for project in session.query(Project).all()
            if user_has_project_group_access(session, project, current_user.ad_groups)
        }
        group_product_ids = {
            row.id
            for row in session.query(Product.id)
            .filter(Product.project_id.in_(sorted(group_project_ids)))
            .all()
        } if group_project_ids else set()
        assigned_product_ids = {
            row.product_id
            for row in session.query(Issue.product_id)
            .filter(
                Issue.assignee_id == current_user.user_id,
                Issue.product_id.isnot(None),
            )
            .all()
            if row.product_id is not None
        }
        visible_product_ids = sorted(group_product_ids | assigned_product_ids)
        if not visible_product_ids:
            return []
        return query.filter(Product.id.in_(visible_product_ids)).order_by(Product.name.asc(), Product.id.asc()).all()

    return []


# Failure predicate & streaks now live in analytics_service; the aliases keep
# the historical import surface (tests import the underscore names from here).
_is_failed_execution_status = analytics_service.is_failed_execution_status
_execution_is_failure = analytics_service.execution_is_failure
_max_failed_streak = analytics_service.max_failed_streak
_get_anomaly_rules = analytics_service.get_anomaly_rules


def _serialize_anomaly_rules(rules: analytics_service.AnomalyRules) -> ProductHealthAnomalyRulesResponse:
    return ProductHealthAnomalyRulesResponse(**rules.as_dict())


def _apply_issue_scope(query, session, current_user: CurrentUser, is_admin: bool):
    if is_admin:
        return query
    if current_user.role == UserRole.REGULAR_USER:
        accessible_product_ids = _get_accessible_product_ids(session, current_user)
        if not accessible_product_ids:
            return query.filter(Issue.id == -1)
        return query.filter(Issue.product_id.in_(accessible_product_ids))
    if current_user.role == UserRole.RELAYOPS_MEMBER:
        return query.filter(Issue.assignee_id == current_user.user_id)
    return query.filter(Issue.id == -1)


def _query_scoped_issues(
    session,
    current_user: CurrentUser,
    is_admin: bool,
    *,
    period_start: datetime,
    period_end: datetime,
    project_id: Optional[int] = None,
    product_id: Optional[int] = None,
    issue_type: Optional[str] = None,
    issue_status: Optional[str] = None,
):
    query = session.query(Issue).filter(
        Issue.created_at >= period_start,
        Issue.created_at < period_end,
    )
    query = _apply_issue_scope(query, session, current_user, is_admin)

    if product_id is not None:
        query = query.filter(Issue.product_id == product_id)
    if project_id is not None:
        project_product_ids = [
            row.id
            for row in session.query(Product.id)
            .filter(Product.project_id == project_id)
            .all()
        ]
        if not project_product_ids:
            return []
        query = query.filter(Issue.product_id.in_(project_product_ids))
    # Both arrive comma-separated from the UI's multi-select filters; a bare
    # single value still parses to a one-element list.
    type_values = parse_filter_values(issue_type)
    status_values = parse_filter_values(issue_status)
    if type_values:
        query = query.filter(Issue.type.in_(type_values))
    if status_values:
        query = query.filter(Issue.status.in_(status_values))

    return query.order_by(Issue.updated_at.desc(), Issue.created_at.desc()).all()


# Issue statistics moved to analytics_service (single home for the math).
_compute_issue_stats = analytics_service.compute_issue_stats


def _serialize_project_with_owner(project: Project) -> dict:
    payload = serialize_project(project)
    owner = get_user_by_id(get_db(), project.owner_id)
    payload["owner_username"] = owner.username if owner else None
    payload["owner_display_name"] = owner.display_name if owner else None
    return payload


def _serialize_product_bundle(session, product: Product) -> dict:
    from api.routers.issues import _enrich_issue

    approved_version = None
    draft_version = None
    if product.current_approved_version_id is not None:
        approved_version = (
            session.query(ProductVersion)
            .filter(ProductVersion.id == product.current_approved_version_id)
            .first()
        )
    if product.current_draft_version_id is not None:
        draft_version = (
            session.query(ProductVersion)
            .filter(ProductVersion.id == product.current_draft_version_id)
            .first()
        )

    jobs = session.query(Job).filter(Job.product_id == product.id).order_by(Job.id.asc()).all()
    applications = (
        session.query(Application)
        .filter(Application.product_id == product.id)
        .order_by(Application.id.asc())
        .all()
    )
    issues = (
        session.query(Issue)
        .filter(Issue.product_id == product.id)
        .order_by(Issue.updated_at.desc(), Issue.created_at.desc())
        .all()
    )

    return {
        "product": serialize_product(product),
        "current_approved_version": serialize_product_version(approved_version) if approved_version else None,
        "current_draft_version": serialize_product_version(draft_version) if draft_version else None,
        "jobs": [
            {
                **serialize_job(job),
                "failure_scenarios": [
                    serialize_job_failure_scenario(scenario)
                    for scenario in session.query(JobFailureScenario)
                    .filter(JobFailureScenario.job_id == job.id)
                    .order_by(JobFailureScenario.created_at.asc(), JobFailureScenario.id.asc())
                    .all()
                ],
            }
            for job in jobs
        ],
        "applications": [
            {
                **serialize_app(app),
                "recovery_scenarios": [
                    serialize_application_recovery_scenario(scenario)
                    for scenario in session.query(ApplicationRecoveryScenario)
                    .filter(ApplicationRecoveryScenario.application_id == app.id)
                    .order_by(ApplicationRecoveryScenario.created_at.asc(), ApplicationRecoveryScenario.id.asc())
                    .all()
                ],
            }
            for app in applications
        ],
        "issues": [_enrich_issue(session, issue) for issue in issues],
    }


@router.get("/api/analytics/products/health", response_model=ProductHealthAnalyticsResponse)
def get_products_health_analytics(
    year: Optional[int] = Query(None, description="Year (defaults to current year)"),
    month: Optional[int] = Query(None, ge=1, le=12, description="Month (defaults to current month)"),
    week_start: Optional[str] = Query(None, description="ISO date in target week (YYYY-MM-DD). Enables week view"),
    project_id: Optional[int] = Query(None, description="Optional project filter"),
    min_runs: Optional[int] = Query(None, ge=1, description="Override anomaly rule: minimum runs"),
    min_failure_rate_percent: Optional[int] = Query(None, ge=1, le=100, description="Override anomaly rule: minimum failure rate percent"),
    min_repeat_failure_streak: Optional[int] = Query(None, ge=1, description="Override anomaly rule: minimum repeat failure streak"),
    min_open_issues: Optional[int] = Query(None, ge=1, description="Override anomaly rule: minimum open issues"),
    recent_failure_hours: Optional[int] = Query(None, ge=1, description="Override anomaly rule: recent failure window (hours)"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get anomaly-first product health analytics for the selected month."""
    is_admin = _is_admin_user(current_user)
    period_granularity, period_start, period_end, target_year, target_month, target_week_start, period_label = _resolve_period(
        year=year,
        month=month,
        week_start=week_start,
    )
    anomaly_rules = _get_anomaly_rules(
        min_runs=min_runs,
        min_failure_rate_percent=min_failure_rate_percent,
        min_repeat_failure_streak=min_repeat_failure_streak,
        min_open_issues=min_open_issues,
        recent_failure_hours=recent_failure_hours,
    )

    def _query():
        try:
            products = _get_scoped_products(session, current_user, is_admin)
            if project_id is not None:
                products = [product for product in products if product.project_id == project_id]
            if not products:
                return ProductHealthAnalyticsResponse(
                    period_granularity=period_granularity,
                    year=target_year,
                    month=target_month,
                    week_start=target_week_start,
                    period_label=period_label,
                    period_start=period_start,
                    period_end=period_end,
                    total_products=0,
                    anomaly_products=0,
                    avg_failure_rate_percent=0,
                    open_high_risk_issues=0,
                    anomaly_rules=_serialize_anomaly_rules(anomaly_rules),
                    items=[],
                )

            product_ids = [product.id for product in products]
            open_issue_rows = _apply_issue_scope(
                session.query(Issue).filter(
                    Issue.product_id.in_(product_ids),
                    Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS]),
                ),
                session,
                current_user,
                is_admin,
            ).all()

            sorted_items = [
                ProductHealthItemResponse(**item)
                for item in analytics_service.product_health_items(
                    session,
                    products,
                    period_start=period_start,
                    period_end=period_end,
                    rules=anomaly_rules,
                    open_issues=open_issue_rows,
                )
            ]
            anomaly_products = sum(1 for item in sorted_items if item.is_anomaly)
            avg_failure_rate = round(
                sum(item.failure_rate_percent for item in sorted_items) / len(sorted_items)
            ) if sorted_items else 0
            open_high_risk_issues = sum(
                item.open_issue_count
                for item in sorted_items
                if item.severity in ("risk", "anomaly")
            )

            return ProductHealthAnalyticsResponse(
                period_granularity=period_granularity,
                year=target_year,
                month=target_month,
                week_start=target_week_start,
                period_label=period_label,
                period_start=period_start,
                period_end=period_end,
                total_products=len(sorted_items),
                anomaly_products=anomaly_products,
                avg_failure_rate_percent=avg_failure_rate,
                open_high_risk_issues=open_high_risk_issues,
                anomaly_rules=_serialize_anomaly_rules(anomaly_rules),
                items=sorted_items,
            )
        finally:
            pass

    return _query()


@router.get("/api/analytics/products/{product_id}/drilldown", response_model=ProductHealthDrilldownResponse)
def get_product_health_drilldown(
    product_id: int,
    year: Optional[int] = Query(None, description="Year (defaults to current year)"),
    month: Optional[int] = Query(None, ge=1, le=12, description="Month (defaults to current month)"),
    week_start: Optional[str] = Query(None, description="ISO date in target week (YYYY-MM-DD). Enables week view"),
    min_runs: Optional[int] = Query(None, ge=1, description="Override anomaly rule: minimum runs"),
    min_failure_rate_percent: Optional[int] = Query(None, ge=1, le=100, description="Override anomaly rule: minimum failure rate percent"),
    min_repeat_failure_streak: Optional[int] = Query(None, ge=1, description="Override anomaly rule: minimum repeat failure streak"),
    min_open_issues: Optional[int] = Query(None, ge=1, description="Override anomaly rule: minimum open issues"),
    recent_failure_hours: Optional[int] = Query(None, ge=1, description="Override anomaly rule: recent failure window (hours)"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get detailed month-scoped product health with trend, jobs, and issue list."""
    is_admin = _is_admin_user(current_user)
    period_granularity, period_start, period_end, target_year, target_month, target_week_start, period_label = _resolve_period(
        year=year,
        month=month,
        week_start=week_start,
    )
    anomaly_rules = _get_anomaly_rules(
        min_runs=min_runs,
        min_failure_rate_percent=min_failure_rate_percent,
        min_repeat_failure_streak=min_repeat_failure_streak,
        min_open_issues=min_open_issues,
        recent_failure_hours=recent_failure_hours,
    )

    def _query():
        from api.routers.issues import _enrich_issue

        try:
            product = session.query(Product).filter(Product.id == product_id).first()
            if product is None:
                return None, "not_found"
            if not _can_access_product(session, product, current_user):
                return None, "forbidden"

            open_issues = _apply_issue_scope(
                session.query(Issue).filter(
                    Issue.product_id == product_id,
                    Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS]),
                ),
                session,
                current_user,
                is_admin,
            ).all()

            drilldown = analytics_service.product_health_drilldown_data(
                session,
                product,
                period_start=period_start,
                period_end=period_end,
                rules=anomaly_rules,
                open_issues=open_issues,
            )
            summary = ProductHealthItemResponse(**drilldown["summary"])
            sorted_job_items = [ProductHealthJobItemResponse(**j) for j in drilldown["jobs"]]
            daily_trend = [ProductHealthTrendPointResponse(**p) for p in drilldown["daily_trend"]]

            issues = _query_scoped_issues(
                session,
                current_user,
                is_admin,
                period_start=period_start,
                period_end=period_end,
                product_id=product_id,
            )
            issue_payload = [_enrich_issue(session, issue) for issue in issues[:200]]

            return ProductHealthDrilldownResponse(
                period_granularity=period_granularity,
                year=target_year,
                month=target_month,
                week_start=target_week_start,
                period_label=period_label,
                period_start=period_start,
                period_end=period_end,
                anomaly_rules=_serialize_anomaly_rules(anomaly_rules),
                summary=summary,
                daily_trend=daily_trend,
                jobs=sorted_job_items[:20],
                issues=issue_payload,
            ), "ok"
        finally:
            pass

    result, err = _query()
    if err == "not_found":
        raise HTTPException(status_code=404, detail="Product not found")
    if err == "forbidden":
        raise HTTPException(status_code=403, detail="Not authorized to view this product")
    return result


@router.get("/api/analytics/products/{product_id}/drilldown/export", response_model=ExportPayloadResponse)
def export_product_health_drilldown(
    product_id: int,
    year: Optional[int] = Query(None, description="Year (defaults to current year)"),
    month: Optional[int] = Query(None, ge=1, le=12, description="Month (defaults to current month)"),
    week_start: Optional[str] = Query(None, description="ISO date in target week (YYYY-MM-DD). Enables week view"),
    min_runs: Optional[int] = Query(None, ge=1, description="Override anomaly rule: minimum runs"),
    min_failure_rate_percent: Optional[int] = Query(None, ge=1, le=100, description="Override anomaly rule: minimum failure rate percent"),
    min_repeat_failure_streak: Optional[int] = Query(None, ge=1, description="Override anomaly rule: minimum repeat failure streak"),
    min_open_issues: Optional[int] = Query(None, ge=1, description="Override anomaly rule: minimum open issues"),
    recent_failure_hours: Optional[int] = Query(None, ge=1, description="Override anomaly rule: recent failure window (hours)"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Export month-scoped product health drill-down payload."""
    drilldown = get_product_health_drilldown(
        product_id=product_id,
        year=year,
        month=month,
        week_start=week_start,
        min_runs=min_runs,
        min_failure_rate_percent=min_failure_rate_percent,
        min_repeat_failure_streak=min_repeat_failure_streak,
        min_open_issues=min_open_issues,
        recent_failure_hours=recent_failure_hours,
        session=session,
        current_user=current_user,
    )
    return ExportPayloadResponse(
        export_type="product_health_drilldown",
        generated_at=datetime.utcnow(),
        filters={
            "product_id": product_id,
            "year": drilldown.year,
            "month": drilldown.month,
            "week_start": drilldown.week_start,
            "period_granularity": drilldown.period_granularity,
            "anomaly_rules": drilldown.anomaly_rules.model_dump(),
        },
        data=drilldown.model_dump(mode="json"),
    )


@router.get("/api/analytics/monthly", response_model=MonthlyAnalyticsResponse)
def get_monthly_analytics(
    year: Optional[int] = Query(None, description="Year (defaults to current year)"),
    month: Optional[int] = Query(None, ge=1, le=12, description="Month (defaults to current month)"),
    week_start: Optional[str] = Query(None, description="ISO date in target week (YYYY-MM-DD). Enables week view"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get month-scoped issue statistics across all visible products."""
    is_admin = _is_admin_user(current_user)
    period_granularity, period_start, period_end, target_year, target_month, target_week_start, period_label = _resolve_period(
        year=year,
        month=month,
        week_start=week_start,
    )

    def _query():
        try:
            issues = _query_scoped_issues(
                session,
                current_user,
                is_admin,
                period_start=period_start,
                period_end=period_end,
            )
            stats = _compute_issue_stats(session, issues)
            return MonthlyAnalyticsResponse(
                period_granularity=period_granularity,
                year=target_year,
                month=target_month,
                week_start=target_week_start,
                period_label=period_label,
                period_start=period_start,
                period_end=period_end,
                **stats,
            )
        finally:
            pass

    return _query()


@router.get("/api/analytics/product/{product_id}", response_model=ProductAnalyticsResponse)
def get_product_analytics(
    product_id: int,
    year: Optional[int] = Query(None, description="Year (defaults to current year)"),
    month: Optional[int] = Query(None, ge=1, le=12, description="Month (defaults to current month)"),
    week_start: Optional[str] = Query(None, description="ISO date in target week (YYYY-MM-DD). Enables week view"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get month-scoped statistics for a specific product."""
    period_granularity, period_start, period_end, target_year, target_month, target_week_start, period_label = _resolve_period(
        year=year,
        month=month,
        week_start=week_start,
    )

    def _query():
        try:
            product = session.query(Product).filter(Product.id == product_id).first()
            if product is None:
                return None, "not_found"
            if not _can_access_product(session, product, current_user):
                return None, "forbidden"

            issues = _query_scoped_issues(
                session,
                current_user,
                _is_admin_user(current_user),
                period_start=period_start,
                period_end=period_end,
                product_id=product_id,
            )
            stats = _compute_issue_stats(session, issues)
            project = session.query(Project).filter(Project.id == product.project_id).first()
            return ProductAnalyticsResponse(
                period_granularity=period_granularity,
                product_id=product_id,
                product_name=product.name,
                project_id=project.id if project else None,
                project_name=project.name if project else None,
                year=target_year,
                month=target_month,
                week_start=target_week_start,
                period_label=period_label,
                period_start=period_start,
                period_end=period_end,
                **stats,
            ), "ok"
        finally:
            pass

    result, err = _query()
    if err == "not_found":
        raise HTTPException(status_code=404, detail="Product not found")
    if err == "forbidden":
        raise HTTPException(status_code=403, detail="Not authorized to view analytics for this product")
    return result


@router.get("/api/analytics/project/{project_id}", response_model=ProjectAnalyticsResponse)
def get_project_analytics(
    project_id: int,
    year: Optional[int] = Query(None, description="Year (defaults to current year)"),
    month: Optional[int] = Query(None, ge=1, le=12, description="Month (defaults to current month)"),
    week_start: Optional[str] = Query(None, description="ISO date in target week (YYYY-MM-DD). Enables week view"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get month-scoped statistics for a specific project."""
    period_granularity, period_start, period_end, target_year, target_month, target_week_start, period_label = _resolve_period(
        year=year,
        month=month,
        week_start=week_start,
    )

    def _query():
        try:
            project = session.query(Project).filter(Project.id == project_id).first()
            if project is None:
                return None, "not_found"
            if not _can_access_project(session, project, current_user):
                return None, "forbidden"

            issues = _query_scoped_issues(
                session,
                current_user,
                _is_admin_user(current_user),
                period_start=period_start,
                period_end=period_end,
                project_id=project_id,
            )
            stats = _compute_issue_stats(session, issues)
            return ProjectAnalyticsResponse(
                period_granularity=period_granularity,
                project_id=project_id,
                project_name=project.name,
                year=target_year,
                month=target_month,
                week_start=target_week_start,
                period_label=period_label,
                period_start=period_start,
                period_end=period_end,
                **stats,
            ), "ok"
        finally:
            pass

    result, err = _query()
    if err == "not_found":
        raise HTTPException(status_code=404, detail="Project not found")
    if err == "forbidden":
        raise HTTPException(status_code=403, detail="Not authorized to view analytics for this project")
    return result


@router.get("/api/analytics/issues", response_model=list[IssueResponse])
def list_analytics_issues(
    year: Optional[int] = Query(None, description="Year (defaults to current year)"),
    month: Optional[int] = Query(None, ge=1, le=12, description="Month (defaults to current month)"),
    week_start: Optional[str] = Query(None, description="ISO date in target week (YYYY-MM-DD). Enables week view"),
    project_id: Optional[int] = Query(None, description="Filter by project"),
    product_id: Optional[int] = Query(None, description="Filter by product"),
    issue_type: Optional[str] = Query(None, alias="type", description="Filter by issue type; comma-separated for multiple"),
    issue_status: Optional[str] = Query(None, alias="status", description="Filter by status; comma-separated for multiple"),
    owner: Optional[str] = Query(None, description="Filter by owner name"),
    assignee: Optional[str] = Query(None, description="Filter by assignee name"),
    search: Optional[str] = Query(None, description="Free-text search across issue, project, product, owner, assignee"),
    limit: int = Query(300, ge=1, le=1000),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List month-scoped issues for analytics drill-down views."""
    is_admin = _is_admin_user(current_user)
    _, period_start, period_end, _, _, _, _ = _resolve_period(
        year=year,
        month=month,
        week_start=week_start,
    )

    def _query():
        from api.routers.issues import _enrich_issue

        try:
            issues = _query_scoped_issues(
                session,
                current_user,
                is_admin,
                period_start=period_start,
                period_end=period_end,
                project_id=project_id,
                product_id=product_id,
                issue_type=issue_type,
                issue_status=issue_status,
            )
            enriched = [_enrich_issue(session, issue) for issue in issues]

            owner_query = (owner or "").strip().lower()
            assignee_query = (assignee or "").strip().lower()
            search_query = (search or "").strip().lower()
            filtered = []
            for item in enriched:
                owner_name = " ".join(filter(None, [
                    item.get("project_owner_display_name"),
                    item.get("project_owner_username"),
                ])).lower()
                assignee_name = " ".join(filter(None, [
                    item.get("assignee_display_name"),
                    item.get("assignee_username"),
                ])).lower()
                haystack = " ".join(filter(None, [
                    str(item.get("id") or ""),
                    item.get("title"),
                    item.get("type"),
                    item.get("status"),
                    item.get("project_name"),
                    item.get("product_name"),
                    item.get("project_owner_display_name"),
                    item.get("project_owner_username"),
                    item.get("assignee_display_name"),
                    item.get("assignee_username"),
                ])).lower()
                if owner_query and owner_query not in owner_name:
                    continue
                if assignee_query and assignee_query not in assignee_name:
                    continue
                if search_query and search_query not in haystack:
                    continue
                filtered.append(item)
            return filtered[:limit]
        finally:
            pass

    return _query()


@router.get("/api/projects/{project_id}/export", response_model=ExportPayloadResponse)
def export_project(
    project_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Export a project with products, versions, runbooks, and issue history."""

    def _query():
        try:
            project = session.query(Project).filter(Project.id == project_id).first()
            if project is None:
                return None, "not_found"
            if not _can_access_project(session, project, current_user):
                return None, "forbidden"

            products = (
                session.query(Product)
                .filter(Product.project_id == project_id)
                .order_by(Product.id.asc())
                .all()
            )
            product_ids = [product.id for product in products]
            issues = (
                session.query(Issue)
                .filter(Issue.product_id.in_(product_ids))
                .order_by(Issue.updated_at.desc(), Issue.created_at.desc())
                .all()
                if product_ids else []
            )

            payload = {
                "project": _serialize_project_with_owner(project),
                "products": [
                    _serialize_product_bundle(session, product)
                    for product in products
                ],
                "issue_summary": _compute_issue_stats(session, issues),
            }
            return ExportPayloadResponse(
                export_type="project",
                generated_at=datetime.utcnow(),
                filters={"project_id": project_id},
                data=payload,
            ), "ok"
        finally:
            pass

    result, err = _query()
    if err == "not_found":
        raise HTTPException(status_code=404, detail="Project not found")
    if err == "forbidden":
        raise HTTPException(status_code=403, detail="Not authorized to export this project")
    return result


@router.get("/api/products/{product_id}/export", response_model=ExportPayloadResponse)
def export_product(
    product_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Export a product with versions, runbooks, and issue history."""

    def _query():
        try:
            product = session.query(Product).filter(Product.id == product_id).first()
            if product is None:
                return None, "not_found"
            if not _can_access_product(session, product, current_user):
                return None, "forbidden"

            project = session.query(Project).filter(Project.id == product.project_id).first()
            issues = (
                session.query(Issue)
                .filter(Issue.product_id == product_id)
                .order_by(Issue.updated_at.desc(), Issue.created_at.desc())
                .all()
            )
            payload = {
                "project": _serialize_project_with_owner(project) if project else None,
                **_serialize_product_bundle(session, product),
                "issue_summary": _compute_issue_stats(session, issues),
            }
            return ExportPayloadResponse(
                export_type="product",
                generated_at=datetime.utcnow(),
                filters={"product_id": product_id},
                data=payload,
            ), "ok"
        finally:
            pass

    result, err = _query()
    if err == "not_found":
        raise HTTPException(status_code=404, detail="Product not found")
    if err == "forbidden":
        raise HTTPException(status_code=403, detail="Not authorized to export this product")
    return result


@router.get("/api/analytics/export", response_model=ExportPayloadResponse)
def export_relayops(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Export the full RelayOps data set for platform admins."""
    if not _is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only platform admins can export the whole RelayOps")


    def _query():
        from api.routers.issues import _enrich_issue

        try:
            projects = session.query(Project).order_by(Project.id.asc()).all()
            products = session.query(Product).order_by(Product.id.asc()).all()
            issues = session.query(Issue).order_by(Issue.updated_at.desc(), Issue.created_at.desc()).all()

            payload = {
                "projects": [_serialize_project_with_owner(project) for project in projects],
                "products": [_serialize_product_bundle(session, product) for product in products],
                "issues": [_enrich_issue(session, issue) for issue in issues],
                "issue_summary": _compute_issue_stats(session, issues),
            }
            return ExportPayloadResponse(
                export_type="relayops",
                generated_at=datetime.utcnow(),
                filters={"scope": "all"},
                data=payload,
            )
        finally:
            pass

    return _query()
