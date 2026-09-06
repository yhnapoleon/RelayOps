# Development and verification

Use Python 3.10+ (the container uses 3.12) and Node.js 22 for local development.

```bash
python -m venv .venv
# Activate .venv using your shell's activation script.
python -m pip install -e ".[agent,dev]"
docker compose up -d --build backend
```

In a second terminal:

```bash
cd src/frontend
npm ci
npm run dev
```

The Vite development server runs on port 3000 and proxies the containerized API to port 8000. Rebuild the backend image after changing Python code. This arrangement keeps the backend and mock discovery endpoints on the same Docker network. Running the backend directly on the host requires adapting the mock applications' advertised addresses; that alternative is not configured by the default demo.

`config.sample.yaml` supplies demo defaults. For direct Python experiments, copy it to the ignored `config.yaml` or point `CONFIG_FILE` at an explicit file. A missing explicit configuration path fails instead of silently switching environments. Compose mounts `config.sample.yaml` explicitly; adjust that volume mapping if you intend to use another configuration file.

## Tests

```bash
python -m pytest tests -q
python -m pytest mock_services/test_cml_platform.py -q
python -m compileall -q src mock_services project.py
python scripts/check_public_tree.py
docker compose config --quiet
```

Run `npm run lint` and `npm run build` inside `src/frontend` as well. The default backend suite covers pure services, database behavior on isolated SQLite fixtures, permissions, assistant contracts, and normalized monitoring. Some tests use mocked transports. `tests/test_agent_golden.py` requires a deliberately configured live model and database and is skipped unless `RELAYOPS_GOLDEN=1` is set.

The full Compose deployment should also be exercised: verify `/healthz`, log in, inspect seeded resources, trigger an incident through the mock console, and verify recovery. Passing unit tests alone does not establish that an external integration works.

## Configuration boundaries

- `RELAYOPS_DEMO_ACCOUNTS` controls demo-account initialization; `RELAYOPS_ADMIN_USERNAME` and `RELAYOPS_ADMIN_PASSWORD` bootstrap an administrator without overwriting existing credentials.
- `RELAYOPS_DATABASE_URL` and `RELAYOPS_JWT_SECRET` override backend connection/signing settings.
- `RELAYOPS_LLM_ENDPOINT`, `RELAYOPS_LLM_API_KEY`, `RELAYOPS_LLM_MODEL` explicitly enable model use; there is no implicit provider or adjacent-file credential discovery.
- SMTP requires explicit `email.backend: smtp` and an SMTP host. The default is `log`.
- Generic HTTP integrations verify TLS by default. The local mock endpoints use HTTP on the isolated demo network.
- Frontend source never receives server API credentials at build time.

Dependencies are declared in `pyproject.toml` and `src/frontend/package-lock.json`. Backend dependency ranges are not a full reproducibility lock; validate upgrades before deployment.
