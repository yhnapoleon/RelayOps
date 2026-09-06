"""Tests for the Mock CML Platform v2 contract.

Covers:
  * Bearer auth on /api/v2/* endpoints
  * Project / job / job-run / application discovery and detail
  * Mock-only control-panel endpoints (asset status flipping)
"""

import sys
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from mock_services.cml_platform import (
    _init_platform_state,
    app,
    platform_state,
)


AUTH_HEADERS = {"Authorization": "Bearer mock-token"}


def _project_id() -> str:
    return next(iter(platform_state.projects.values())).id


def _project_name() -> str:
    return next(iter(platform_state.projects.values())).name


def _first_job_id() -> str:
    return next(iter(platform_state.jobs.values())).id


def _first_app_id() -> str:
    return next(iter(platform_state.applications.values())).id


@pytest.fixture(autouse=True)
def reset_state():
    fresh = _init_platform_state()
    platform_state.projects = fresh.projects
    platform_state.project_name_to_id = fresh.project_name_to_id
    platform_state.jobs = fresh.jobs
    platform_state.applications = fresh.applications
    yield


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# v2 auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_v2_endpoint_without_bearer_returns_401(self, client: TestClient):
        resp = client.get("/api/v2/projects")
        assert resp.status_code == 401

    def test_v2_endpoint_with_blank_bearer_returns_401(self, client: TestClient):
        resp = client.get(
            "/api/v2/projects", headers={"Authorization": "Bearer "}
        )
        assert resp.status_code == 401

    def test_v2_endpoint_with_non_bearer_scheme_returns_401(self, client: TestClient):
        resp = client.get(
            "/api/v2/projects", headers={"Authorization": "Basic abc"}
        )
        assert resp.status_code == 401

    def test_mock_only_paths_do_not_require_auth(self, client: TestClient):
        # Dashboard control endpoints stay open.
        assert client.get("/control/status").status_code == 200


# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------


