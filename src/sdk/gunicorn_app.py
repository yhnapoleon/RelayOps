"""Custom gunicorn application with worker lifecycle tracking."""

import multiprocessing
import time

from gunicorn.app.base import BaseApplication

from core.logging import get_logger

logger = get_logger(__name__)

# Fork-safe shared counters. Created in the master at module import (before any
# worker fork); children inherit the same shared-memory pages via fork().
_configured = multiprocessing.Value("i", 0)  # int32
_alive = multiprocessing.Value("i", 0)  # int32
_restarts = multiprocessing.Value("Q", 0)  # uint64
_last_restart = multiprocessing.Value("d", 0.0)  # float64


def get_worker_stats() -> dict:
    """Return a snapshot of current worker stats."""
    return {
        "configured": _configured.value,
        "alive": _alive.value,
        "restarts": _restarts.value,
        "last_restart": _last_restart.value,
    }


class RelayOpsCenterGunicornApp(BaseApplication):
    """Custom gunicorn application that loads the FastAPI app and tracks worker health."""

    def __init__(self, app_uri: str, options: dict = None):
        self.app_uri = app_uri
        self.options = options or {}
        super().__init__()

    def load_config(self):
        """Apply configuration from the options dict to gunicorn."""
        for key, value in self.options.items():
            if key in self.cfg.settings and value is not None:
                self.cfg.set(key.lower(), value)

        # Register lifecycle hooks
        self.cfg.set("on_starting", self._on_starting)
        self.cfg.set("pre_fork", self._pre_fork)
        self.cfg.set("post_worker_init", self._post_worker_init)
        self.cfg.set("child_exit", self._child_exit)
        self.cfg.set("on_exit", self._on_exit)

    def load(self):
        """Load the ASGI application."""
        from api.main import app

        return app

    # --- Lifecycle hooks ---

    @staticmethod
    def _on_starting(server):
        """Called just before the master process is initialized. Runs in master."""
        workers = server.app.cfg.settings["workers"].get()
        with _configured.get_lock():
            _configured.value = workers
        with _alive.get_lock():
            _alive.value = 0
        logger.info("Gunicorn starting with {} worker(s)", workers)

    @staticmethod
    def _pre_fork(server, worker):
        """Called just before a worker is forked. Runs in master."""
        with _alive.get_lock():
            _alive.value += 1

    @staticmethod
    def _post_worker_init(worker):
        """Called just after a worker has been initialized. Runs in the worker."""
        logger.info("Worker initialized (pid: {}, age: {})", worker.pid, worker.age)

    @staticmethod
    def _child_exit(server, worker):
        """Called when a worker process exits. Runs in master."""
        with _alive.get_lock():
            _alive.value = max(0, _alive.value - 1)
            alive_now = _alive.value
        with _restarts.get_lock():
            _restarts.value += 1
            restarts_now = _restarts.value
        with _last_restart.get_lock():
            _last_restart.value = time.time()
        logger.warning(
            "Worker exited (pid: {}, alive: {}, total restarts: {})",
            worker.pid,
            alive_now,
            restarts_now,
        )

    @staticmethod
    def _on_exit(server):
        """Called just before exiting gunicorn. Runs in master."""
        with _alive.get_lock():
            _alive.value = 0
        logger.info("Gunicorn shutting down")
