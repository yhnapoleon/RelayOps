"""Exercise the running Compose demo without third-party Python dependencies."""
import json
import os
import time
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen


def request(url, payload=None, token=None, method=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers, method=method)
    with urlopen(req, timeout=10) as response:
        body = response.read()
        return json.loads(body) if 'json' in response.headers.get('Content-Type', '') else body.decode()


def wait_for(check, seconds=180):
    deadline = time.monotonic() + seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (URLError, HTTPError, TimeoutError, OSError) as exc:
            last_error = type(exc).__name__
        time.sleep(2)
    raise RuntimeError(f'Demo did not become ready ({last_error})')


def main():
    base = os.getenv('RELAYOPS_SMOKE_API_URL', 'http://127.0.0.1:8000').rstrip('/')
    wait_for(lambda: request(base + '/healthz'))
    login = wait_for(lambda: request(base + '/login', {'username': 'admin', 'password': 'admin123'}))
    token = login['access_token']
    me = request(base + '/me', token=token)
    assert me['username'] == 'admin' and me['role'] == 'admin'
    projects = request(base + '/api/projects', token=token)
    assert isinstance(projects, list) and any('Demo' in item['name'] for item in projects), 'Demo seeding failed'
    account = {'username': f'smoke-user-{time.time_ns()}', 'password': 'smoke-account-password'}
    user = request(base + '/api/admin/users', account, token)
    user_token = request(base + '/login', account)['access_token']
    assert request(base + '/me', token=user_token)['role'] == 'regular_user'
    try:
        request(base + '/api/admin/users', token=user_token)
        raise AssertionError('Regular account accessed admin users')
    except HTTPError as error:
        assert error.code == 403
    request(base + f"/api/admin/users/{user['id']}/password", {'password': 'updated-smoke-password'}, token, method='PUT')
    try:
        request(base + '/me', token=user_token)
        raise AssertionError('Reset password left an old session valid')
    except HTTPError as error:
        assert error.code == 401
    user_token = request(base + '/login', {**account, 'password': 'updated-smoke-password'})['access_token']
    request(base + '/logout', {}, user_token)
    try:
        request(base + '/me', token=user_token)
        raise AssertionError('Logout left a session valid')
    except HTTPError as error:
        assert error.code == 401
    assert 'RelayOps' in request(os.getenv('RELAYOPS_SMOKE_FRONTEND_URL', 'http://127.0.0.1:8080'))
    monitor = 'http://127.0.0.1:9005'
    demo_token = 'relayops-demo-monitor-token'
    request(monitor + '/api/demo/signals', {'drifted': True}, demo_token)
    def drift_issue():
        return next((i for i in request(base + '/api/issues', token=token)
                     if i.get('type') == 'mmp_drift' and i.get('status') not in ('resolved', 'closed')), None)
    issue = wait_for(drift_issue, seconds=120)
    request(monitor + '/api/demo/signals', {'drifted': False}, demo_token)
    recovered = wait_for(lambda: request(base + f"/api/issues/{issue['id']}", token=token).get('status')
                        in ('resolved', 'closed'), seconds=120)
    assert recovered
    print('Demo smoke passed: frontend, local accounts, passwords, session revocation, roles, seeded projects, model drift and recovery.')


if __name__ == '__main__':
    main()
