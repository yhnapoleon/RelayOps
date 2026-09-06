"""Phase R — relayops_member platform elevation.

A global ``relayops_member`` is a small, trusted, high-privilege population and
should reach project assets like ``admin`` (see-all + act-as-owner), EXCEPT
platform-admin-only powers (T3 / admin panel): issue reassignment,
handover/version approval, account management, global templates, system-locked projects.

These tests pin:
  * the pure role helper ``is_elevated_role`` (P13 core);
  * the service access gates grant relayops_member like admin and still deny a
    plain regular_user (no regression);
  * T3 stays closed to relayops_member (``AdminOnly`` dependency).
"""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.models.entities  # noqa: F401 — register every table on Base
from api.deps.rbac import require_role
from core.auth.jwt import CurrentUser
from core.exceptions import ForbiddenError
from core.models.database import Base
from core.models.entities import Product, Project
from core.models.user import UserRole, is_elevated_role
from core.services import app_service, job_service, product_service


def _actor(role: str, uid: int = 99, username: str = "elev") -> CurrentUser:
    return CurrentUser(username=username, user_id=uid, role=role)


# ── P13 core: the pure role helper ──────────────────────────────────────


@pytest.mark.parametrize(
    "role,expected",
    [
        (UserRole.ADMIN, True),
        (UserRole.RELAYOPS_MEMBER, True),
        (UserRole.REGULAR_USER, False),
        ("business_owner", False),  # legacy alias → normalizes to regular_user
        (None, False),
        ("", False),
    ],
)
def test_is_elevated_role(role, expected):
    assert is_elevated_role(role) is expected


# ── Service access gates: relayops_member == admin, regular_user denied ──────


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    # Project owned by user 1; the elevation actors below are NOT owner/member.
    s.add(Project(id=1, name="P", owner_id=1))
    s.add(Product(id=1, project_id=1, name="Prod"))
    s.commit()
    return s


@pytest.mark.parametrize(
    "role,granted",
    [
        (UserRole.ADMIN, True),
        (UserRole.RELAYOPS_MEMBER, True),
        (UserRole.REGULAR_USER, False),
    ],
)
def test_check_product_access_elevation(session, role, granted):
    """app/job _check_product_access grant relayops_member like admin (require_owner)."""
    actor = _actor(role)
    if granted:
        assert (
            app_service._check_product_access(
                session, product_id=1, actor=actor, require_owner=True
            ).id
            == 1
        )
        assert (
            job_service._check_product_access(
                session, product_id=1, actor=actor, require_owner=True
            ).id
            == 1
        )
    else:
        with pytest.raises(ForbiddenError):
            app_service._check_product_access(
                session, product_id=1, actor=actor, require_owner=True
            )
        with pytest.raises(ForbiddenError):
            job_service._check_product_access(
                session, product_id=1, actor=actor, require_owner=True
            )


@pytest.mark.parametrize(
    "role,granted",
    [
        (UserRole.ADMIN, True),
        (UserRole.RELAYOPS_MEMBER, True),
        (UserRole.REGULAR_USER, False),
    ],
)
def test_check_project_access_elevation(session, role, granted):
    """product _check_project_access grants relayops_member like admin (require_owner)."""
    actor = _actor(role)
    if granted:
        assert (
            product_service._check_project_access(
                session, project_id=1, actor=actor, require_owner=True
            ).id
            == 1
        )
    else:
        with pytest.raises(ForbiddenError):
            product_service._check_project_access(
                session, project_id=1, actor=actor, require_owner=True
            )


# ── Member management: relayops_member gets owner-equivalent membership reach ─


@pytest.mark.parametrize(
    "role,expected_state",
    [
        (UserRole.ADMIN, "owner"),
        (UserRole.RELAYOPS_MEMBER, "owner"),  # platform-level Ops: owner-equivalent
        (UserRole.REGULAR_USER, "forbidden"),  # non-member stays locked out
    ],
)
def test_members_access_state_elevation(session, role, expected_state):
    """members._project_access_state grants a global relayops_member owner-equivalent
    membership access to a project they neither own nor belong to, while a plain
    regular_user is still forbidden (no regression)."""
    from api.routers import members

    _, state = members._project_access_state(session, 1, _actor(role))
    assert state == expected_state


def test_transfer_owner_denies_relayops_member(session):
    """Owner transfer is a platform-admin/true-owner-only power: a global
    relayops_member's owner-equivalent membership reach deliberately stops short of
    handing ownership off, even though _project_access_state grants them
    'owner' state for add/edit/remove."""
    from api.routers import members
    from api.schema import ProjectOwnerTransferRequest

    with pytest.raises(ForbiddenError):
        members.transfer_project_owner(
            project_id=1,
            body=ProjectOwnerTransferRequest(username="newowner"),
            session=session,
            current_user=_actor(UserRole.RELAYOPS_MEMBER),
        )


# ── T3 stays closed: admin panel is not granted to relayops_member ───────────


def _run_role_dep(role: str):
    dep = require_role(UserRole.ADMIN)  # == AdminOnly
    return asyncio.run(dep(current_user=_actor(role)))


def test_admin_only_denies_relayops_member():
    with pytest.raises(HTTPException) as exc:
        _run_role_dep(UserRole.RELAYOPS_MEMBER)
    assert exc.value.status_code == 403


def test_admin_only_allows_admin():
    user = _run_role_dep(UserRole.ADMIN)
    assert user.role == UserRole.ADMIN
