"""Live CML/MMP gateway tools — fake platform clients, real RBAC + binding
resolution over the June-2026 seed. The platforms are never touched."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.models.entities  # noqa: F401
from core.agent import live_tools
from core.auth.jwt import CurrentUser
from core.models.database import Base
from core.models.constants import ProjectRole
from core.models.entities import Project, ProjectMember
from core.models.user import User, UserRole

from test_agent_tools import _seed


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    _seed(s)
    # Give the seeded project a CML binding so relayops_job_id resolution works.
    s.query(Project).filter(Project.id == 1).update({"cml_project_name": "ns-proj"})
    s.commit()
    yield s
    s.close()


@pytest.fixture
def admin() -> CurrentUser:
    return CurrentUser(username="boss", user_id=1, role=UserRole.ADMIN)


@pytest.fixture
def relayops_member() -> CurrentUser:
    # Global Ops member → full platform-wide live/raw read, like admin.
    return CurrentUser(username="relayops", user_id=8, role=UserRole.RELAYOPS_MEMBER)


@pytest.fixture
def regular() -> CurrentUser:
    # Plain user with no project membership → scoped to nothing by default.
    return CurrentUser(username="reg", user_id=9, role=UserRole.REGULAR_USER)


class FakeControl:
    def list_projects_page(self, *, name_filter=None, page_size=50):
        items = [{"id": "", "name": "ns-proj"}, {"id": "", "name": "other-proj"}]
        if name_filter:
            items = [i for i in items if name_filter.lower() in i["name"].lower()]
        return items, False

    def resolve_project_id(self, name):
        if name != "ns-proj":
            from core.integrations.control_interface import CmlApiError
            raise CmlApiError(f"CML project '{name}' not found", status_code=404)
        return "proj-id-1"

    def list_jobs(self, project_id, name_filter=None):
        return [{"id": "job-id-1", "name": "ns_daily", "script": "run.py",
                 "schedule": "0 2 * * *", "paused": False,
                 "cpu": 4, "memory": 32, "timeout": "0",
                 "updated_at": "2026-06-10T00:00:00Z"}]

    def resolve_job_id(self, project_id, name):
        return "job-id-1"

    def list_job_runs(self, project_id, job_id, *, sort="-created_at",
                      status_filter=None, limit=None):
        runs = [
            {"id": "r2", "status": "ENGINE_FAILED",
             "created_at": "2026-06-10T02:00:00Z",
             "finished_at": "2026-06-10T02:01:00Z",
             "failure_reason": "OOMKilled: container exceeded memory limit"},
            {"id": "r1", "status": "ENGINE_SUCCEEDED",
             "created_at": "2026-06-09T02:00:00Z",
             "finished_at": "2026-06-09T02:05:00Z", "failure_reason": None},
        ]
        if status_filter == "failed":
            runs = [r for r in runs if "FAILED" in r["status"]]
        return runs[: (limit or len(runs))]

    def list_applications(self, project_id, *, name_filter=None,
                          subdomain_filter=None, status_filter=None):
        return [{"id": "app-id-1", "name": "ns-api", "subdomain": "nsapi",
                 "status": "APPLICATION_RUNNING", "script": "app.py",
                 "cpu": 2, "memory": 8, "nvidia_gpu": 0,
                 "updated_at": "2026-06-10T00:00:00Z"}]

    def get_raw(self, path, params=None):
        from core.integrations.control_interface import CmlApiError
        if path == "/api/v2/boom":
            raise CmlApiError("CML API GET returned HTTP 500", status_code=500)
        if path == "/api/v2/big":
            return {"items": ["x" * 2000 for _ in range(100)]}
        return {"path_echo": path, "ok": True}


class FakeMmp:
    def is_configured(self):
        return True

    def list_projects_shallow(self):
        return {
            "demo-ns": {"id": 7, "business_name": "Inventory Scoring",
                         "models": [{"id": 1, "name": "ns-model", "is_production": True}]},
            "demo-wealth": {"id": 8, "business_name": "Wealth",
                             "models": [{"id": 2, "name": "w-model", "is_production": False}]},
        }

    def get_project(self, project_id):
        assert project_id == 7
        return {
            "business_understanding_project_name": "Inventory Scoring",
            "models": [{
                "id": 1,
                "model_name": "ns-model", "is_production": True,
                "attention_required": {
                    "model_drifted": {"status": True, "description": "fmean drift"},
                    "run_pending_approval": {"status": False, "description": ""},
                },
                "runs": [
                    {"id": 101, "is_production_run": True, "date_created": "2026-06-01T00:00:00",
                     "is_deployed": True, "drifted": True, "pmetric_drifted": False,
                     "fmean_drifted": True, "fmissing_drifted": False,
                     "has_fairness_risk": False, "approval_status": 2},
                    {"id": 100, "is_production_run": True, "date_created": "2026-01-01T00:00:00",
                     "is_deployed": False, "drifted": False, "approval_status": 2},
                    {"id": 99, "is_production_run": False, "date_created": "2026-06-09T00:00:00",
                     "approval_status": 0},
                ],
            }],
        }

    def get_raw(self, path):
        if path == "/api/boom":
            from core.integrations.mmp_interface import MmpApiError
            raise MmpApiError("MMP returned 500", status_code=500)
        return {"path_echo": path, "projects": []}

    def get_model_raw(self, repo_name, model_name):
        directory = self.list_projects_shallow()
        info = directory.get(repo_name)
        if info is None:
            raise LookupError(f"project {repo_name!r} not found")
        model_id = next((m["id"] for m in info["models"] if m["name"] == model_name), None)
        if model_id is None:
            raise LookupError(f"model {model_name!r} not found")
        project = self.get_project(info["id"])
        return next(m for m in project["models"] if m.get("id") == model_id)


@pytest.fixture(autouse=True)
def fake_platforms(monkeypatch):
    monkeypatch.setattr(live_tools, "_control", lambda: FakeControl())
    monkeypatch.setattr(live_tools, "_mmp", lambda: FakeMmp())


# ── CML ───────────────────────────────────────────────────────────────


def test_live_projects_mark_relayops_binding(session, admin):
    out = live_tools.cml_live_projects(session, admin)
    by_name = {r["cml_project_name"]: r for r in out["rows"]}
    assert by_name["ns-proj"]["relayops_project_id"] == 1
    assert by_name["other-proj"]["relayops_project_id"] is None


def test_live_jobs_mark_relayops_binding(session, admin):
    out = live_tools.cml_live_jobs(session, admin, cml_project_name="ns-proj")
    row = out["rows"][0]
    assert row["schedule"] == "0 2 * * *"
    assert row["relayops_job_id"] == 1


def test_live_job_runs_via_relayops_binding_carries_failure_reason(session, admin):
    out = live_tools.cml_live_job_runs(session, admin, relayops_job_id=1)
    assert out["cml_project_name"] == "ns-proj"      # resolved via parent project
    assert out["rows"][0]["failure_reason"].startswith("OOMKilled")


def test_live_job_runs_status_filter_and_validation(session, admin):
    out = live_tools.cml_live_job_runs(
        session, admin, cml_project_name="ns-proj", cml_job_name="ns_daily",
        status="failed")
    assert [r["status"] for r in out["rows"]] == ["ENGINE_FAILED"]
    bad = live_tools.cml_live_job_runs(
        session, admin, cml_project_name="ns-proj", cml_job_name="ns_daily",
        status="exploded")
    assert "valid_values" in bad


def test_live_job_runs_rbac_blocks_invisible_job(session):
    session.add(User(id=9, username="outsider", role=UserRole.REGULAR_USER))
    session.commit()
    outsider = CurrentUser(username="outsider", user_id=9, role=UserRole.REGULAR_USER)
    out = live_tools.cml_live_job_runs(session, outsider, relayops_job_id=1)
    assert "error" in out


def test_live_job_runs_unknown_cml_project_degrades(session, admin):
    out = live_tools.cml_live_job_runs(
        session, admin, cml_project_name="ghost", cml_job_name="x")
    assert "CML" in out["error"]


def test_live_apps_via_relayops_binding(session, admin):
    from core.models.entities import Application

    session.add(Application(id=1, product_id=1, cml_application_name="ns-api"))
    session.commit()
    out = live_tools.cml_live_apps(session, admin, relayops_app_id=1)
    assert out["rows"][0]["status"] == "APPLICATION_RUNNING"


# ── MMP ───────────────────────────────────────────────────────────────


def test_mmp_live_projects_filter_and_binding(session, admin):
    session.query(Project).filter(Project.id == 1).update({"mmp_project_id": "demo-ns"})
    session.commit()
    out = live_tools.mmp_live_projects(session, admin, q="inventory")
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["repo_name"] == "demo-ns"
    assert row["relayops_project_id"] == 1
    assert row["production_models"] == ["ns-model"]


def test_mmp_live_project_status_flags_and_latest_run(session, admin):
    out = live_tools.mmp_live_project_status(session, admin, repo_name="demo-ns")
    model = out["models"][0]
    flags = {a["flag"] for a in model["attention_required"]}
    assert flags == {"model_drifted"}
    latest = model["latest_production_run"]
    assert latest["fmean_drifted"] is True and latest["is_deployed"] is True


def test_mmp_live_project_status_unknown_repo(session, admin):
    out = live_tools.mmp_live_project_status(session, admin, repo_name="nope")
    assert "不存在" in out["error"]


def test_mmp_live_model_raw_annotates_status_and_picks_latest(session, admin):
    out = live_tools.mmp_live_model_raw(
        session, admin, repo_name="demo-ns", model_name="ns-model")
    # Latest production run is the 2026-06-01 one (run 101), not the non-prod run.
    assert out["latest_production_run"]["id"] == 101
    assert out["latest_production_run"]["approval_status"] == 2
    assert "Approved" in out["latest_production_run"]["approval_status_label"]
    # Only production runs counted; non-production run excluded.
    assert out["total_production_runs"] == 2
    assert {r["id"] for r in out["recent_production_runs"]} == {100, 101}
    # Raw attention block passed through verbatim.
    assert out["attention_required"]["model_drifted"]["status"] is True
    # Legend covers the terminal codes.
    assert 1 in out["approval_status_legend"] and 2 in out["approval_status_legend"]


def test_mmp_live_model_raw_unknown_model_degrades(session, admin):
    out = live_tools.mmp_live_model_raw(
        session, admin, repo_name="demo-ns", model_name="ghost-model")
    assert "error" in out and "hint" in out


def test_mmp_live_model_raw_traces_specific_run(session, admin):
    # Trace the older run (100) by id — focus_run resolves to it, not the latest.
    out = live_tools.mmp_live_model_raw(
        session, admin, repo_name="demo-ns", model_name="ns-model", run_id=100)
    assert out["focus_run"]["id"] == 100
    assert out["focus_run_found"] is True
    assert out["focus_run_is_latest"] is False
    # The current latest is still surfaced so the UI can show state moved on.
    assert out["latest_production_run"]["id"] == 101


def test_mmp_live_model_raw_focus_falls_back_when_run_missing(session, admin):
    out = live_tools.mmp_live_model_raw(
        session, admin, repo_name="demo-ns", model_name="ns-model", run_id=999999)
    assert out["focus_run_found"] is False
    # Falls back to the latest production run.
    assert out["focus_run"]["id"] == 101
    assert out["focus_run_is_latest"] is True


# ── Raw API passthrough ───────────────────────────────────────────────


def test_cml_get_raw_returns_complete_body(session, admin):
    out = live_tools.cml_get_raw(session, admin, path="/api/v2/projects/7")
    assert out["data"] == {"path_echo": "/api/v2/projects/7", "ok": True}
    assert "note" in out


def test_mmp_get_raw_returns_complete_body(session, admin):
    out = live_tools.mmp_get_raw(session, admin, path="/api/projects/189")
    assert out["data"]["path_echo"] == "/api/projects/189"


@pytest.mark.parametrize("bad", ["", "projects", "https://untrusted.example.com/x", "/a/../b"])
def test_raw_path_validation_rejects(session, admin, bad):
    assert "error" in live_tools.cml_get_raw(session, admin, path=bad)
    assert "error" in live_tools.mmp_get_raw(session, admin, path=bad)


def test_cml_get_raw_truncates_oversized_body(session, admin):
    out = live_tools.cml_get_raw(session, admin, path="/api/v2/big")
    assert out["truncated"] is True
    assert out["size_chars"] > len(out["preview"])
    assert "data" not in out  # oversized → preview, not full body


def test_cml_get_raw_degrades_on_api_error(session, admin):
    out = live_tools.cml_get_raw(session, admin, path="/api/v2/boom")
    assert "error" in out and "CML" in out["error"]


def test_mmp_get_raw_degrades_on_api_error(session, admin):
    out = live_tools.mmp_get_raw(session, admin, path="/api/boom")
    assert "error" in out and "MMP" in out["error"]


# ── Access guardrail (platform-wide read = Ops member / admin only) ────


def _bind_mmp(session):
    session.query(Project).filter(Project.id == 1).update({"mmp_project_id": "demo-ns"})
    session.commit()


def test_regular_user_denied_cml_live_jobs(session, regular):
    out = live_tools.cml_live_jobs(session, regular, cml_project_name="ns-proj")
    assert "无权限" in out["error"]


def test_regular_user_denied_cml_live_job_runs_name_path(session, regular):
    out = live_tools.cml_live_job_runs(
        session, regular, cml_project_name="ns-proj", cml_job_name="ns_daily")
    assert "无权限" in out["error"]


def test_regular_user_denied_mmp_status(session, regular):
    _bind_mmp(session)
    out = live_tools.mmp_live_project_status(session, regular, repo_name="demo-ns")
    assert "无权限" in out["error"]


def test_regular_user_denied_mmp_model_raw(session, regular):
    _bind_mmp(session)
    out = live_tools.mmp_live_model_raw(
        session, regular, repo_name="demo-ns", model_name="ns-model")
    assert "无权限" in out["error"]


def test_regular_user_denied_raw_passthrough(session, regular):
    assert "无权限" in live_tools.cml_get_raw(session, regular, path="/api/v2/projects")["error"]
    assert "无权限" in live_tools.mmp_get_raw(session, regular, path="/api/projects")["error"]


def test_regular_user_cml_live_projects_scoped_to_empty(session, regular):
    # No membership → directory shows nothing (not the whole platform).
    out = live_tools.cml_live_projects(session, regular)
    assert out["rows"] == []


def test_regular_user_member_can_read_own_project(session, regular):
    # Granted read access to project 1 → can query its CML jobs.
    session.add(ProjectMember(project_id=1, user_id=9, role=ProjectRole.RELAYOPS_MEMBER, added_by=1))
    session.commit()
    out = live_tools.cml_live_jobs(session, regular, cml_project_name="ns-proj")
    assert "error" not in out
    assert out["rows"][0]["relayops_job_id"] == 1


def test_relayops_member_has_full_platform_read(session, relayops_member):
    _bind_mmp(session)
    # Directory browse, project status, and raw passthrough all work.
    assert "error" not in live_tools.cml_live_jobs(
        session, relayops_member, cml_project_name="ns-proj")
    assert "error" not in live_tools.mmp_live_project_status(
        session, relayops_member, repo_name="demo-ns")
    assert "error" not in live_tools.cml_get_raw(
        session, relayops_member, path="/api/v2/projects/7")
