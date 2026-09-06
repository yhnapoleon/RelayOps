"""Unit tests for ControlInterface.find_application_by_subdomain.

The method composes already-tested calls (list_project_names /
resolve_project_id / list_applications); here we stub those three so the
scan/exclude/short-circuit/cap logic is exercised without real HTTP.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

from core.integrations.control_interface import CmlApiError, ControlInterface


def _make(projects: dict[str, list[dict]], *, names_error: bool = False):
    """Build a ControlInterface with the three CML calls stubbed.

    ``projects`` maps project_name -> list of application dicts. project_id is
    derived as ``id-<name>`` so exclusion can be asserted.
    """
    ci = ControlInterface(base_url="http://stub")
    calls = {"list_apps": []}

    def list_projects(name_filter=None):
        # Force the /projectnames + per-name resolve fallback path so the
        # tests exercise resolve_project_id ordering deterministically.
        raise CmlApiError("bulk listing unavailable on this workspace")

    def list_project_names(name_filter=None):
        if names_error:
            raise CmlApiError("boom")
        return list(projects.keys())

    def resolve_project_id(name):
        if name not in projects:
            raise CmlApiError("not found", status_code=404)
        return f"id-{name}"

    def list_applications(project_id, *, name_filter=None, subdomain_filter=None, status_filter=None):
        calls["list_apps"].append(project_id)
        name = project_id.removeprefix("id-")
        apps = projects.get(name, [])
        if subdomain_filter:
            return [a for a in apps if subdomain_filter in (a.get("subdomain") or "")]
        return apps

    ci.list_projects = list_projects  # type: ignore[assignment]
    ci.list_project_names = list_project_names  # type: ignore[assignment]
    ci.resolve_project_id = resolve_project_id  # type: ignore[assignment]
    ci.list_applications = list_applications  # type: ignore[assignment]
    return ci, calls


def test_finds_subdomain_in_other_project():
    ci, _ = _make({
        "material-classifier": [{"id": "a1", "name": "batch", "subdomain": "batch-ns"}],
        "inventory-scoring": [{"id": "a2", "name": "ns-api", "subdomain": "dynamic-inventory-scoring-prod"}],
    })
    diag = ci.find_application_by_subdomain("dynamic-inventory-scoring-prod")
    assert diag["match"] == {
        "project_id": "id-inventory-scoring",
        "project_name": "inventory-scoring",
        "application_id": "a2",
        "application_name": "ns-api",
        "subdomain": "dynamic-inventory-scoring-prod",
    }


def test_excludes_bound_project():
    # Same subdomain present only in the excluded project -> no match.
    ci, _ = _make({
        "bound": [{"id": "a1", "name": "app", "subdomain": "sub-x"}],
    })
    diag = ci.find_application_by_subdomain("sub-x", exclude_project_id="id-bound")
    assert diag["match"] is None


def test_short_circuits_on_first_match():
    ci, calls = _make({
        "p1": [{"id": "a1", "name": "x", "subdomain": "want"}],
        "p2": [{"id": "a2", "name": "y", "subdomain": "want"}],
    })
    ci.find_application_by_subdomain("want")
    # Stops after the first project that matches (p1), never lists p2's apps.
    assert calls["list_apps"] == ["id-p1"]


def test_cap_limits_scan():
    projects = {f"p{i}": [{"id": f"a{i}", "name": "n", "subdomain": "none"}] for i in range(10)}
    ci, calls = _make(projects)
    diag = ci.find_application_by_subdomain("missing", max_projects=3)
    assert diag["match"] is None
    assert diag["capped"] is True
    assert len(calls["list_apps"]) == 3


def test_blank_subdomain_returns_no_match():
    ci, _ = _make({"p": []})
    assert ci.find_application_by_subdomain("  ")["match"] is None


def test_discovery_error_degrades_gracefully():
    ci, _ = _make({"p": []}, names_error=True)
    diag = ci.find_application_by_subdomain("sub")
    assert diag["match"] is None
    assert diag["error"]
