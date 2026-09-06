"""Control Interface — CML v2 API client.

Encapsulates all GET-only communication with the CML v2 contract documented
in CML/job.md and CML/app.md. The interface authenticates via
``Authorization: Bearer <api_key>``, transparently follows ``next_page_token``
pagination, and surfaces typed errors via :class:`CmlApiError` so callers
never need to know about httpx exceptions.

Methods are organized by the CML resource hierarchy:
  * project name discovery / resolution  -> :meth:`list_project_names`,
    :meth:`list_projects`, :meth:`resolve_project_id`
  * project detail                       -> :meth:`get_project`
  * job discovery / detail / runs        -> :meth:`list_jobs`,
    :meth:`resolve_job_id`, :meth:`get_job`, :meth:`list_job_runs`,
    :meth:`get_job_run`
  * application discovery / detail       -> :meth:`list_applications`,
    :meth:`resolve_application_id`, :meth:`get_application`

Legacy methods :meth:`get_all_job_statuses` and :meth:`get_job_status` are
kept as deprecated stubs that always raise :class:`CmlApiError`. Existing
callers (cml_checker, mmp_interface, jobs router, product_service) wrap
ControlInterface calls in ``try/except CmlApiError`` so they degrade to
"CML unreachable" until Phase 2 step 6/9 rewires them onto the new methods.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

from core.integrations.normalization import (
    CanonicalControlMBatchEvent,
    CanonicalControlMJobStatus,
)

logger = logging.getLogger(__name__)


class CmlApiError(Exception):
    """Typed exception for CML API communication failures.

    ``status_code`` is set when the failure originated from an HTTP response
    (so callers can distinguish 401/403/404/5xx). It is ``None`` for
    timeouts, connection errors, or local errors (e.g. resolution misses).
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ControlInterface:
    """CML v2 API client used by checkers / routers / services.

    Construction parameters mirror the four config knobs in ``config.yaml``::

        cml_platform:
          base_url: ...
          api_key: ...
          verify_ssl: ...
          ca_bundle_path: ...

    plus ``default_project_name`` so the Controller can resolve the default
    project at startup.

    For back-compat the constructor still accepts ``auth_token`` as an alias
    for ``api_key``; it will be removed once all call sites are migrated.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        *,
        timeout: float = 10.0,
        verify_ssl: bool = True,
        ca_bundle: Optional[str] = None,
        default_project_name: Optional[str] = None,
        auth_token: Optional[str] = None,  # deprecated alias for api_key
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key if api_key is not None else (auth_token or "")
        self.timeout = timeout
        self.default_project_name = default_project_name or None
        # httpx ``verify`` accepts True / False / path-to-CA-bundle.
        if ca_bundle:
            self._verify: Any = ca_bundle
        else:
            self._verify = bool(verify_ssl)

    # ------------------------------------------------------------------ http

    def _build_headers(self) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # Retry budget for transient CML API errors. CML clusters occasionally
    # 502/504 during rolling restarts and TCP resets show up under load —
    # without this, every blip turns into a relayops_health="unknown" sample.
    # 4xx is *not* retried (auth / missing id / bad request are permanent).
    _RETRY_BACKOFFS_SEC = (0.5, 2.0)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        last_err: Optional[CmlApiError] = None
        attempts = len(self._RETRY_BACKOFFS_SEC) + 1
        for attempt in range(attempts):
            try:
                with httpx.Client(timeout=self.timeout, verify=self._verify) as client:
                    response = client.request(
                        method, url, headers=self._build_headers(), **kwargs
                    )
                    response.raise_for_status()
                    if not response.content:
                        return {}
                    return response.json()
            except httpx.TimeoutException as exc:
                last_err = CmlApiError(
                    f"Timeout connecting to CML API at {url}: {exc}",
                    status_code=None,
                )
                last_err.__cause__ = exc
            except httpx.HTTPStatusError as exc:
                sc = exc.response.status_code
                body = exc.response.text[:200] if exc.response.content else ""
                err = CmlApiError(
                    f"CML API {method} {path} returned HTTP {sc}: {body}",
                    status_code=sc,
                )
                err.__cause__ = exc
                if sc < 500:
                    raise err
                last_err = err
            except httpx.HTTPError as exc:
                last_err = CmlApiError(
                    f"Failed to connect to CML API at {url}: {exc}",
                    status_code=None,
                )
                last_err.__cause__ = exc

            if attempt < attempts - 1:
                backoff = self._RETRY_BACKOFFS_SEC[attempt]
                logger.info(
                    "CML API %s %s transient failure (attempt %d/%d), "
                    "retrying in %.1fs: %s",
                    method, path, attempt + 1, attempts, backoff,
                    last_err.message,
                )
                time.sleep(backoff)

        assert last_err is not None
        raise last_err

    def _paginated(
        self,
        path: str,
        *,
        items_key: str,
        params: Optional[dict] = None,
    ) -> list:
        """Follow CML's ``next_page_token`` until exhausted.

        Treats the token as opaque per CML/job.md §5.2 (no parsing/encoding).
        """
        params = dict(params or {})
        out: list = []
        while True:
            data = self._request("GET", path, params=params)
            items = data.get(items_key) or []
            if isinstance(items, list):
                out.extend(items)
            else:
                # Some endpoints return a dict (e.g. workloadstatus); just bail.
                out = items
                break
            token = data.get("next_page_token") or ""
            if not token:
                break
            params["page_token"] = token
        return out

    def get_raw(self, path: str, params: Optional[dict] = None) -> Any:
        """Read-only GET of an arbitrary CML API path → the raw parsed JSON.

        Reuses the standard auth headers, retry/backoff, and error mapping of
        every other call (GET only — this never mutates). Powers the chat
        assistant's deep-dive "read the complete API response" capability when
        the curated ``list_*`` helpers don't surface a needed field. Raises
        :class:`CmlApiError` on failure.
        """
        return self._request("GET", path, params=params or None)

    # =================================================================
    # Project
    # =================================================================

    def list_project_names(self, name_filter: Optional[str] = None) -> list[str]:
        params: dict = {"page_size": 50}
        if name_filter:
            params["search_filter"] = json.dumps({"name": name_filter})
        return self._paginated(
            "/api/v2/projectnames", items_key="project_names", params=params
        )

    def list_projects(self, name_filter: Optional[str] = None) -> list[dict]:
        params: dict = {"page_size": 50}
        if name_filter:
            params["search_filter"] = json.dumps({"name": name_filter})
        return self._paginated(
            "/api/v2/projects", items_key="projects", params=params
        )

    def list_projects_page(
        self,
        *,
        name_filter: Optional[str] = None,
        page_size: int = 50,
    ) -> tuple[list[dict], bool]:
        """First page of accessible project NAMES with a ``has_more`` flag.

        Uses the ``/api/v2/projectnames`` discovery endpoint per CML
        app.md §3.1 / §10 — the documented entry point for "list projects
        the current user can see". Earlier this hit ``/api/v2/projects``
        directly, but that endpoint is documented only for the
        name-resolution step (§3.2, with ``search_filter`` mandatory) and
        on some CML workspaces the auth gateway responds with an HTML
        login page rather than JSON when called without a filter.

        Returns dicts shaped like ``{"name": <str>, "id": ""}`` so the
        existing :class:`CmlProjectOption` mapping keeps working. ``id``
        is left empty here — name→id resolution happens at save time via
        :meth:`resolve_project_id` (also documented in §3.2).
        """
        params: dict = {"page_size": max(1, min(int(page_size), 50))}
        if name_filter:
            params["search_filter"] = json.dumps({"name": name_filter})
        data = self._request("GET", "/api/v2/projectnames", params=params)
        names = data.get("project_names") or []
        if not isinstance(names, list):
            names = []
        items = [{"id": "", "name": str(n)} for n in names if n]
        has_more = bool(data.get("next_page_token"))
        return items, has_more

    def resolve_project_id(self, name: str) -> str:
        """Resolve a CML project *name* to its *project_id*.

        Uses ``GET /api/v2/projects?search_filter={"name":...}`` and requires
        an exact name match (CML's filter does substring matching, so we
        post-filter to be safe). Raises :class:`CmlApiError` if zero or
        multiple exact matches are found.
        """
        if not name:
            raise CmlApiError("project name required for resolve_project_id")
        projects = self.list_projects(name_filter=name)
        exact = [p for p in projects if p.get("name") == name]
        if not exact:
            raise CmlApiError(
                f"CML project '{name}' not found", status_code=404
            )
        if len(exact) > 1:
            ids = [p.get("id") for p in exact]
            raise CmlApiError(
                f"Multiple CML projects matched name '{name}': {ids}"
            )
        return exact[0]["id"]

    def get_project(self, project_id: str) -> dict:
        return self._request("GET", f"/api/v2/projects/{project_id}")

    # =================================================================
    # Job
    # =================================================================

    def list_jobs(
        self, project_id: str, name_filter: Optional[str] = None
    ) -> list[dict]:
        params: dict = {"page_size": 50}
        if name_filter:
            params["search_filter"] = json.dumps({"name": name_filter})
        return self._paginated(
            f"/api/v2/projects/{project_id}/jobs",
            items_key="jobs",
            params=params,
        )

    def resolve_job_id(self, project_id: str, name: str) -> str:
        """Resolve a CML job *name* (within a project) to its *job_id*."""
        if not name:
            raise CmlApiError("job name required for resolve_job_id")
        jobs = self.list_jobs(project_id, name_filter=name)
        exact = [j for j in jobs if j.get("name") == name]
        if not exact:
            raise CmlApiError(
                f"CML job '{name}' not found in project '{project_id}'",
                status_code=404,
            )
        if len(exact) > 1:
            ids = [j.get("id") for j in exact]
            raise CmlApiError(
                f"Multiple CML jobs matched name '{name}': {ids}"
            )
        return exact[0]["id"]

    def get_job(self, project_id: str, job_id: str) -> dict:
        return self._request(
            "GET", f"/api/v2/projects/{project_id}/jobs/{job_id}"
        )

    def list_job_runs(
        self,
        project_id: str,
        job_id: str,
        *,
        sort: str = "-created_at",
        status_filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """List run history for a job.

        ``sort`` defaults to ``-created_at`` (newest first), matching the
        recommendation in CML/job.md §3.4. When ``limit`` is provided the
        method does not follow pagination — useful for "I just want the
        latest N runs" polling paths.
        """
        params: dict = {"sort": sort}
        if status_filter:
            params["search_filter"] = json.dumps({"status": status_filter})
        path = f"/api/v2/projects/{project_id}/jobs/{job_id}/runs"

        if limit is not None:
            params["page_size"] = max(1, min(int(limit), 50))
            data = self._request("GET", path, params=params)
            runs = data.get("job_runs") or []
            return runs[: int(limit)]

        params["page_size"] = 50
        return self._paginated(path, items_key="job_runs", params=params)

    def list_job_runs_recent(
        self,
        project_id: str,
        job_id: str,
        *,
        bootstrap: bool = False,
        page_size: int = 50,
        max_pages: int = 5,
    ) -> list[dict]:
        """Fetch the most recent job runs, newest-first.

        Backfill counterpart to :meth:`list_job_runs`. Always pulls a fixed
        window (``max_pages`` × ``page_size``) of recent runs; the caller
        deduplicates via ``(job_id, cml_run_id)`` so reading the same run
        on consecutive ticks is harmless. The window is what makes
        historical-gap recovery work: a run missed by an earlier tick is
        re-fetched and inserted on every subsequent tick until it falls
        outside the window.

        When ``bootstrap`` is True (caller has no prior persistence for
        this Job), only the first page is returned — a fresh binding
        shouldn't trigger an ancient-history backfill on its first poll.

        At the default 5×50 = 250 runs/job/tick, an 8-job deployment costs
        at most ~40 CML calls per 5-minute tick (≈0.13 req/s averaged),
        well under any rate limit and within typical CML response times.
        """
        params: dict = {
            "sort": "-created_at",
            "page_size": max(1, min(int(page_size), 50)),
        }
        path = f"/api/v2/projects/{project_id}/jobs/{job_id}/runs"
        out: list = []

        pages = 1 if bootstrap else max(1, int(max_pages))
        for _ in range(pages):
            data = self._request("GET", path, params=params)
            runs = data.get("job_runs") or []
            if not runs:
                break
            out.extend(runs)
            token = data.get("next_page_token") or ""
            if not token:
                break
            params["page_token"] = token

        return out

    def get_job_run(
        self, project_id: str, job_id: str, run_id: str
    ) -> dict:
        return self._request(
            "GET",
            f"/api/v2/projects/{project_id}/jobs/{job_id}/runs/{run_id}",
        )

    # =================================================================
    # Application
    # =================================================================

    def list_applications(
        self,
        project_id: str,
        *,
        name_filter: Optional[str] = None,
        subdomain_filter: Optional[str] = None,
        status_filter: Optional[str] = None,
    ) -> list[dict]:
        params: dict = {"page_size": 50}
        sf: dict = {}
        if name_filter:
            sf["name"] = name_filter
        if subdomain_filter:
            sf["subdomain"] = subdomain_filter
        if status_filter:
            sf["status"] = status_filter
        if sf:
            params["search_filter"] = json.dumps(sf)
        return self._paginated(
            f"/api/v2/projects/{project_id}/applications",
            items_key="applications",
            params=params,
        )

    def resolve_application_id(
        self,
        project_id: str,
        *,
        name: Optional[str] = None,
        subdomain: Optional[str] = None,
    ) -> str:
        """Resolve a CML application by *name* and/or *subdomain*.

        Per CML/app.md §7, ``application_id`` is the primary key and
        ``subdomain`` is the secondary binding key. So we try name first
        (with a single-filter call) and, if that misses or is ambiguous,
        fall back to subdomain alone (another single-filter call). We
        deliberately avoid combining both filters in one call because CML's
        search_filter ANDs them, which means a typo in name would also
        defeat the subdomain rescue path.
        """
        if not name and not subdomain:
            raise CmlApiError(
                "name or subdomain required for resolve_application_id"
            )

        # 1) Try name as primary key.
        if name:
            apps_by_name = self.list_applications(project_id, name_filter=name)
            exact_name = [a for a in apps_by_name if a.get("name") == name]
            if len(exact_name) == 1:
                return exact_name[0]["id"]
            if len(exact_name) > 1:
                # Disambiguate with subdomain when caller supplied one.
                if subdomain:
                    both = [a for a in exact_name if a.get("subdomain") == subdomain]
                    if len(both) == 1:
                        return both[0]["id"]
                ids = [a.get("id") for a in exact_name]
                raise CmlApiError(
                    f"Multiple CML applications matched name '{name}': {ids}"
                )
            # name returned no exact hits — fall through to subdomain rescue.

        # 2) Subdomain fallback (secondary binding key).
        if subdomain:
            apps_by_sub = self.list_applications(
                project_id, subdomain_filter=subdomain
            )
            exact_sub = [
                a for a in apps_by_sub if a.get("subdomain") == subdomain
            ]
            if len(exact_sub) == 1:
                return exact_sub[0]["id"]
            if len(exact_sub) > 1:
                ids = [a.get("id") for a in exact_sub]
                raise CmlApiError(
                    f"Multiple CML applications matched subdomain "
                    f"'{subdomain}': {ids}"
                )

        raise CmlApiError(
            f"CML application not found in project '{project_id}' "
            f"(name={name!r}, subdomain={subdomain!r})",
            status_code=404,
        )

    def get_application(
        self, project_id: str, application_id: str
    ) -> dict:
        return self._request(
            "GET",
            f"/api/v2/projects/{project_id}/applications/{application_id}",
        )

    def find_application_by_subdomain(
        self,
        subdomain: str,
        *,
        exclude_project_id: Optional[str] = None,
        max_projects: int = 300,
        time_budget_seconds: float = 8.0,
    ) -> dict:
        """Workspace-wide search for an application by *exact* subdomain.

        CML v2 has no global application listing — apps are only reachable
        under ``/projects/{id}/applications`` — so we enumerate accessible
        projects and ask CML to filter each project's apps by subdomain.
        Short-circuits on the first exact match.

        Project discovery prefers the bulk ``/api/v2/projects`` listing (id +
        name in one paginated sweep → 1 app-list call per project); if that
        endpoint is unavailable on this workspace it falls back to the
        ``/projectnames`` discovery endpoint + a per-name id resolve (2 calls
        per project). Either way the walk is bounded by BOTH a project count
        cap and a wall-clock budget so an on-demand Fetch can never hang a
        worker. Best-effort: a project that fails to resolve / list is skipped.
        Never call from the monitoring loop.

        Returns a diagnostics dict::

            {"match": {...} | None,   # owning project/app when found
             "scanned": int,          # projects actually probed
             "total": int,            # accessible projects discovered
             "capped": bool,          # stopped at max_projects
             "timed_out": bool,       # stopped at the time budget
             "error": str | None}     # discovery failure (token / CML)

        ``match`` carries ``project_id / project_name / application_id /
        application_name / subdomain``.
        """
        import time

        diag: dict = {
            "match": None, "scanned": 0, "total": 0,
            "capped": False, "timed_out": False, "error": None,
        }
        sub = (subdomain or "").strip()
        if not sub:
            return diag

        # Prefer the bulk listing (id + name together → halves the call count).
        projects: list[tuple[str, str]] = []
        try:
            rows = self.list_projects()
            projects = [
                (str(r.get("id") or ""), str(r.get("name") or ""))
                for r in rows
                if r.get("id")
            ]
        except CmlApiError:
            projects = []
        if not projects:
            # Fallback: name-only discovery, resolve id lazily during the walk.
            try:
                names = self.list_project_names()
            except CmlApiError as exc:
                diag["error"] = exc.message
                return diag
            projects = [("", str(n)) for n in names if n]

        diag["total"] = len(projects)
        start = time.monotonic()
        for pid, name in projects:
            if diag["scanned"] >= max_projects:
                diag["capped"] = True
                break
            if time.monotonic() - start > time_budget_seconds:
                diag["timed_out"] = True
                break
            if not pid:
                try:
                    pid = self.resolve_project_id(name)
                except CmlApiError:
                    continue
            if exclude_project_id and str(pid) == str(exclude_project_id):
                continue
            diag["scanned"] += 1
            try:
                apps = self.list_applications(pid, subdomain_filter=sub)
            except CmlApiError:
                continue
            for a in apps:
                if a.get("subdomain") == sub:
                    diag["match"] = {
                        "project_id": str(pid),
                        "project_name": name,
                        "application_id": str(a.get("id") or ""),
                        "application_name": str(a.get("name") or ""),
                        "subdomain": sub,
                    }
                    return diag
        return diag

    # =================================================================
    # Reference / dictionary endpoints
    # =================================================================

    def list_runtimes(self, image_filter: Optional[str] = None) -> list[dict]:
        params: dict = {"page_size": 50}
        if image_filter:
            params["search_filter"] = json.dumps(
                {"image_identifier": image_filter}
            )
        return self._paginated(
            "/api/v2/runtimes", items_key="runtimes", params=params
        )

    def list_runtime_addons(self) -> list[dict]:
        return self._paginated(
            "/api/v2/runtimeaddons", items_key="runtime_addons"
        )

    # =================================================================
    # Legacy methods (deprecated)
    # =================================================================
    #
    # The two methods below targeted the pre-Phase-2 mock contract
    # (``/api/cml/jobs``) and are no longer reachable against the v2 mock or
    # against real CML. They are kept as stubs that raise CmlApiError so
    # existing callers (cml_checker, mmp_interface, product_service, jobs
    # router) — all of which already wrap ControlInterface calls in
    # ``try/except CmlApiError`` — degrade gracefully to "CML unreachable"
    # rather than crashing with AttributeError. They will be removed once
    # Phase 2 step 6 / step 9 rewire those call sites onto the new methods
    # (list_jobs + list_job_runs).

    _DEPRECATED_MSG = (
        "{name}() targets the deprecated /api/cml/jobs contract; "
        "callers must migrate to list_jobs(project_id) + "
        "list_job_runs(project_id, job_id). This stub raises so existing "
        "try/except CmlApiError blocks fall through to the unreachable path."
    )

    def get_all_job_statuses(self) -> CanonicalControlMBatchEvent:
        raise CmlApiError(
            self._DEPRECATED_MSG.format(name="get_all_job_statuses")
        )

    def get_job_status(self, job_id: str) -> CanonicalControlMJobStatus:
        raise CmlApiError(
            self._DEPRECATED_MSG.format(name="get_job_status")
        )
