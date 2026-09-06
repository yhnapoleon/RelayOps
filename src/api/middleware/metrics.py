"""Prometheus HTTP metrics middleware.

Pure-ASGI middleware (same shape as SafetyMiddleware) that records a request
count + latency for every HTTP request into the private metrics registry.

It reads the matched route TEMPLATE from ``scope["route"].path`` (populated by
Starlette's router during the inner call), so dynamic routes collapse to a
single low-cardinality label — ``/api/projects/{project_id}`` rather than one
series per id. Unmatched requests (404 / no route) fall back to ``unmatched``.
"""

import time

from starlette.types import ASGIApp, Receive, Scope, Send

from core.metrics.metrics import record_http_request

# /metrics would otherwise count itself on every scrape; exclude it.
_EXCLUDED_PATHS = {"/metrics"}


class PrometheusMiddleware:
    """Record per-request count + duration, labelled by route template."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_holder = {"code": 500}

        async def _send(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            route = scope.get("route")
            path = getattr(route, "path", None) or "unmatched"
            if path not in _EXCLUDED_PATHS:
                record_http_request(
                    method=scope.get("method", "UNKNOWN"),
                    path=path,
                    status=status_holder["code"],
                    duration_seconds=time.perf_counter() - start,
                )
