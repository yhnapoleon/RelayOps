"""Access-log middleware that logs on state change, not on every request.

CDSW keeps the Application alive by polling ``CDSW_APP_POLLING_ENDPOINT`` ('/')
continuously, and external uptime checks hit ``/healthz``. Logging every
"GET / 200" on this single-process deployment floods stdout and is exactly the
kind of unbounded log volume that can back-pressure the whole process.

This middleware keeps the signal while dropping the noise:

  * Poll paths ('/', '/healthz'): a line is emitted only when the status code
    *changes* — the first response, and every later regression (200 -> 5xx)
    or recovery (5xx -> 200). Steady-state identical responses are dropped.
  * All other paths: user-driven and low volume, so they are logged normally
    (2xx/3xx at INFO, 4xx at WARNING, 5xx at ERROR).

It replaces uvicorn/gunicorn's own access log (disabled on the CDSW path), so
a probe that starts failing is still immediately visible.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.logging import get_logger

logger = get_logger(__name__)

# Paths hit by health/uptime pollers. Steady-state 2xx repeats on these are
# suppressed. Kept as a fixed, tiny set so the per-path status cache below can
# never grow unbounded (the leak this whole change set is guarding against).
_POLL_PATHS = frozenset({"/", "/healthz"})


class AccessLogMiddleware:
    """Log HTTP requests, suppressing steady-state poll-path repeats."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # Only ever keyed by members of _POLL_PATHS -> bounded to <= 2 entries.
        self._last_poll_status: dict[str, int] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "?")
        path = scope.get("path", "?")
        status = {"code": 0}

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        await self.app(scope, receive, _send)
        self._record(method, path, status["code"])

    def _record(self, method: str, path: str, status: int) -> None:
        if path in _POLL_PATHS:
            # Emit only on change so the continuous poller logs once, then only
            # when it flips healthy<->unhealthy.
            if self._last_poll_status.get(path) == status:
                return
            self._last_poll_status[path] = status
            self._emit(method, path, status, suffix=" (repeats suppressed)")
            return
        self._emit(method, path, status)

    def _emit(self, method: str, path: str, status: int, suffix: str = "") -> None:
        if status >= 500:
            logger.error("{} {} -> {}{}", method, path, status, suffix)
        elif status >= 400:
            logger.warning("{} {} -> {}{}", method, path, status, suffix)
        else:
            logger.info("{} {} -> {}{}", method, path, status, suffix)
