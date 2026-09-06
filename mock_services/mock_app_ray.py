"""Mock Ray Application service (port 9003).

Simulates a real Ray-backed application (model-serving-ray) exposing the
``/ray/full-test`` health probe.

The mock CML Platform's control panel toggles healthy via PUT /control/health.
When unhealthy: /ray/full-test returns 503 with full_test="failed" and one
sub-check flipped to passed=false.
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
    title="Mock Ray App (model-serving-ray)",
    description="Provides a synthetic Ray /ray/full-test response.",
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
    return {"service": "mock-app-ray", "app_type": "ray"}


def _healthy_payload() -> dict:
    return {
        "app_type": "ray",
        "test_name": "full",
        "dependency_path": "/app/demo-dependencies",
        "dependency_path_exists": True,
        "ray_imported": True,
        "ray_initialized": True,
        "ray_version": "2.6.3",
        "numpy_version": "1.26.4",
        "pyarrow_version": "6.0.1",
        "ray_import_error": None,
        "ray_init_error": None,
        "cluster_resources": {
            "CPU": 3.0,
            "object_store_memory": 9338118144.0,
            "memory": 18676236288.0,
        },
        "available_resources": {
            "CPU": 3.0,
            "object_store_memory": 9338118144.0,
            "memory": 18676236288.0,
        },
        "full_test": "passed",
        "results": {
            "status": {"passed": True, "nodes_count": 1, "alive_nodes_count": 1},
            "task": {"passed": True, "result": 42, "expected": 42},
            "object_store": {
                "passed": True,
                "result": {"message": "hello", "value": 123},
            },
            "parallel_tasks": {
                "passed": True,
                "results": [0, 1, 4, 9, 16, 25, 36, 49],
                "expected": [0, 1, 4, 9, 16, 25, 36, 49],
            },
            "actor": {
                "passed": True,
                "first_increment": 1,
                "second_increment": 2,
                "final_value": 2,
            },
        },
        "child_returncode": 0,
        "child_stderr_tail": "",
    }


def _unhealthy_payload() -> dict:
    payload = _healthy_payload()
    payload["full_test"] = "failed"
    payload["results"]["status"] = {
        "passed": False,
        "nodes_count": 0,
        "alive_nodes_count": 0,
    }
    payload["available_resources"] = {
        "CPU": 0.0,
        "object_store_memory": 0.0,
        "memory": 0.0,
    }
    payload["child_returncode"] = 1
    payload["child_stderr_tail"] = "Ray cluster has no alive nodes"
    return payload


@app.get("/ray/full-test")
async def ray_full_test():
    if app_state["healthy"]:
        return JSONResponse(status_code=200, content=_healthy_payload())
    return JSONResponse(status_code=503, content=_unhealthy_payload())


@app.put("/control/health")
async def control_health(body: HealthControlRequest):
    app_state["healthy"] = body.healthy
    return {"healthy": app_state["healthy"]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9003)
