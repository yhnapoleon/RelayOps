"""Phase 2 integration tests — wires the Ops service / router layer onto the
v2 contract exposed by mock_services.cml_platform.

Exercises the cml_binding_resolver helper, the rewritten /api/jobs/verification/run
endpoint, and the cml_* fields plumbed through the audit / Pydantic schema
layers. Each test asserts behaviour against the real mock state machine
(via httpx.MockTransport) so a regression in the contract surface is caught
end-to-end without needing a live backend.
"""

import sys
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from mock_services.cml_platform import (
    _init_platform_state,
    app as mock_cml,
    platform_state,
)


# ---------------------------------------------------------------------------
# httpx patch + state reset
# ---------------------------------------------------------------------------


_real_httpx_client = httpx.Client


def _mock_transport() -> httpx.MockTransport:
    tc = TestClient(mock_cml)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        q = request.url.query
        if q:
            qs = q.decode() if isinstance(q, bytes) else q
            path = f"{path}?{qs}"
        upstream = tc.request(
            request.method, path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(
            upstream.status_code,
            headers=dict(upstream.headers),
            content=upstream.content,
        )

    return httpx.MockTransport(handler)


class _PatchedClient(_real_httpx_client):
    def __init__(self, *a: Any, **kw: Any):
        kw["transport"] = _mock_transport()
        super().__init__(*a, **kw)


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    """Route every httpx.Client through the in-process mock CML."""
    monkeypatch.setattr(httpx, "Client", _PatchedClient)
    yield


@pytest.fixture(autouse=True)
def reset_mock_state():
    fresh = _init_platform_state()
    platform_state.projects = fresh.projects
    platform_state.project_name_to_id = fresh.project_name_to_id
    platform_state.jobs = fresh.jobs
    platform_state.applications = fresh.applications
    platform_state.mmp_drifts = fresh.mmp_drifts
    yield


# ---------------------------------------------------------------------------
# cml_binding_resolver
# ---------------------------------------------------------------------------


class TestCmlBindingResolver:
    def test_build_control_interface_from_config(self):
        from core.services.cml_binding_resolver import build_control_interface

        ctrl = build_control_interface()
        assert ctrl.api_key == "mock-token"
        assert ctrl.default_project_name == "relayops-demo"

    def test_resolve_project_id_happy(self):
        from core.services.cml_binding_resolver import (
            build_control_interface,
            resolve_project_id,
        )

        pid = resolve_project_id(build_control_interface(), "relayops-demo")
        assert pid == "proj-relayops-demo-0001"

    def test_resolve_project_id_missing_swallows_error(self):
        from core.services.cml_binding_resolver import (
            build_control_interface,
            resolve_project_id,
        )

        assert resolve_project_id(build_control_interface(), "no-such") is None

    def test_resolve_project_id_empty_returns_none(self):
        from core.services.cml_binding_resolver import (
            build_control_interface,
            resolve_project_id,
        )

        assert resolve_project_id(build_control_interface(), "") is None
        assert resolve_project_id(build_control_interface(), None) is None

    def test_resolve_job_id_by_name(self):
        from core.services.cml_binding_resolver import (
            build_control_interface,
            resolve_job_id,
            resolve_project_id,
        )

        ctrl = build_control_interface()
        pid = resolve_project_id(ctrl, "relayops-demo")
        jid = resolve_job_id(ctrl, pid, "hourly-data-sync")
        assert jid is not None
        assert len(jid.split("-")) == 4  # CML id format from mock

    def test_resolve_job_id_missing_returns_none(self):
        from core.services.cml_binding_resolver import (
            build_control_interface,
            resolve_job_id,
            resolve_project_id,
        )

        ctrl = build_control_interface()
        pid = resolve_project_id(ctrl, "relayops-demo")
        assert resolve_job_id(ctrl, pid, "no-such-job") is None

    def test_resolve_application_by_subdomain(self):
        from core.services.cml_binding_resolver import (
            build_control_interface,
            resolve_application_id,
            resolve_project_id,
        )

        ctrl = build_control_interface()
        pid = resolve_project_id(ctrl, "relayops-demo")
        aid = resolve_application_id(ctrl, pid, subdomain="runtimetest")
        assert aid is not None

    def test_resolve_application_by_name(self):
        from core.services.cml_binding_resolver import (
            build_control_interface,
            resolve_application_id,
            resolve_project_id,
        )

        ctrl = build_control_interface()
        pid = resolve_project_id(ctrl, "relayops-demo")
        aid = resolve_application_id(ctrl, pid, name="inventory-health-api")
        assert aid is not None

    def test_resolve_application_requires_name_or_subdomain(self):
        from core.services.cml_binding_resolver import (
            build_control_interface,
            resolve_application_id,
            resolve_project_id,
        )

        ctrl = build_control_interface()
        pid = resolve_project_id(ctrl, "relayops-demo")
        assert resolve_application_id(ctrl, pid) is None


# ---------------------------------------------------------------------------
# /api/jobs/verification/run (rewritten in Phase 2 step 9)
# ---------------------------------------------------------------------------


class _StubActor:
    user_id = 99
    role = "ADMIN"
    ad_groups: list = []


class TestRunJobVerification:
    def _run(self, **kwargs):
        import asyncio

        from api.routers.jobs import run_job_verification
        from api.schemas.job_schemas import JobVerificationRequest

        return asyncio.run(
            run_job_verification(
                body=JobVerificationRequest(**kwargs),
                current_user=_StubActor(),  # type: ignore[arg-type]
            )
        )

    def test_happy_path_returns_run_id_and_status(self):
        resp = self._run(
            cml_project_name="relayops-demo", cml_job_name="hourly-data-sync"
        )
        assert resp.ok is True
        assert resp.job_found is True
        assert resp.job_status == "completed"
        assert resp.cml_project_id == "proj-relayops-demo-0001"
        assert resp.cml_job_id is not None
        assert resp.cml_run_id is not None
        assert resp.duration_ms >= 0

    def test_missing_job_marks_404_as_unsuccessful(self):
        resp = self._run(cml_project_name="relayops-demo", cml_job_name="no-such-job")
        assert resp.ok is False
        assert resp.job_found is False
        assert "not found" in (resp.error or "").lower()

    def test_missing_project_returns_error(self):
        resp = self._run(
            cml_project_name="no-such-project", cml_job_name="hourly-data-sync"
        )
        assert resp.ok is False
        assert resp.job_found is False

    def test_falls_back_to_default_project_when_only_job_name_given(self):
        resp = self._run(cml_job_name="hourly-data-sync")
        # config.yaml's default_project_name is "relayops-demo"
        assert resp.ok is True
        assert resp.cml_project_id == "proj-relayops-demo-0001"

    def test_legacy_control_m_alias_still_works(self):
        resp = self._run(
            cml_project_name="relayops-demo", control_m_job_name="daily-etl-pipeline"
        )
        assert resp.ok is True
        assert resp.job_found is True

    def test_requires_at_least_a_job_name(self):
        resp = self._run(cml_project_name="relayops-demo")
        assert resp.ok is False
        assert "required" in (resp.error or "").lower()

    def test_run_id_changes_after_control_panel_update(self):
        first = self._run(
            cml_project_name="relayops-demo", cml_job_name="hourly-data-sync"
        )
        # Force a new ENGINE_FAILED run via the dashboard control endpoint
        TestClient(mock_cml).put(
            f"/control/assets/{first.cml_job_id}", json={"status": "failed"}
        )
        second = self._run(
            cml_project_name="relayops-demo", cml_job_name="hourly-data-sync"
        )
        assert second.cml_run_id != first.cml_run_id
        assert second.job_status == "failed"


# ---------------------------------------------------------------------------
# Pydantic schema fields (Phase 2 step 9)
# ---------------------------------------------------------------------------


class TestPydanticSchemaCmlFields:
    def test_job_create_round_trips_cml_fields(self):
        from api.schemas.job_schemas import JobCreate

        body = JobCreate(
            cml_project_name="relayops-demo",
            cml_job_name="hourly-data-sync",
        )
        assert body.cml_project_name == "relayops-demo"
        assert body.cml_job_name == "hourly-data-sync"

    def test_app_create_validates_cml_app_type(self):
        from api.schemas.app_schemas import AppCreate

        AppCreate(application_type="other", cml_app_type="fastapi")
        AppCreate(application_type="other", cml_app_type="runtime")
        AppCreate(application_type="other", cml_app_type="ray")
        AppCreate(application_type="other", cml_app_type="generic")

    def test_app_create_rejects_unknown_cml_app_type(self):
        from api.schemas.app_schemas import AppCreate

        with pytest.raises(Exception):
            AppCreate(application_type="other", cml_app_type="bogus")

    def test_audit_field_lists_include_cml_columns(self):
        from core.services.audit_service import APP_FIELDS, JOB_FIELDS

        for field in (
            "cml_project_name",
            "cml_job_name",
            "cml_project_id",
            "cml_job_id",
        ):
            assert field in JOB_FIELDS
        for field in (
            "cml_project_name",
            "cml_application_name",
            "cml_subdomain",
            "cml_app_type",
            "cml_project_id",
            "cml_application_id",
            "cml_serving_url",
        ):
            assert field in APP_FIELDS


# ---------------------------------------------------------------------------
# Controller default-project bootstrap (Phase 2 step 8)
# ---------------------------------------------------------------------------


class TestControllerBootstrap:
    def test_resolves_default_project_at_init(self):
        from core.config import get_config
        from core.controller import Controller

        ctrl = Controller(get_config())
        assert ctrl.default_cml_project_id == "proj-relayops-demo-0001"

    def test_unknown_project_does_not_crash(self):
        from core.config import get_config
        from core.controller import Controller

        base = get_config()

        class _Cfg:
            def __getattr__(self, name):
                if name == "cml_platform_default_project_name":
                    return "no-such-project"
                return getattr(base, name)

        ctrl = Controller(_Cfg())  # type: ignore[arg-type]
        assert ctrl.default_cml_project_id is None

    def test_get_default_cml_project_id_helper(self):
        from core import controller as ctrl_mod
        from core.config import get_config

        ctrl = ctrl_mod.Controller(get_config())
        ctrl_mod._instance = ctrl
        try:
            assert ctrl_mod.get_default_cml_project_id() == "proj-relayops-demo-0001"
        finally:
            ctrl_mod._instance = None
