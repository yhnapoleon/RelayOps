"""Idempotent backfill for the guide KB's FTS5 index.

Rebuilds the ``kb_fts`` virtual table in ``core/agent/retrieval.py``'s SQLite
sidecar from the Markdown knowledge base under ``docs/kb/`` (parsed by
``core/agent/kb_loader.py``). The KB is static source, so this is a full
replace — safe to run on every startup, right after the resolution/scenario
backfill.
"""
from __future__ import annotations

from core.logging import get_logger

logger = get_logger(__name__)


def run_backfill() -> dict:
    from core.agent import kb_loader, retrieval

    kb_loader.clear_cache()  # pick up any edited/added KB file on restart
    indexed = retrieval.reindex_kb()
    logger.info("KB FTS backfill: {} chunks", indexed)
    return {"chunks": indexed}


if __name__ == "__main__":
    run_backfill()
