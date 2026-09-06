"""Mock FastAPI Application service (port 9001).

Simulates a real FastAPI application (inventory-health-api) exposing the
``/health`` contract documented in CML/app.md §5.1::

    {
      "status": "healthy",
      "app_type": "fastapi",
      "hostname": "<hostname>",
      "timestamp": "<ISO-8601>"
    }

The mock CML Platform's control panel can flip the underlying healthy
state via PUT /control/health.
"""

from __future__ import annotations

import socket
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel


app_state: dict[str, bool] = {"healthy": True}


class HealthControlRequest(BaseModel):
    healthy: bool


app = FastAPI(
    title="Mock FastAPI App (inventory-health-api)",
    description="Speaks the FastAPI /health contract from CML/app.md §5.1.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@app.get("/")
async def root():
    return {"service": "mock-app-fastapi", "app_type": "fastapi"}


@app.get("/health")
async def health():
    """Return §5.1 health response. Status code 503 when the toggle is off."""
    body = {
        "status": "healthy" if app_state["healthy"] else "unhealthy",
        "app_type": "fastapi",
        "hostname": socket.gethostname(),
        "timestamp": _now_iso(),
    }
    return JSONResponse(
        status_code=200 if app_state["healthy"] else 503,
        content=body,
    )


@app.put("/control/health")
async def control_health(body: HealthControlRequest):
    """Toggle the underlying health flag (driven by mock CML's control panel)."""
    app_state["healthy"] = body.healthy
    return {"healthy": app_state["healthy"]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9001)
