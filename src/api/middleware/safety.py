"""Safety middleware: catch-all ASGI wrapper that guarantees a response even if FastAPI internals fail."""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from core.logging import get_logger

logger = get_logger(__name__)


class SafetyMiddleware:
    """ASGI middleware that wraps the entire app to prevent unhandled exceptions from hanging connections.

    This sits below FastAPI's own exception handling. If anything escapes
    (including errors in middleware, lifespan, or framework internals),
    this layer catches it, logs the traceback, and returns a 500 JSON response
    so the HTTP connection is always properly closed.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        except Exception:
            logger.opt(exception=True).critical("SafetyMiddleware caught unhandled exception")
            try:
                Request(scope)
                response = JSONResponse(
                    status_code=500,
                    content={"detail": "Internal server error"},
                )
                await response(scope, receive, send)
            except Exception:
                logger.opt(exception=True).error("SafetyMiddleware failed to send error response")
