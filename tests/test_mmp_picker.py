"""Unit tests for the MMP picker endpoint helper.

These tests target ``_build_mmp_model_options`` — the pure transformation
that turns ``MmpInterface.list_projects_shallow()`` into the flat sorted
list of ``MmpModelOption`` rows the Job form consumes.

The route handler itself is a one-liner over this helper, so once the
helper is correct the route is too.
"""

from __future__ import annotations

from typing import Any

import pytest

from api.routers.projects import (
    _build_mmp_model_options,
    _build_mmp_project_options,
)
from core.integrations.mmp_interface import MmpApiError


class StubInterface:
    """Minimal MmpInterface stand-in. Returns whatever we tell it to,
    raises whatever we tell it to.
    """

    def __init__(
        self,
        *,
        configured: bool = True,
        directory: dict[str, dict[str, Any]] | None = None,
        raise_on_list: Exception | None = None,
    ):
        self._configured = configured
        self._directory = directory or {}
        self._raise = raise_on_list

    def is_configured(self) -> bool:
        return self._configured

    def list_projects_shallow(self) -> dict[str, dict[str, Any]]:
        if self._raise is not None:
            raise self._raise
        return self._directory


SAMPLE_DIRECTORY = {
    "alpha-repo@entity-1": {
        "id": 100,
        "business_name": "Alpha Business",
        "models": [
            {"id": 1, "name": "zebra-model", "is_production": True},
            {"id": 2, "name": "alpha-model", "is_production": False},
        ],
    },
    "beta-repo@entity-2": {
        "id": 200,
        "business_name": "Beta Business",
        "models": [
            {"id": 3, "name": "alpha-model", "is_production": True},
        ],
    },
}


class TestEmptyAndUnconfigured:
    def test_returns_empty_when_not_configured(self):
        assert _build_mmp_model_options(StubInterface(configured=False)) == []

    def test_returns_empty_when_directory_empty(self):
        assert _build_mmp_model_options(StubInterface(directory={})) == []

    def test_returns_empty_when_api_error(self):
        iface = StubInterface(raise_on_list=MmpApiError("server down", status_code=500))
        # Helper degrades gracefully — UI gets [] and falls back to plain
        # text input rather than failing the whole Job form load.
        assert _build_mmp_model_options(iface) == []

    def test_returns_empty_on_unexpected_exception(self):
        iface = StubInterface(raise_on_list=RuntimeError("boom"))
        assert _build_mmp_model_options(iface) == []


class TestPayloadShape:
    def test_one_option_per_project_model_pair(self):
        options = _build_mmp_model_options(StubInterface(directory=SAMPLE_DIRECTORY))
        # 2 + 1 = 3 model rows
        assert len(options) == 3

    def test_carries_all_secondary_metadata(self):
        options = _build_mmp_model_options(StubInterface(directory=SAMPLE_DIRECTORY))
        zebra = next(o for o in options if o.model_name == "zebra-model")
        assert zebra.project_repo_name == "alpha-repo@entity-1"
        assert zebra.business_name == "Alpha Business"
        assert zebra.is_production is True

    def test_drops_models_with_blank_name(self):
        directory = {
            "x@y": {
                "id": 1,
                "business_name": "B",
                "models": [
                    {"id": 1, "name": "", "is_production": True},
                    {"id": 2, "name": "ok-model", "is_production": True},
                ],
            },
        }
        options = _build_mmp_model_options(StubInterface(directory=directory))
        assert [o.model_name for o in options] == ["ok-model"]

    def test_business_name_is_none_when_missing(self):
        directory = {
            "x@y": {
                "id": 1,
                "business_name": "",
                "models": [{"id": 1, "name": "m", "is_production": True}],
            },
        }
        options = _build_mmp_model_options(StubInterface(directory=directory))
        assert options[0].business_name is None


