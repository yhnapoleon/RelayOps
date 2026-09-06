"""Prometheus metrics endpoint: GET /metrics.

Public (no authentication) by design — Prometheus scrapers don't carry a JWT.
The response exposes only aggregated counts (no PII, no business content), so
this follows the standard application-metrics pattern.

On CML the whole app sits behind Cloudera's authenticated reverse proxy, so
reachability/protection of this endpoint is handled at the network layer (a
scraper inside the cluster, or the scrape config's bearer token) rather than
in the app. See the Frontier project for the same decision.
"""

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from prometheus_client import generate_latest

from core.logging import get_logger
from core.metrics.metrics import (
    format_business_metrics,
    get_metrics_content_type,
    registry,
)
from core.models.database import get_db

logger = get_logger(__name__)

router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def metrics():
    """Return business metrics (queried live) + runtime metrics (in-process)."""

    def _collect_business() -> str:
        session = get_db().get_session()
        try:
            return format_business_metrics(session)
        finally:
            session.close()

    try:
        # DB work off the event loop so a slow query can't block the loop.
        text = await run_in_threadpool(_collect_business)
        text += generate_latest(registry).decode()
        return Response(content=text, media_type=get_metrics_content_type())
    except Exception as exc:
        logger.opt(exception=True).error("Failed to collect metrics")
        return Response(
            content=f"# ERROR: failed to collect metrics: {exc}\n",
            media_type="text/plain",
            status_code=500,
        )