class TestProjects:
    def test_projectnames_lists_seeded_project(self, client: TestClient):
        resp = client.get("/api/v2/projectnames", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert _project_name() in data["project_names"]
        assert data["next_page_token"] == ""

    def test_projects_search_by_name_returns_id(self, client: TestClient):
        resp = client.get(
            "/api/v2/projects",
            headers=AUTH_HEADERS,
            params={"search_filter": f'{{"name":"{_project_name()}"}}'},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["projects"]) == 1
        assert body["projects"][0]["id"] == _project_id()
        assert body["projects"][0]["permissions"]["read"] is True

    def test_invalid_search_filter_returns_400(self, client: TestClient):
        resp = client.get(
            "/api/v2/projects",
            headers=AUTH_HEADERS,
            params={"search_filter": "not-json"},
        )
        assert resp.status_code == 400

    def test_project_detail(self, client: TestClient):
        resp = client.get(
            f"/api/v2/projects/{_project_id()}", headers=AUTH_HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == _project_id()
        assert "permissions" in body
        assert "default_engine_type" in body

    def test_project_detail_unknown_returns_404(self, client: TestClient):
        resp = client.get("/api/v2/projects/no-such-project", headers=AUTH_HEADERS)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Jobs / runs
# ---------------------------------------------------------------------------


class TestJobs:
    def test_list_jobs_returns_seeded_jobs(self, client: TestClient):
        resp = client.get(
            f"/api/v2/projects/{_project_id()}/jobs", headers=AUTH_HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        names = {j["name"] for j in body["jobs"]}
        assert names == {
            "hourly-data-sync",
            "daily-etl-pipeline",
            "daily-model-training",
            "weekly-model-retrain",
        }

    def test_list_jobs_search_filter_by_name(self, client: TestClient):
        resp = client.get(
            f"/api/v2/projects/{_project_id()}/jobs",
            headers=AUTH_HEADERS,
            params={"search_filter": '{"name":"hourly"}'},
        )
        assert resp.status_code == 200
        names = {j["name"] for j in resp.json()["jobs"]}
        assert names == {"hourly-data-sync"}

    def test_get_job_detail(self, client: TestClient):
        job_id = _first_job_id()
        resp = client.get(
            f"/api/v2/projects/{_project_id()}/jobs/{job_id}", headers=AUTH_HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == job_id
        assert "runtime_identifier" in body
        assert "paused" in body

    def test_get_job_unknown_returns_404(self, client: TestClient):
        resp = client.get(
            f"/api/v2/projects/{_project_id()}/jobs/missing", headers=AUTH_HEADERS
        )
        assert resp.status_code == 404

    def test_list_runs_returns_seeded_run(self, client: TestClient):
        job_id = _first_job_id()
        resp = client.get(
            f"/api/v2/projects/{_project_id()}/jobs/{job_id}/runs",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["job_runs"]) >= 1
        run = body["job_runs"][0]
        assert run["job_id"] == job_id
        assert run["status"].startswith("ENGINE_")

    def test_list_runs_status_filter(self, client: TestClient):
        # Use the control panel to append a failed run, then filter for it.
        job_id = _first_job_id()
        client.put(f"/control/assets/{job_id}", json={"status": "failed"})
        resp = client.get(
            f"/api/v2/projects/{_project_id()}/jobs/{job_id}/runs",
            headers=AUTH_HEADERS,
            params={"search_filter": '{"status":"failed"}'},
        )
        assert resp.status_code == 200
        statuses = {r["status"] for r in resp.json()["job_runs"]}
        assert statuses == {"ENGINE_FAILED"}

    def test_get_run_detail(self, client: TestClient):
        job_id = _first_job_id()
        runs_resp = client.get(
            f"/api/v2/projects/{_project_id()}/jobs/{job_id}/runs",
            headers=AUTH_HEADERS,
        )
        run_id = runs_resp.json()["job_runs"][0]["id"]
        resp = client.get(
            f"/api/v2/projects/{_project_id()}/jobs/{job_id}/runs/{run_id}",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == run_id


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------


class TestApplications:
    def test_list_applications(self, client: TestClient):
        resp = client.get(
            f"/api/v2/projects/{_project_id()}/applications", headers=AUTH_HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["applications"]) == 3
        for a in body["applications"]:
            assert a["status"] == "APPLICATION_RUNNING"
            assert "subdomain" in a

    def test_list_applications_subdomain_filter(self, client: TestClient):
        resp = client.get(
            f"/api/v2/projects/{_project_id()}/applications",
            headers=AUTH_HEADERS,
            params={"search_filter": '{"subdomain":"runtimetest"}'},
        )
        assert resp.status_code == 200
        names = {a["name"] for a in resp.json()["applications"]}
        assert names == {"feature-store-cluster"}

    def test_list_applications_status_filter(self, client: TestClient):
        resp = client.get(
            f"/api/v2/projects/{_project_id()}/applications",
            headers=AUTH_HEADERS,
            params={"search_filter": '{"status":"running"}'},
        )
        assert resp.status_code == 200
        assert len(resp.json()["applications"]) == 3

    def test_get_application_detail(self, client: TestClient):
        app_id = _first_app_id()
        resp = client.get(
            f"/api/v2/projects/{_project_id()}/applications/{app_id}",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == app_id
        assert body["status"] == "APPLICATION_RUNNING"
        assert "running_at" in body


# ---------------------------------------------------------------------------
# Runtimes / dictionaries
# ---------------------------------------------------------------------------


class TestRuntimesAndDicts:
    def test_runtimes(self, client: TestClient):
        resp = client.get("/api/v2/runtimes", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["runtimes"]

    def test_runtime_addons(self, client: TestClient):
        resp = client.get("/api/v2/runtimeaddons", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["runtime_addons"]

    def test_workload_status(self, client: TestClient):
        resp = client.get("/api/v2/workloadstatus", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        statuses = resp.json()["workload_status"]
        assert "ENGINE_SUCCEEDED" in statuses
        assert "APPLICATION_RUNNING" in statuses

    def test_workload_types(self, client: TestClient):
        resp = client.get("/api/v2/workloadtypes", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert "application" in resp.json()["workload_type"]


# ---------------------------------------------------------------------------
# Control panel
# ---------------------------------------------------------------------------


class TestControlPanel:
    def test_status_lists_all_jobs_and_apps(self, client: TestClient):
        resp = client.get("/control/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 7
        types = {a["type"] for a in body["assets"]}
        assert types == {"job", "app"}

    def test_set_job_status_appends_run_visible_via_v2(self, client: TestClient):
        job_id = _first_job_id()
        before = client.get(
            f"/api/v2/projects/{_project_id()}/jobs/{job_id}/runs",
            headers=AUTH_HEADERS,
        ).json()["job_runs"]

        resp = client.put(f"/control/assets/{job_id}", json={"status": "failed"})
        assert resp.status_code == 200
        assert resp.json()["next_run_status"] == "ENGINE_FAILED"

        after = client.get(
            f"/api/v2/projects/{_project_id()}/jobs/{job_id}/runs",
            headers=AUTH_HEADERS,
        ).json()["job_runs"]
        assert len(after) == len(before) + 1
        assert any(r["status"] == "ENGINE_FAILED" for r in after)

    @patch("httpx.AsyncClient.put", new_callable=AsyncMock)
    def test_toggle_app_proxies_to_serving_url(self, mock_put, client: TestClient):
        mock_put.return_value = AsyncMock(
            status_code=200, raise_for_status=lambda: None
        )
        app_id = _first_app_id()
        resp = client.put(f"/control/assets/{app_id}", json={"healthy": False})
        assert resp.status_code == 200
        body = resp.json()
        assert body["healthy"] is False
        assert body["cml_status"] == "APPLICATION_FAILED"

    def test_toggle_app_updates_v2_status_even_if_proxy_fails(self, client: TestClient):
        app_id = _first_app_id()
        resp = client.put(f"/control/assets/{app_id}", json={"healthy": False})
        assert resp.status_code == 200

        detail = client.get(
            f"/api/v2/projects/{_project_id()}/applications/{app_id}",
            headers=AUTH_HEADERS,
        ).json()
        assert detail["status"] == "APPLICATION_FAILED"

    def test_unknown_asset_returns_404(self, client: TestClient):
        resp = client.put("/control/assets/nope", json={"status": "failed"})
        assert resp.status_code == 404
