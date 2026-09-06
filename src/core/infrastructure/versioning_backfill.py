"""Startup backfill for legacy products/issues into ProductVersion semantics."""

from __future__ import annotations

from typing import Dict, Optional

from core.models.database import get_db
from core.models.entities import (
    Issue,
    IssueStatus,
    IssueType,
    Product,
    ProductLifecycleStatus,
    ProductStatus,
    ProductVersion,
    ProductVersionStatus,
)
from core.logging import get_logger
from core.services.product_version_service import (
    _sorted_product_versions,
    ensure_product_baseline_version,
    get_current_approved_version,
    get_current_draft_version,
    refresh_product_version_artifacts,
)

logger = get_logger(__name__)


def _pick_latest_version(session, product_id: int) -> Optional[ProductVersion]:
    versions = _sorted_product_versions(session, product_id)
    return versions[0] if versions else None


def _repair_product_refs(session, product: Product, stats: Dict[str, int]) -> None:
    versions = _sorted_product_versions(session, product.id)
    if not versions:
        ensure_product_baseline_version(session, product)
        stats["products_backfilled"] += 1
        return

    approved = next((v for v in versions if v.version_status == ProductVersionStatus.APPROVED), None)
    active_draft = next(
        (v for v in versions if v.version_status in (ProductVersionStatus.DRAFT, ProductVersionStatus.PENDING_REVIEW)),
        None,
    )

    changed = False
    if product.current_approved_version_id is None and approved is not None:
        product.current_approved_version_id = approved.id
        changed = True
    if product.current_draft_version_id is None and active_draft is not None:
        product.current_draft_version_id = active_draft.id
        changed = True

    if product.status == ProductStatus.ACTIVE and product.current_approved_version_id is None:
        latest = _pick_latest_version(session, product.id)
        if latest is not None:
            latest.version_status = ProductVersionStatus.APPROVED
            product.current_approved_version_id = latest.id
            changed = True
    elif product.status == ProductStatus.PENDING_REVIEW and product.current_draft_version_id is None:
        latest = _pick_latest_version(session, product.id)
        if latest is not None:
            latest.version_status = ProductVersionStatus.PENDING_REVIEW
            product.current_draft_version_id = latest.id
            changed = True
    elif product.status == ProductStatus.DRAFT and product.current_draft_version_id is None:
        latest = _pick_latest_version(session, product.id)
        if latest is not None:
            if latest.version_status not in (ProductVersionStatus.DRAFT, ProductVersionStatus.PENDING_REVIEW):
                latest.version_status = ProductVersionStatus.DRAFT
            product.current_draft_version_id = latest.id
            changed = True

    if product.status == ProductStatus.ARCHIVED:
        product.lifecycle_status = ProductLifecycleStatus.ARCHIVED
        changed = True
    elif product.current_approved_version_id:
        product.lifecycle_status = ProductLifecycleStatus.ACTIVE
        changed = True
    else:
        product.lifecycle_status = ProductLifecycleStatus.DRAFTING
        changed = True

    if changed:
        stats["products_repaired"] += 1

    for version in versions:
        if version.snapshot_json is None or version.completeness_summary_json is None:
            refresh_product_version_artifacts(session, version)
            stats["version_artifacts_refreshed"] += 1


def _choose_issue_version(session, product: Product, issue: Issue) -> ProductVersion:
    if issue.status == IssueStatus.OPEN:
        draft = get_current_draft_version(session, product)
        if draft is None:
            draft = ensure_product_baseline_version(session, product)
        if draft.version_status == ProductVersionStatus.DRAFT:
            draft.version_status = ProductVersionStatus.PENDING_REVIEW
            draft.submitted_by = draft.submitted_by or issue.created_by
            draft.submitted_at = draft.submitted_at or issue.created_at
            refresh_product_version_artifacts(session, draft)
        if product.current_approved_version_id:
            product.status = ProductStatus.ACTIVE
            product.lifecycle_status = ProductLifecycleStatus.ACTIVE
        else:
            product.status = ProductStatus.PENDING_REVIEW
            product.lifecycle_status = ProductLifecycleStatus.DRAFTING
        return draft

    approved = get_current_approved_version(session, product)
    draft = get_current_draft_version(session, product)
    latest = _pick_latest_version(session, product.id) or ensure_product_baseline_version(session, product)

    if issue.rejection_reason:
        return draft or latest
    if approved is not None:
        return approved
    return draft or latest


def backfill_legacy_versioning_state() -> Dict[str, int]:
    """
    Backfill legacy products/issues so startup state matches ProductVersion semantics.

    This is safe to run repeatedly. It repairs missing refs and best-effort binds
    legacy handover issues to versions.
    """
    db = get_db()
    session = db.get_session()
    stats = {
        "products_backfilled": 0,
        "products_repaired": 0,
        "issues_bound": 0,
        "version_artifacts_refreshed": 0,
    }
    try:
        products = session.query(Product).order_by(Product.id.asc()).all()
        for product in products:
            _repair_product_refs(session, product, stats)

        handover_issues = (
            session.query(Issue)
            .filter(Issue.type == IssueType.HANDOVER_REVIEW, Issue.product_id.isnot(None))
            .order_by(Issue.id.asc())
            .all()
        )
        for issue in handover_issues:
            product = session.query(Product).filter(Product.id == issue.product_id).first()
            if product is None:
                continue
            version = None
            if issue.product_version_id is not None:
                version = session.query(ProductVersion).filter(ProductVersion.id == issue.product_version_id).first()
            if version is None:
                version = _choose_issue_version(session, product, issue)
                issue.product_version_id = version.id
                stats["issues_bound"] += 1

        session.commit()
        logger.info(
            "Legacy versioning backfill complete: products_backfilled={} products_repaired={} issues_bound={} artifacts_refreshed={}",
            stats["products_backfilled"],
            stats["products_repaired"],
            stats["issues_bound"],
            stats["version_artifacts_refreshed"],
        )
        return stats
    except Exception:
        session.rollback()
        logger.opt(exception=True).warning("Legacy versioning backfill failed; continuing startup")
        return stats
    finally:
        session.close()
