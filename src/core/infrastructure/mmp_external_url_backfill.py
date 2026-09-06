"""
Backfill ``Issue.external_url`` on existing open MMP issues.

The MMP deep link (``<web>/project/{numeric_id}/models``) is written onto an
Issue at creation time. Issues created before that feature existed have
``external_url = NULL``, so the "Open in MMP" button can't render for them.

This backfill repairs those rows: for every OPEN / IN_PROGRESS MMP issue with a
missing ``external_url``, it resolves the issue's Job → MMP project repo name →
numeric project id (via one shallow MMP directory fetch) and stores the derived
URL. Idempotent — rows that already have a URL, or whose URL can't be derived
(MMP unconfigured/unreachable, or no derivable web base), are left untouched and
simply retried on the next startup.
"""
from __future__ import annotations

from typing import Dict

from core.config import get_config
from core.models.database import get_db
from core.models.entities import Issue, IssueStatus, IssueType, Job
from core.logging import get_logger

logger = get_logger(__name__)

_MMP_ISSUE_TYPES = [
    IssueType.MMP_DRIFT,
    IssueType.MMP_RUN_PENDING_APPROVAL,
    IssueType.MMP_PENDING_REVIEW,
]


def backfill_mmp_issue_external_urls() -> Dict[str, int]:
    """Populate external_url on open MMP issues that are missing it.

    Safe to run on every startup. Returns a small stats dict.
    """
    stats = {"candidates": 0, "updated": 0}
    cfg = get_config()

    db = get_db()
    session = db.get_session()
    try:
        candidates = (
            session.query(Issue)
            .filter(
                Issue.type.in_(_MMP_ISSUE_TYPES),
                Issue.status.in_([IssueStatus.OPEN, IssueStatus.IN_PROGRESS]),
                Issue.external_url.is_(None),
                Issue.job_id.isnot(None),
            )
            .all()
        )
        stats["candidates"] = len(candidates)
        if not candidates:
            return stats

        # Build the repo_name -> numeric project id map once (one HTTP call).
        from core.integrations.mmp_interface import MmpApiError, MmpInterface

        iface = MmpInterface(
            base_url=cfg.mmp_base_url,
            bearer_token=cfg.mmp_bearer_token,
            refresh_token=cfg.mmp_refresh_token,
            verify_ssl=cfg.mmp_verify_ssl,
            ca_bundle=cfg.mmp_ca_bundle_path or None,
            timeout=float(cfg.mmp_timeout_seconds),
        )
        if not iface.is_configured():
            logger.info(
                "MMP external_url backfill skipped: MMP not configured "
                "({} candidate issue(s) left for a later run)",
                stats["candidates"],
            )
            return stats
        try:
            directory = iface.list_projects_shallow()
        except MmpApiError as exc:
            logger.info(
                "MMP external_url backfill skipped: directory fetch failed ({}); "
                "{} candidate(s) left for a later run",
                exc.message,
                stats["candidates"],
            )
            return stats

        # Cache job_id -> repo_name so multiple issues on one job share a lookup.
        job_repo: Dict[int, str] = {}
        for issue in candidates:
            repo_name = job_repo.get(issue.job_id)
            if repo_name is None:
                job = session.query(Job).filter(Job.id == issue.job_id).first()
                repo_name = (job.mmp_project_id or "") if job else ""
                job_repo[issue.job_id] = repo_name
            if not repo_name:
                continue
            info = directory.get(repo_name)
            if not info:
                continue
            url = cfg.mmp_model_web_url(info.get("id"))
            if url:
                issue.external_url = url
                stats["updated"] += 1

        if stats["updated"]:
            session.commit()
            logger.info(
                "MMP external_url backfill complete: {} of {} candidate issue(s) updated",
                stats["updated"],
                stats["candidates"],
            )
        return stats
    except Exception:
        session.rollback()
        logger.opt(exception=True).warning(
            "MMP external_url backfill failed; continuing startup"
        )
        return stats
    finally:
        session.close()
