"""SPA static files mounting logic."""

import os
import mimetypes

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from core.frontend.frontend import Frontend
from core.logging import get_logger

logger = get_logger(__name__)


class SPAStaticFiles(StaticFiles):
    """
    StaticFiles subclass with SPA fallback behavior.

    For single-page applications, serves index.html for any path that
    doesn't match an existing static file, enabling client-side routing.
    """

    async def get_response(self, path: str, scope):
        """
        Get response for a path, falling back to index.html for unknown paths.

        Args:
            path: Requested file path.
            scope: ASGI scope dict.

        Returns:
            Static file response or index.html fallback.
        """
        try:
            return await super().get_response(path, scope)
        except Exception:
            logger.debug("SPA fallback: serving index.html for path: {}", path)
            return await super().get_response("index.html", scope)


def mount_spa(app: FastAPI) -> None:
    """
    Mount the frontend build directory as a single-page application.

    Configures the FastAPI app to serve static files from the frontend
    build directory (dist/) with SPA fallback routing (unknown paths serve index.html).

    Args:
        app: FastAPI application instance.
    """
    mimetypes.add_type("text/javascript", ".js")
    frontend = Frontend()
    build_dir = frontend.get_build_dir()
    if not os.path.isdir(build_dir):
        logger.warning("Frontend build directory missing; skipping SPA mount: {}", build_dir)
        return
    app.mount(
        "/",
        SPAStaticFiles(directory=build_dir, html=True),
        name="spa-static-files",
    )
