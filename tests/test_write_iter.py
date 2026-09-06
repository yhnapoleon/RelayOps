"""S5 — iterate write tools: descriptive edits + member role (propose + commit).

Covers the phase's correctness properties without a real LLM/HTTP:
  * P11 field whitelist — only descriptive fields enter a proposal; owner/status/
    is_system are dropped even when proposed;
  * member-role limits — only product_member ↔ relayops_member, never business_owner;
  * P2 read-only preview — a draft tool mutates nothing;
  * commit dispatch maps each proposal to the right deterministic service write.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.models.entities  # noqa: F401 — register tables
from core.auth.jwt import CurrentUser
from core.models.database import Base
from core.models.entities import Product, Project, ProjectMember
from core.models.user import UserRole

NOW = datetime(2026, 6, 12, 9, 0)
OWNER_ID = 99


def _actor(role=UserRole.RELAYOPS_MEMBER, uid=42, username="dora"):
    return CurrentUser(username=username, user_id=uid, role=role)


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine)
    s = f()
    s.add(Project(id=1, name="Risk", description="old desc", owner_id=OWNER_ID,
                  prod_stat_url="", is_system=0))
    s.add(Product(id=1, project_id=1, name="Scoring"))
    s.add(ProjectMember(id=1, project_id=1, user_id=7, role="relayops_member", added_by=OWNER_ID))
    s.add(ProjectMember(id=2, project_id=1, user_id=OWNER_ID, role="business_owner", added_by=OWNER_ID))
    s.commit()
    s.close()
    return f


# ── S5.2 draft_edit_project: whitelist (P11) ─────────────────────────────


def test_edit_project_whitelist_keeps_only_descriptive_fields(factory):
    from core.agent.write_tools import draft_edit_project
    s = factory()
    # Propose a descriptive change PLUS sensitive keys — the latter must vanish.
    out = draft_edit_project(s, _actor(), project_id=1, fields={
        "description": "new desc", "owner_id": 1, "status": "archived",
        "is_system": 1, "cml_project_id": "x",
    })
    assert out["kind"] == "edit_project" and out["commit_path"] == "direct"
    paths = {c["field_path"] for c in out["changes"]}
    assert paths == {"description"}  # P11: sensitive fields dropped
    assert s.get(Project, 1).description == "old desc"  # P2 read-only
    s.close()


def test_edit_project_no_change_returns_note(factory):
    from core.agent.write_tools import draft_edit_project
    s = factory()
    out = draft_edit_project(s, _actor(), project_id=1, fields={"description": "old desc"})
    assert "note" in out and "kind" not in out
    s.close()


def test_edit_project_rbac_denies_stranger(factory):
    from core.agent.write_tools import draft_edit_project
    s = factory()
    stranger = _actor(role=UserRole.REGULAR_USER, uid=7, username="bob")
    out = draft_edit_project(s, stranger, project_id=1, fields={"description": "x"})
    assert out.get("rbac_ok") is False
    s.close()


def test_edit_project_owner_allowed(factory):
    from core.agent.write_tools import draft_edit_project
    s = factory()
    owner = _actor(role=UserRole.REGULAR_USER, uid=OWNER_ID, username="boss")
    out = draft_edit_project(s, owner, project_id=1, fields={"name": "Risk2"})
    assert out["kind"] == "edit_project"
    s.close()


# ── S5.2 draft_edit_product: version flow ────────────────────────────────


def test_edit_product_proposes_version_flow(factory):
    from core.agent.write_tools import draft_edit_product
    s = factory()
    out = draft_edit_product(s, _actor(), product_id=1, name="Scoring v2")
    assert out["kind"] == "edit_product" and out["commit_path"] == "version_flow"
    assert out["changes"][0]["new_value"] == "Scoring v2"
    assert s.get(Product, 1).name == "Scoring"  # P2 read-only
    s.close()


# ── S5.1 draft_set_member_role: role limits (P11) ────────────────────────


def test_set_member_role_swaps_within_allowed(factory):
    from core.agent.write_tools import draft_set_member_role
    s = factory()
    out = draft_set_member_role(s, _actor(role=UserRole.ADMIN), project_id=1,
                                user_id=7, new_role="product_member")
    assert out["kind"] == "set_member_role" and out["entity_type"] == "project"
    assert out["changes"][0]["field_path"] == "members.7.role"
    assert out["changes"][0]["new_value"] == "product_member"
    s.close()


def test_set_member_role_rejects_business_owner(factory):
    from core.agent.write_tools import draft_set_member_role
    s = factory()
    out = draft_set_member_role(s, _actor(role=UserRole.ADMIN), project_id=1,
                                user_id=OWNER_ID, new_role="product_member")
    assert "error" in out and "business owner" in out["error"].lower()
    s.close()


def test_set_member_role_rejects_unknown_role(factory):
    from core.agent.write_tools import draft_set_member_role
    s = factory()
    out = draft_set_member_role(s, _actor(role=UserRole.ADMIN), project_id=1,
                                user_id=7, new_role="admin")
    assert "error" in out and out["valid_values"] == ["product_member", "relayops_member"]
    s.close()


def test_set_member_role_not_elevated_for_relayops_member(factory):
    # Member management is NOT covered by the relayops_member elevation (Phase R):
    # a relayops_member who is neither admin nor owner nor self is denied (P4).
    from core.agent.write_tools import draft_set_member_role
    s = factory()
    out = draft_set_member_role(s, _actor(role=UserRole.RELAYOPS_MEMBER, uid=42), project_id=1,
                                user_id=7, new_role="product_member")
    assert out.get("rbac_ok") is False
    s.close()


# ── tool pool stays T1-only with the new tools registered (P10/P14) ──────


def test_new_tools_are_all_t1_no_high_risk():
    from core.agent.write_tools import build_write_tools
    names = {t.name for t in build_write_tools(_actor(), proposal_sink=[])}
    assert {"draft_edit_project_tool", "draft_edit_product_tool",
            "draft_set_member_role_tool"} <= names
    forbidden = ("owner", "transfer", "delete", "approve", "reject", "create",
                 "local_account", "global")
    assert not any(any(f in n for f in forbidden) for n in names)


# ── commit dispatch ──────────────────────────────────────────────────────


def test_commit_edit_project_calls_service(monkeypatch):
    from api.routers import agent_actions
    from core.agent.write_schemas import FieldChange, WriteProposal

    captured = {}

    class _Resp:
        status = "ok"

        class project:
            id = 1

    def _fake_update(db, **kwargs):
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr("core.services.project_service.update_project", _fake_update)
    monkeypatch.setattr("core.models.database.get_db", lambda: object())
    proposal = WriteProposal(
        kind="edit_project", entity_type="project", entity_id=1, title="x",
        changes=[FieldChange(field_path="description", label="描述",
                             old_value="old", new_value="new")])
    out = agent_actions._commit_proposal(proposal, _actor(), session=None)
    assert out == {"entity": "project", "project_id": 1}
    assert captured["description"] == "new" and captured["name"] is None
    # relayops_member is elevated for asset edits (Phase R).
    assert captured["is_admin"] is True


def test_commit_set_member_role_mutates(factory):
    from api.routers import agent_actions
    from core.agent.write_schemas import FieldChange, WriteProposal

    s = factory()
    proposal = WriteProposal(
        kind="set_member_role", entity_type="project", entity_id=1, title="x",
        changes=[FieldChange(field_path="members.7.role", label="项目角色",
                             old_value="relayops_member", new_value="product_member")])
    out = agent_actions._commit_set_member_role(proposal, _actor(role=UserRole.ADMIN), s)
    assert out["new_role"] == "product_member"
    assert s.get(ProjectMember, 1).role == "product_member"
    s.close()