class TestSorting:
    def test_sorted_by_model_name_primary(self):
        options = _build_mmp_model_options(StubInterface(directory=SAMPLE_DIRECTORY))
        names = [o.model_name for o in options]
        # alpha-model (alpha-repo), alpha-model (beta-repo), then zebra-model
        assert names == ["alpha-model", "alpha-model", "zebra-model"]

    def test_repo_name_breaks_tie_when_model_names_collide(self):
        options = _build_mmp_model_options(StubInterface(directory=SAMPLE_DIRECTORY))
        alphas = [o for o in options if o.model_name == "alpha-model"]
        # alpha-repo sorts before beta-repo
        assert [a.project_repo_name for a in alphas] == [
            "alpha-repo@entity-1",
            "beta-repo@entity-2",
        ]

    def test_sort_is_case_insensitive(self):
        directory = {
            "x@y": {
                "id": 1,
                "business_name": "B",
                "models": [
                    {"id": 1, "name": "Zebra", "is_production": True},
                    {"id": 2, "name": "alpha", "is_production": True},
                ],
            },
        }
        options = _build_mmp_model_options(StubInterface(directory=directory))
        assert [o.model_name for o in options] == ["alpha", "Zebra"]


class TestRepoNameFilter:
    """Phase 7: Job-level picker scopes to parent Project's MMP binding."""

    def test_filter_keeps_only_target_repo(self):
        options = _build_mmp_model_options(
            StubInterface(directory=SAMPLE_DIRECTORY),
            repo_name_filter="alpha-repo@entity-1",
        )
        assert {o.project_repo_name for o in options} == {"alpha-repo@entity-1"}
        assert {o.model_name for o in options} == {"zebra-model", "alpha-model"}

    def test_empty_filter_acts_like_no_filter(self):
        all_options = _build_mmp_model_options(StubInterface(directory=SAMPLE_DIRECTORY))
        empty_filter = _build_mmp_model_options(
            StubInterface(directory=SAMPLE_DIRECTORY), repo_name_filter="",
        )
        assert [(o.model_name, o.project_repo_name) for o in all_options] == \
               [(o.model_name, o.project_repo_name) for o in empty_filter]

    def test_unknown_repo_returns_empty(self):
        options = _build_mmp_model_options(
            StubInterface(directory=SAMPLE_DIRECTORY),
            repo_name_filter="nonexistent@repo",
        )
        assert options == []


class TestProjectOptions:
    """Phase 7: workspace-wide MMP project picker (one row per MMP project)."""

    def test_returns_empty_when_not_configured(self):
        assert _build_mmp_project_options(StubInterface(configured=False)) == []

    def test_returns_empty_on_api_error(self):
        iface = StubInterface(raise_on_list=MmpApiError("down", status_code=500))
        assert _build_mmp_project_options(iface) == []

    def test_one_option_per_project(self):
        options = _build_mmp_project_options(StubInterface(directory=SAMPLE_DIRECTORY))
        assert len(options) == 2
        assert {o.project_repo_name for o in options} == {
            "alpha-repo@entity-1",
            "beta-repo@entity-2",
        }

    def test_model_count_reflects_directory(self):
        options = _build_mmp_project_options(StubInterface(directory=SAMPLE_DIRECTORY))
        by_repo = {o.project_repo_name: o for o in options}
        assert by_repo["alpha-repo@entity-1"].model_count == 2
        assert by_repo["beta-repo@entity-2"].model_count == 1

    def test_sorted_by_business_name(self):
        # Both "Alpha Business" and "Beta Business" share initials; verify
        # business_name drives sort order, not project_repo_name.
        options = _build_mmp_project_options(StubInterface(directory=SAMPLE_DIRECTORY))
        names = [o.business_name for o in options]
        assert names == ["Alpha Business", "Beta Business"]

    def test_falls_back_to_repo_name_when_business_name_missing(self):
        directory = {
            "z-repo@e": {"id": 1, "business_name": "", "models": []},
            "a-repo@e": {"id": 2, "business_name": "", "models": []},
        }
        options = _build_mmp_project_options(StubInterface(directory=directory))
        assert [o.project_repo_name for o in options] == ["a-repo@e", "z-repo@e"]
        assert all(o.business_name is None for o in options)
