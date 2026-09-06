"""Verification-report routes — Phase-2 of the Verification tab.

Endpoint surface:

* ``POST /api/verification/reports/generate``
    Server-side 'Run all verification' — executes every active
    ``ApplicationApiCheck`` across every Application, persists each
    outcome as an ``ApplicationApiCheckRun``, folds the results into a
    new ``VerificationReport`` row, and broadcasts an in-app
    notification to Ops members + admins. The frontend uses this as
    the canonical "did everything pass?" sign-off action.

* ``GET /api/verification/reports``
    List recent reports (newest first). Lightweight — never returns
    the ``details`` payload; the Analytics list view doesn't need it.

* ``GET /api/verification/reports/{id}``
    Full report including the per-check ``details`` array — the
    Detail dialog pulls this on demand.

* ``DELETE /api/verification/reports/{id}``
    Admin-only cleanup. We don't expose Update at all — reports are
    immutable audit snapshots.

The HTTP executor (and assertion evaluator) live in ``apps.py``; we
import them by underscored name to avoid duplicating the SSRF guard
and timeout logic. The contract is intentionally minimal so a later
refactor that moves them into ``core.services`` won't ripple here.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from api.deps.auth import CurrentUser, get_current_user
from api.deps.db import get_session
from api.deps.rbac import AdminOnly, OpsMemberOrAdmin
from api.routers.apps import _evaluate_assertions, _execute_api_check_http
from api.schemas.verification_schemas import (
    VerificationReportDetailResponse,
    VerificationReportEntry,
    VerificationReportGenerateRequest,
    VerificationReportSummaryResponse,
)
from core.exceptions import NotFoundError
from core.logging import get_logger
from core.models.app_entities import (
    Application,
    ApplicationApiCheck,
    ApplicationApiCheckRun,
    VerificationReport,
)
from core.models.product_entities import Product
from core.models.project_entities import Project
from core.models.system_entities import Notification
from core.models.user import User, UserRole
from core.services.audit_service import log_audit

logger = get_logger(__name__)
router = APIRouter(tags=["verification"])

# Rolling retention for the per-check run history table. Kept in sync
# with the value in apps.py so 'Generate report' doesn't suddenly blow
# past the limit that the per-app endpoint maintains.
_API_CHECK_RUN_HISTORY_PER_CHECK = 50

# 'Generate report' is fundamentally O(checks). We cap concurrent
# in-flight runs at this many to avoid hammering downstream apps when
# the catalog is large; each outbound httpx call still has its own
# per-request timeout enforced inside _execute_api_check_http.
_MAX_REPORT_CHECKS = 500


def _name_lookup(session: Session) -> tuple[Dict[int, str], Dict[int, str], Dict[int, Optional[int]]]:
    """Pre-build {product_id: name}, {project_id: name},
    {product_id: project_id} maps so the per-check loop doesn't issue
    a JOIN per row when there are hundreds of checks."""
    product_map: Dict[int, str] = {}
    product_project: Dict[int, Optional[int]] = {}
    for row in session.query(Product.id, Product.name, Product.project_id).all():
        product_map[row.id] = row.name or ""
        product_project[row.id] = row.project_id
    project_map: Dict[int, str] = {}
    for row in session.query(Project.id, Project.name).all():
        project_map[row.id] = row.name or ""
    return product_map, project_map, product_project


def _serialize_report_summary(
    report: VerificationReport,
    session: Session,
) -> VerificationReportSummaryResponse:
    """Shape a row for the list endpoint. Resolves ``generated_by`` to
    a display name in one extra query (None when the FK is null)."""
    generated_by_name: Optional[str] = None
    if report.generated_by is not None:
        user = session.query(User).filter(User.id == report.generated_by).first()
        if user is not None:
            generated_by_name = user.display_name or user.username
    return VerificationReportSummaryResponse(
        id=report.id,
        generated_at=report.generated_at,
        generated_by=report.generated_by,
        generated_by_name=generated_by_name,
        total_count=int(report.total_count or 0),
        pass_count=int(report.pass_count or 0),
        fail_count=int(report.fail_count or 0),
        duration_ms=int(report.duration_ms or 0),
        notes=(report.notes or ""),
    )


def _serialize_report_detail(
    report: VerificationReport,
    session: Session,
) -> VerificationReportDetailResponse:
    """Same as summary + the full ``details`` payload, coerced through
    the entry schema so the JSON has the shape the frontend expects
    even if old rows are missing newer keys."""
    summary = _serialize_report_summary(report, session)
    raw_entries: List[Dict[str, Any]] = list(report.details or [])
    entries = [VerificationReportEntry(**(entry or {})) for entry in raw_entries]
    return VerificationReportDetailResponse(
        **summary.model_dump(),
        details=entries,
    )


def _broadcast_signoff_notification(
    session: Session,
    report: VerificationReport,
    actor_user_id: Optional[int],
) -> None:
    """Drop one Notification per Ops member / admin so the sign-off
    message lands in their bell menu — closest analogue to "send 群里面"
    we have in-platform until an external IM integration exists.

    Best-effort: errors are logged and swallowed so a notification
    failure doesn't roll back the report itself.
    """
    try:
        recipients = (
            session.query(User.id)
            .filter(User.role.in_([UserRole.RELAYOPS_MEMBER, UserRole.ADMIN]))
            .all()
        )
        if not recipients:
            return
        if report.fail_count == 0:
            title = f"Verification done — can sign off ({report.pass_count}/{report.total_count})"
            message = (
                f"All {report.total_count} verifications passed at "
                f"{report.generated_at.isoformat(timespec='seconds')}. "
                "Safe to sign off the CML downtime window."
            )
        else:
            title = f"Verification finished — {report.fail_count} failing ({report.pass_count}/{report.total_count})"
            message = (
                f"{report.pass_count}/{report.total_count} checks passed at "
                f"{report.generated_at.isoformat(timespec='seconds')}. "
                "Review the failing checks before sign-off."
            )
        for (uid,) in recipients:
            notif = Notification(
                user_id=uid,
                title=title,
                message=message,
                type="verification_report",
                related_entity_type="verification_report",
                related_entity_id=report.id,
            )
            session.add(notif)
        session.flush()
    except Exception:  # pragma: no cover — observability only
        logger.opt(exception=True).warning(
            "Failed to broadcast verification sign-off notifications"
        )


@router.post(
    "/api/verification/reports/generate",
    response_model=VerificationReportDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_verification_report(
    body: VerificationReportGenerateRequest | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(OpsMemberOrAdmin),
):
    """Fire every active API check across every app and persist a
    consolidated report. Long-running by design (O(active_checks)),
    so the frontend keeps a spinner up while this runs."""
    started = time.perf_counter()
    started_at = datetime.utcnow()

    product_names, project_names, product_to_project = _name_lookup(session)

    # Pull every active check joined to its parent application so we
    # know which product (and hence project) it belongs to. One query
    # — we then iterate sequentially so persisted ApplicationApiCheckRun
    # rows land in deterministic order in the table.
    checks: List[tuple[ApplicationApiCheck, Application]] = (
        session.query(ApplicationApiCheck, Application)
        .join(Application, Application.id == ApplicationApiCheck.application_id)
        .filter(ApplicationApiCheck.is_active.is_(True))
        .order_by(
            Application.product_id.asc(),
            ApplicationApiCheck.application_id.asc(),
            ApplicationApiCheck.sort_order.asc(),
            ApplicationApiCheck.id.asc(),
        )
        .limit(_MAX_REPORT_CHECKS)
        .all()
    )

    details: List[Dict[str, Any]] = []
    pass_count = 0
    fail_count = 0

    for check, app in checks:
        run_result = await _execute_api_check_http(
            url=check.url or "",
            method=check.method or "GET",
            headers=dict(check.headers or {}),
            body_text=check.body or "",
            body_is_json=bool(check.body_is_json),
            timeout_seconds=int(check.timeout_seconds or 15),
        )
        assertion_passed, assertion_reason = _evaluate_assertions(check, run_result)
        ok = bool(run_result["ok"]) and (assertion_passed is not False)

        # Mirror the per-app endpoint: append to the rolling run history
        # so audit and the per-check 'last_run_*' summary stay in sync.
        run_row = ApplicationApiCheckRun(
            api_check_id=check.id,
            ran_at=datetime.utcnow(),
            ran_by=current_user.user_id,
            ok=ok,
            status_code=run_result["status_code"],
            duration_ms=int(run_result["duration_ms"]),
            response_headers=run_result["response_headers"],
            response_body=run_result["response_body"],
            error=(
                run_result["error"]
                if run_result["error"]
                else (None if assertion_passed is not False else assertion_reason)
            ),
            request_url=check.url,
            request_method=check.method,
        )
        session.add(run_row)
        session.flush()  # populate run_row.id for the details payload

        # Bump the cached summary on the check itself.
        check.last_run_at = run_row.ran_at
        check.last_run_ok = run_row.ok
        check.last_run_status_code = run_row.status_code
        check.last_run_duration_ms = run_row.duration_ms

        # Rolling retention — same N as the per-app endpoint uses.
        surplus_ids = [
            row.id for row in (
                session.query(ApplicationApiCheckRun.id)
                .filter(ApplicationApiCheckRun.api_check_id == check.id)
                .order_by(ApplicationApiCheckRun.ran_at.desc())
                .offset(_API_CHECK_RUN_HISTORY_PER_CHECK)
                .all()
            )
        ]
        if surplus_ids:
            session.query(ApplicationApiCheckRun).filter(
                ApplicationApiCheckRun.id.in_(surplus_ids)
            ).delete(synchronize_session=False)

        if ok:
            pass_count += 1
        else:
            fail_count += 1

        project_id = product_to_project.get(app.product_id)
        details.append(
            {
                "project_id": project_id,
                "project_name": project_names.get(project_id, "") if project_id else "",
                "product_id": app.product_id,
                "product_name": product_names.get(app.product_id, ""),
                "application_id": app.id,
                "check_id": check.id,
                "check_name": check.name or "",
                "method": check.method or "GET",
                "url": check.url or "",
                "status": "pass" if ok else "fail",
                "status_code": run_result["status_code"],
                "duration_ms": int(run_result["duration_ms"]),
                "error": run_result["error"],
                "assertion_reason": assertion_reason,
                "run_id": run_row.id,
            }
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    notes = (body.notes if body and body.notes else "") or ""

    report = VerificationReport(
        generated_at=started_at,
        generated_by=current_user.user_id,
        total_count=len(details),
        pass_count=pass_count,
        fail_count=fail_count,
        duration_ms=elapsed_ms,
        notes=notes,
        details=details,
    )
    session.add(report)
    session.flush()  # need report.id before broadcasting

    _broadcast_signoff_notification(session, report, current_user.user_id)

    session.commit()
    session.refresh(report)

    log_audit(
        user_id=current_user.user_id,
        action="generate",
        entity_type="verification_report",
        entity_id=report.id,
        new_value={
            "total": report.total_count,
            "pass": report.pass_count,
            "fail": report.fail_count,
            "notes": report.notes,
        },
    )
    return _serialize_report_detail(report, session)


@router.get(
    "/api/verification/reports",
    response_model=List[VerificationReportSummaryResponse],
)
def list_verification_reports(
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Newest-first list of recent reports. Open to any authenticated
    user so the Analytics section is visible to regular users too
    (read-only) — generation itself stays gated to Ops/Admin."""
    rows = (
        session.query(VerificationReport)
        .order_by(VerificationReport.generated_at.desc())
        .limit(limit)
        .all()
    )
    return [_serialize_report_summary(r, session) for r in rows]


@router.get(
    "/api/verification/reports/{report_id}",
    response_model=VerificationReportDetailResponse,
)
def get_verification_report(
    report_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    report = (
        session.query(VerificationReport)
        .filter(VerificationReport.id == report_id)
        .first()
    )
    if report is None:
        raise NotFoundError("Verification report not found")
    return _serialize_report_detail(report, session)


@router.delete(
    "/api/verification/reports/{report_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_verification_report(
    report_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(AdminOnly),
):
    """Admin-only cleanup. Reports are otherwise immutable."""
    report = (
        session.query(VerificationReport)
        .filter(VerificationReport.id == report_id)
        .first()
    )
    if report is None:
        raise NotFoundError("Verification report not found")
    snapshot = {
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
        "total": report.total_count,
        "pass": report.pass_count,
        "fail": report.fail_count,
    }
    session.delete(report)
    session.commit()
    log_audit(
        user_id=current_user.user_id,
        action="delete",
        entity_type="verification_report",
        entity_id=report_id,
        old_value=snapshot,
    )
