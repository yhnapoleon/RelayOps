"""Exercise the running Compose demo without third-party Python dependencies."""
import json
import time
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen


def request(url, payload=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
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
    base = 'http://127.0.0.1:8000'
    wait_for(lambda: request(base + '/healthz'))
    login = wait_for(lambda: request(base + '/login', {'username': 'admin', 'password': 'admin123'}))
    token = login['access_token']
    me = request(base + '/me', token=token)
    assert me['username'] == 'admin' and me['role'] == 'admin'
    projects = request(base + '/api/projects', token=token)
    assert isinstance(projects, list) and any('Demo' in item['name'] for item in projects), 'Demo seeding failed'
    assert 'RelayOps' in request('http://127.0.0.1:8080')
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
    print('Demo smoke passed: frontend, LDAP login, roles, seeded projects, model drift and recovery.')


if __name__ == '__main__':
    main()
