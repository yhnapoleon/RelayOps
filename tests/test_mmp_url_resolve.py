"""Tests for projects.resolve_mmp_url — pasting an MMP web link to auto-fill
the binding. Covers id extraction from the URL and production-model picking.
"""
from __future__ import annotations

import sys
from unittest.mock import patch

sys.path.insert(0, "src")

from api.routers.projects import resolve_mmp_url


class _FakeMmp:
    def __init__(self, *, configured=True, payload=None, raise_exc=None):
        self._configured = configured
        self._payload = payload or {}
        self._raise = raise_exc

    def is_configured(self):
        return self._configured

    def get_project(self, pid):
        if self._raise:
            raise self._raise
        return self._payload


def _patch(fake):
    return patch("api.routers.projects._make_mmp_interface", return_value=fake)


def test_no_id_in_url_errors():
    res = resolve_mmp_url(url="https://runtime-mmp-web/notaproject", current_user=None)
    assert res.project_repo_name is None
    assert res.error and "project id" in res.error.lower()


def test_single_production_model_suggested():
    payload = {
        "project_repo_name": "demo-material-classifier@dynamic-inventory-scoring",
        "business_understanding_project_name": "Dynamic Inventory Scoring - CFS",
        "models": [
            {"model_name": "sg-rome-api", "is_production": True},
            {"model_name": "my-rome-api", "is_production": False},
        ],
    }
    with _patch(_FakeMmp(payload=payload)):
        res = resolve_mmp_url(
            url="https://runtime-mmp-web-prod…/project/225/projectDetails", current_user=None
        )
    assert res.project_id == 225
    assert res.project_repo_name == "demo-material-classifier@dynamic-inventory-scoring"
    assert res.suggested_model_name == "sg-rome-api"
    assert len(res.models) == 2


def test_multiple_production_models_no_suggestion():
    payload = {
        "project_repo_name": "demo-material-classifier@batch-inventory-scoring",
        "models": [
            {"model_name": "sg-relayops-batch", "is_production": True},
            {"model_name": "sg-northstar-batch", "is_production": True},
        ],
    }
    with _patch(_FakeMmp(payload=payload)):
        res = resolve_mmp_url(url="https://x/project/224/", current_user=None)
    assert res.suggested_model_name is None
    assert {m.model_name for m in res.models} == {"sg-relayops-batch", "sg-northstar-batch"}


def test_not_configured_errors():
    with _patch(_FakeMmp(configured=False)):
        res = resolve_mmp_url(url="https://x/project/160/projectDetails", current_user=None)
    assert res.project_id == 160
    assert res.error and "not configured" in res.error.lower()
