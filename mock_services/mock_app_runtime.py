"""Mock Runtime Application service (port 9002).

Simulates a real Runtime-backed application (feature-store-cluster) exposing
two health and configuration endpoints::

    GET /health
        {
          "status": "healthy",
          "app_type": "runtime",
          "runtime_imported": true,
          "config_loaded": true,
          "runtime_import_error": null,
          "config_error": null
        }

    GET /runtime/config
        {
          "status": "ok",
          "app_type": "runtime",
          "config": { "app": {...}, "model": {...}, "runtime": {...} }
        }

The mock CML Platform's control panel toggles healthy via PUT /control/health.
When unhealthy: /health returns 503 + config_error populated, and
/runtime/config returns 503 with status="error".
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel


app_state: dict[str, bool] = {"healthy": True}


class HealthControlRequest(BaseModel):
    healthy: bool


app = FastAPI(
    title="Mock Runtime App (feature-store-cluster)",
    description="Provides synthetic Runtime /health and /runtime/config responses.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"service": "mock-app-runtime", "app_type": "runtime"}


@app.get("/health")
async def health():
    if app_state["healthy"]:
        return JSONResponse(
            status_code=200,
            content={
                "status": "healthy",
                "app_type": "runtime",
                "runtime_imported": True,
                "config_loaded": True,
                "runtime_import_error": None,
                "config_error": None,
            },
        )
    return JSONResponse(
        status_code=503,
        content={
            "status": "unhealthy",
            "app_type": "runtime",
            "runtime_imported": True,
            "config_loaded": False,
            "runtime_import_error": None,
            "config_error": "config file failed to load",
        },
    )


@app.get("/runtime/config")
async def runtime_config():
    if app_state["healthy"]:
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "app_type": "runtime",
                "config": {
                    "app": {"name": "feature-store-cluster"},
                    "model": {"version": "1.0.0"},
                    "runtime": {"shards": 4, "replicas": 3},
                },
            },
        )
    return JSONResponse(
        status_code=503,
        content={
            "status": "error",
            "app_type": "runtime",
            "config_error": "config file failed to load",
        },
    )


@app.put("/control/health")
async def control_health(body: HealthControlRequest):
    app_state["healthy"] = body.healthy
    return {"healthy": app_state["healthy"]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9002)
