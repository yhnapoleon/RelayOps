"""Tests for apps._probe_cml_app_binding — the binding-only resolver behind
the Subdomain "Fetch" button and the Validate binding check.

Focus on the fallback wiring: a subdomain Fetch must scan the workspace even
when there's no usable project binding, and a bare URL probe (no name/
subdomain) must stay "not asked" (ok=None).
"""
from __future__ import annotations

import sys
from unittest.mock import patch

sys.path.insert(0, "src")

from api.routers.apps import _probe_cml_app_binding


class _FakeControl:
    def __init__(self, *, subdomain_hit=None, scanned=0, total=0):
        self._hit = subdomain_hit
        self._scanned = scanned
        self._total = total
        self.scanned_with_exclude = "unset"

    def find_application_by_subdomain(
        self, subdomain, *, exclude_project_id=None, max_projects=300, time_budget_seconds=8.0
    ):
        self.scanned_with_exclude = exclude_project_id
        return {
            "match": self._hit,
            "scanned": self._scanned,
            "total": self._total,
            "capped": False,
            "timed_out": False,
            "error": None,
        }


def _patch_control(control):
    return patch(
        "core.services.cml_binding_resolver.build_control_interface",
        return_value=control,
    )


def test_no_name_or_subdomain_is_not_asked():
    probe = _probe_cml_app_binding(
        None, project_id=None, cml_application_name=None, cml_subdomain=None,
    )
    assert probe["ok"] is None
    assert probe["other"] is None


def test_subdomain_fetch_scans_workspace_without_project_binding():
    hit = {
        "project_id": "id-inventory-scoring",
        "project_name": "inventory-scoring",
        "application_id": "a2",
        "application_name": "ns-api",
        "subdomain": "dynamic-inventory-scoring-prod",
    }
    control = _FakeControl(subdomain_hit=hit)
    with _patch_control(control):
        probe = _probe_cml_app_binding(
            None,
            project_id=None,
            cml_application_name=None,
            cml_subdomain="dynamic-inventory-scoring-prod",
        )
    # No bound project, but the scan still found the owning project.
    assert probe["ok"] is False
    assert probe["other"] == hit
    assert control.scanned_with_exclude is None


def test_subdomain_fetch_no_match_returns_no_other():
    control = _FakeControl(subdomain_hit=None)
    with _patch_control(control):
        probe = _probe_cml_app_binding(
            None,
            project_id=None,
            cml_application_name=None,
            cml_subdomain="nope",
        )
    assert probe["ok"] is False
    assert probe["other"] is None
