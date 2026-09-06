"""Local account authentication and session permission boundaries."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.models.entities  # Register models for isolated database fixtures.
from core.auth import auth as passwords
from core.models.database import Base
from core.models.user import User


def test_password_hash_is_salted_and_rejects_invalid_values():
    assert hasattr(passwords, 'hash_password'), 'Local password hashing is required'
    first = passwords.hash_password('long-demo-password')
    second = passwords.hash_password('long-demo-password')
    assert first != second
    assert 'long-demo-password' not in first
    assert passwords.verify_password('long-demo-password', first)
    assert not passwords.verify_password('wrong-password', first)
    for value in [None, '', 'plaintext', 'pbkdf2_sha256$999999999$bad$bad']:
        assert not passwords.verify_password('long-demo-password', value)


@pytest.fixture
def client(monkeypatch):
    from core.models import database
    from core.services import user_service
    from api.routers import auth, users
    from api.deps.db import get_session

    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = SimpleNamespace(get_session=factory)
    monkeypatch.setattr(database, 'get_db', lambda: db)
    monkeypatch.setattr(user_service, 'get_db', lambda: db)
    assert hasattr(user_service, 'bootstrap_local_accounts'), 'Local account bootstrap is required'
    user_service.bootstrap_local_accounts()
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(users.router)

    def session():
        with factory() as value:
            yield value

    app.dependency_overrides[get_session] = session
    with TestClient(app) as api:
        yield api, factory
    engine.dispose()


def headers(api, username='admin', password='admin123'):
    response = api.post('/login', json={'username': username, 'password': password})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['access_token']}


def test_local_login_rejects_wrong_unknown_and_unset_passwords(client):
    api, factory = client
    assert api.get('/me', headers=headers(api, ' ADMIN ')).json()['username'] == 'admin'
    for username, password in [('admin', 'wrong'), ('missing', 'admin123')]:
        response = api.post('/login', json={'username': username, 'password': password})
        assert response.status_code == 401
        assert response.json()['detail'] == 'Invalid credentials'
    with factory() as session:
        session.add(User(username='pending', role='regular_user'))
        session.commit()
    assert api.post('/login', json={'username': 'pending', 'password': 'anything'}).status_code == 401


def test_admin_can_create_account_without_exposing_hash_and_regular_user_cannot(client):
    api, _ = client
    regular = headers(api, 'testuser', 'test123')
    body = {'username': 'new-user', 'password': 'long-demo-password', 'display_name': 'New User'}
    assert api.post('/api/admin/users', json=body, headers=regular).status_code == 403
    admin = headers(api)
    response = api.post('/api/admin/users', json=body, headers=admin)
    assert response.status_code == 201, response.text
    assert 'password' not in response.text
    assert api.post('/api/admin/users', json=body, headers=admin).status_code == 409
    assert api.get('/me', headers=headers(api, 'new-user', body['password'])).json()['role'] == 'regular_user'
    assert api.post('/api/admin/users', json={**body, 'username': 'short', 'password': 'short'}, headers=admin).status_code == 422


def test_logout_revokes_token(client):
    api, _ = client
    token = headers(api)
    assert api.post('/logout', headers=token).status_code == 200
    assert api.get('/me', headers=token).status_code == 401


def test_password_change_verifies_old_password_and_revokes_sessions(client):
    api, _ = client
    token = headers(api, 'testuser', 'test123')
    assert api.post('/auth/password', headers=token, json={'current_password': 'wrong', 'new_password': 'new-demo-password'}).status_code == 400
    assert api.post('/auth/password', headers=token, json={'current_password': 'test123', 'new_password': 'new-demo-password'}).status_code == 200
    assert api.get('/me', headers=token).status_code == 401
    headers(api, 'testuser', 'new-demo-password')


def test_admin_reset_password_provisions_pending_account(client):
    api, factory = client
    with factory() as session:
        user = User(username='pending', role='regular_user')
        session.add(user)
        session.commit()
        uid = user.id
    body = {'password': 'new-demo-password'}
    assert api.put(f'/api/admin/users/{uid}/password', headers=headers(api, 'testuser', 'test123'), json=body).status_code == 403
    assert api.put(f'/api/admin/users/{uid}/password', headers=headers(api), json=body).status_code == 200
    headers(api, 'pending', body['password'])


def test_database_role_and_group_changes_apply_to_existing_token(client):
    api, factory = client
    token = headers(api, 'relayopsmember1', 'relayops123')
    with factory() as session:
        user = session.query(User).filter_by(username='relayopsmember1').one()
        user.role = 'regular_user'
        user.group_keys = []
        session.commit()
    me = api.get('/me', headers=token)
    assert me.status_code == 200
    assert me.json()['role'] == 'regular_user'
    assert me.json()['groups'] == []


def test_bootstrap_is_idempotent_and_never_resets_existing_password(client):
    api, factory = client
    from core.services.user_service import bootstrap_local_accounts
    with factory() as session:
        user = session.query(User).filter_by(username='testuser').one()
        user.password_hash = passwords.hash_password('changed-demo-password')
        user.role = 'relayops_member'
        session.commit()
    bootstrap_local_accounts()
    assert api.get('/me', headers=headers(api, 'testuser', 'changed-demo-password')).json()['role'] == 'relayops_member'


def test_expired_and_tampered_tokens_fail(client):
    api, _ = client
    from core.auth.jwt import create_access_token
    login = api.post('/login', json={'username': 'admin', 'password': 'admin123'}).json()
    expired = create_access_token('admin', login['user_id'], expires_delta=timedelta(seconds=-1))
    for token in [expired, login['access_token'] + 'tampered']:
        assert api.get('/me', headers={'Authorization': 'Bearer ' + token}).status_code == 401


def test_reset_revokes_existing_sessions_and_audit_never_stores_password(client):
    api, factory = client
    from core.models.entities import AuditLog
    token = headers(api, 'testuser', 'test123')
    uid = api.get('/me', headers=token).json()['user_id']
    password = 'replacement-demo-password'
    assert api.put(f'/api/admin/users/{uid}/password', headers=headers(api), json={'password': password}).status_code == 200
    assert api.get('/me', headers=token).status_code == 401
    headers(api, 'testuser', password)
    with factory() as session:
        audit = session.query(AuditLog).filter_by(entity_type='user_password', entity_id=uid).one()
        assert audit.new_value == {'reset': True}
        assert audit.old_value is None


def test_local_user_search_requires_auth_and_omits_credentials(client):
    api, _ = client
    assert api.get('/api/users/search?q=test').status_code in (401, 403)
    results = api.get('/api/users/search?q=test', headers=headers(api)).json()['results']
    assert any(row['username'] == 'testuser' for row in results)
    assert all('password_hash' not in row and 'token_version' not in row for row in results)


@pytest.mark.parametrize('production', [False, True])
def test_explicit_bootstrap_without_demo_accounts(monkeypatch, production):
    from core import config
    from core.services import user_service
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(user_service, 'get_db', lambda: SimpleNamespace(get_session=factory))
    # Production must suppress demo accounts even if the demo option is on.
    monkeypatch.setattr(config, 'get_config', lambda: SimpleNamespace(auth_demo_accounts=production, is_production=production))
    monkeypatch.delenv('RELAYOPS_ADMIN_PASSWORD', raising=False)
    with pytest.raises(RuntimeError, match='initialize an administrator'):
        user_service.bootstrap_local_accounts()
    monkeypatch.setenv('RELAYOPS_ADMIN_USERNAME', 'operator')
    monkeypatch.setenv('RELAYOPS_ADMIN_PASSWORD', 'operator-demo-password')
    user_service.bootstrap_local_accounts()
    with factory() as session:
        users = session.query(User).all()
        assert len(users) == 1 and users[0].username == 'operator'
        assert users[0].role == 'admin'
        assert passwords.verify_password('operator-demo-password', users[0].password_hash)
    engine.dispose()
