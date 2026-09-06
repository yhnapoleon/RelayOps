"""Standalone mock console — Ops login helper.

The CML monitor mocks (job/app status, application metadata) live in
``mock_services/cml_platform.py`` (port 9000). This console is a small
side-panel for logging into Ops as a developer to obtain a JWT and minting
a service API key for local testing.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from mock_services._dashboard import DASHBOARD_HTML

DEFAULT_RELAYOPS_URL = "http://backend:8000"

SERVICE_URLS: Dict[str, str] = {}

_relayops_tokens: Dict[str, str] = {}
_service_api_key: str = f"relayops_{secrets.token_hex(24)}"


class RelayOpsLoginRequest(BaseModel):
    username: str
    password: str
    relayops_url: str = DEFAULT_RELAYOPS_URL


class RelayOpsApiRequest(BaseModel):
    method: str = "GET"
    path: str
    body: Optional[Dict[str, Any]] = None
    relayops_url: str = DEFAULT_RELAYOPS_URL
    username: str = "admin"
    api_key: Optional[str] = None


class ConsoleAuthResponse(BaseModel):
    ok: bool
    username: str
    token_prefix: str
    role: str
    user_id: Optional[int] = None
    service_api_key: str
    service_api_key_prefix: str


app = FastAPI(
    title="Ops Mock Console",
    description="Ops login helper + Mock MMP control surface (job/app mocks live in cml_platform.py).",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _proxy_json(
    method: str,
    url: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
) -> Any:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.request(method, url, json=json_body)
        resp.raise_for_status()
        if not resp.content:
            return {}
        return resp.json()


async def _proxy_relayops_request(req: RelayOpsApiRequest) -> Dict[str, Any]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if req.api_key:
        headers["X-API-Key"] = req.api_key
    else:
        token = _relayops_tokens.get(req.username)
        if not token:
            return {"ok": False, "status": 0, "detail": f"No session for '{req.username}'. Login first."}
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.request(
                req.method.upper(),
                f"{req.relayops_url}{req.path}",
                headers=headers,
                json=req.body or None,
            )
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            return {"ok": 200 <= resp.status_code < 300, "status": resp.status_code, "body": body}
    except Exception as exc:
        return {"ok": False, "status": 0, "detail": str(exc)}


@app.get("/", response_class=HTMLResponse)
async def root_dashboard():
    return HTMLResponse(content=DASHBOARD_HTML, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/auth/service-key")
async def get_service_key():
    return {
        "service_api_key": _service_api_key,
        "service_api_key_prefix": _service_api_key[:12] + "...",
    }


@app.get("/control/status")
async def get_all_status():
    return {}


# ---------------------------------------------------------------------------
# Ops login + service-key minting
# ---------------------------------------------------------------------------


@app.post("/relayops/login")
async def relayops_login(req: RelayOpsLoginRequest):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{req.relayops_url}/login",
                json={"username": req.username, "password": req.password},
            )
            if resp.status_code != 200:
                return {"ok": False, "status": resp.status_code, "detail": resp.text}
            data = resp.json()
            token = data["access_token"]
            _relayops_tokens[req.username] = token
            me_resp = await client.get(
                f"{req.relayops_url}/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            me = me_resp.json() if me_resp.status_code == 200 else {}
            await _prime_external_services(client, req.relayops_url, token, me.get("role"))
            return {
                "ok": True,
                "username": req.username,
                "token_prefix": token[:20] + "...",
                "role": me.get("role", "unknown"),
                "user_id": me.get("user_id"),
                "service_api_key": _service_api_key,
                "service_api_key_prefix": _service_api_key[:12] + "...",
            }
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}


async def _prime_external_services(
    client: httpx.AsyncClient,
    relayops_url: str,
    jwt_token: str,
    role: Optional[str],
) -> None:
    """Mint a Ops API key on admin login for local-dev testing."""
    global _service_api_key

    if role == "admin":
        key_resp = await client.post(
            f"{relayops_url}/api/api-keys",
            headers={"Authorization": f"Bearer {jwt_token}"},
            json={"name": f"mock-console-auto-{secrets.token_hex(4)}"},
        )
        if key_resp.status_code in (200, 201):
            key_body = key_resp.json()
            if key_body.get("key"):
                _service_api_key = key_body["key"]


@app.get("/relayops/sessions")
async def relayops_sessions():
    return {username: token[:20] + "..." for username, token in _relayops_tokens.items()}


@app.post("/relayops/proxy")
async def relayops_proxy(req: RelayOpsApiRequest):
    return await _proxy_relayops_request(req)


@app.get("/service-ping")
async def service_ping():
    statuses = {}
    for name, base_url in SERVICE_URLS.items():
        try:
            data = await _proxy_json("GET", f"{base_url}/")
            statuses[name] = {"ok": True, "data": data}
        except Exception as exc:
            statuses[name] = {"ok": False, "detail": str(exc)}
    return statuses
