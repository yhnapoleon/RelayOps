<p align="center"><img src="src/frontend/src/assets/relayops-mark.svg" width="88" alt="RelayOps" /></p>

# RelayOps

**Operations handover, service monitoring, and incident management with optional AI assistance.**

[中文说明](README.zh-CN.md) · [Architecture](docs/ARCHITECTURE.md) · [Demo walkthrough](docs/DEMO.md) · [Development](docs/DEVELOPMENT.md)

RelayOps connects project handover to day-to-day operations: define ownership and recovery procedures, monitor services and scheduled jobs, route incidents to the on-duty team, and retain an audit trail of what happened.

## What you can explore

- **Handover and verification:** projects, products, versioned handovers, recovery scenarios, and review workflows.
- **Monitoring:** application health, scheduled-job execution, heartbeat/staleness checks, and synthetic model drift signals.
- **Incident lifecycle:** ownership, on-duty assignment, SLA tracking, escalation, recovery, and audit records.
- **Operational visibility:** schedules, dashboards, analytics, and duty summaries.
- **Optional AI assistance:** document extraction, onboarding drafts, contextual guidance, diagnosis, and proposed changes that require confirmation. Core workflows run without a model API key.

## Run the local demo

Install Docker with Docker Compose, then:

```bash
git clone https://github.com/yhnapoleon/RelayOps.git
cd RelayOps
docker compose up -d --build
```

Open **http://localhost:8080** and sign in with a local demo account:

| Username | Password | Purpose |
|---|---|---|
| `admin` | `admin123` | Platform administration |
| `testuser` | `test123` | Project ownership and handover |
| `relayopsmember1` | `relayops123` | Operations workflows |
| `relayopsmember2` | `relayops123` | Operations workflows |

These are deliberately public demo credentials. Compose binds published ports to the local computer. Do not expose this stack as a production service.

On startup the app creates its database schema, initializes local accounts, and populates synthetic walkthrough resources. The first image build downloads dependencies and may take several minutes.

| Surface | Address |
|---|---|
| Application | http://localhost:8080 |
| API documentation | http://localhost:8000/docs |
| Scheduler and application mock console | http://localhost:9000 |
| Model-monitor demo API | http://localhost:9005/docs |
| Demo applications | Ports `9001`, `9002`, `9003` |

Stop services with `docker compose down`. Data remains in the named volume. Start with the [walkthrough](docs/DEMO.md) to trigger a synthetic incident and recovery.

## Accounts and sign-in

Sign-in uses local usernames and passwords with salted PBKDF2-SHA256 hashes and JWT sessions. Administrators can create accounts and set passwords in **Admin Panel → User Management**. Any signed-in user can choose **Change password** in the sidebar. New passwords require at least 12 characters; changing a password or signing out invalidates all existing sessions for that account. Role changes apply on the next request.

For a fresh database without demo accounts, copy `.env.example` to `.env`, set `RELAYOPS_DEMO_ACCOUNTS=false`, and configure `RELAYOPS_ADMIN_USERNAME`, `RELAYOPS_ADMIN_PASSWORD`, and a random `RELAYOPS_JWT_SECRET`. Bootstrap settings never reset existing passwords. Disabling demo initialization does not remove previously created accounts. Existing accounts without a local password need an administrator to use **Set password** before they can sign in.

## Optional AI configuration

Copy `.env.example` to `.env`, then set `RELAYOPS_LLM_ENDPOINT`, `RELAYOPS_LLM_API_KEY`, and `RELAYOPS_LLM_MODEL` for your OpenAI-compatible provider. Recreate the backend with `docker compose up -d backend`.

Credentials stay on the backend; no API key is compiled into the frontend. Model calls use the provider you explicitly configure, so use synthetic documents and data in this demo. Live-model evaluations are opt-in and are not part of the default test run.

## Stack and checks

Python 3.10+ · FastAPI · SQLAlchemy · PostgreSQL · React 19 · TypeScript · Vite · Tailwind CSS · optional LangGraph / LangChain.

```bash
python -m pip install -e ".[agent,dev]"
python -m pytest tests -q
cd src/frontend
npm ci
npm run lint
npm run build
```

See [development notes](docs/DEVELOPMENT.md) for local services, configuration, and test boundaries.
