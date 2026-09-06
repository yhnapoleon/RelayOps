"""Shared pytest fixtures for RelayOps unit tests.

The project layout uses ``src/`` as the source root (per pyproject.toml's
``[tool.setuptools.packages.find]``). Tests run from the repo root, so
we prepend ``src/`` to sys.path here once per pytest session instead of
repeating ``sys.path.insert(0, "src")`` in every test file.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
