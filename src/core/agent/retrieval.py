"""Resolution / runbook retrieval — SQLite FTS5 sidecar index (zero new deps).

The platform DB is PostgreSQL; the search index is a small standalone SQLite
file (BM25 via FTS5 ships in the stdlib sqlite3). Two virtual tables:

* ``issue_fts``   — resolved/closed/false-positive issues that carry a
  resolution_description. Indexed on resolve (issue_service hook) and via
  ``core/infrastructure/resolution_fts_backfill.py``;
* ``scenario_fts`` — every job-failure / app-recovery runbook scenario,
  refreshed by the backfill ("哪个 runbook 提到过 kerberos"类问题).

CJK note: FTS5's unicode61 tokenizer doesn't segment Chinese, so both the
indexed text and the queries are preprocessed into character bigrams
(``_cjk_ngrams``) — standard zero-dependency trick, good enough for runbook /
resolution lookup.

RBAC stays with the caller: this module returns candidate ids + snippets; the
tool layer re-filters them through the caller's issue scope before anything
reaches the model.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional

from core.config import get_config
from core.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_PATH = "data/agent_fts.sqlite3"
_fts_disabled = False  # set when this sqlite build lacks FTS5

# Bump when the virtual-table layout changes: the sidecar is fully
# rebuildable from Postgres + the KB Markdown (startup backfill), so migration
# = drop & recreate, gated on PRAGMA user_version.
_SCHEMA_VERSION = 3


def _fts_path() -> Path:
    raw = getattr(get_config(), "_raw", {}) or {}
    agent_cfg = raw.get("agent") if isinstance(raw, dict) else None
    configured = (agent_cfg or {}).get("fts_index_path") if isinstance(agent_cfg, dict) else None
    path = Path(configured or _DEFAULT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect(path: Optional[Path] = None) -> Optional[sqlite3.Connection]:
    global _fts_disabled
    if _fts_disabled:
        return None
    conn = sqlite3.connect(str(path or _fts_path()))
    try:
        ensure_schema(conn)
        return conn
    except sqlite3.OperationalError as exc:
        _fts_disabled = True
        conn.close()
        logger.warning("agent retrieval disabled — sqlite without FTS5: {}", exc)
        return None


def ensure_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == _SCHEMA_VERSION:
        return
    # Layout changed (or fresh file): rebuild from scratch — the startup
    # backfill repopulates everything from Postgres.
    conn.execute("DROP TABLE IF EXISTS issue_fts")
    conn.execute("DROP TABLE IF EXISTS scenario_fts")
    # *_display columns carry the original text for output; the indexed
    # columns hold the CJK-bigram-expanded variant used for matching.
    # Label columns (scenario_type / resolution_kind / escalated /
    # signatures) drive the tiered structured retrieval (plan §6.2 P2).
    conn.execute(
        "CREATE VIRTUAL TABLE issue_fts USING fts5("
        "  issue_id UNINDEXED, issue_type, job_id UNINDEXED, app_id UNINDEXED,"
        "  product_id UNINDEXED, resolved_at UNINDEXED,"
        "  scenario_type UNINDEXED, resolution_kind UNINDEXED,"
        "  escalated UNINDEXED, signatures UNINDEXED,"
        "  title, description, resolution,"
        "  title_display UNINDEXED, resolution_display UNINDEXED"
        ")"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE scenario_fts USING fts5("
        "  scenario_id UNINDEXED, kind UNINDEXED, entity_id UNINDEXED,"
        "  scenario_type, scenario_name, condition, steps, escalation_target UNINDEXED,"
        "  name_display UNINDEXED, condition_display UNINDEXED, steps_display UNINDEXED"
        ")"
    )
    # KB (guide knowledge base) chunks parsed from docs/kb/*.md. ``tab`` is
    # indexed so a MATCH query can be tab-scoped (search_kb); *_display carry
    # the original text for output, title/body hold the CJK-bigram variant.
    conn.execute("DROP TABLE IF EXISTS kb_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE kb_fts USING fts5("
        "  tab, section UNINDEXED, sub_view UNINDEXED,"
        "  title, body,"
        "  title_display UNINDEXED, body_display UNINDEXED"
        ")"
    )
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()


# ── CJK-aware text preparation ────────────────────────────────────────

_CJK_RUN = re.compile(r"[一-鿿]{2,}")


def _cjk_ngrams(text: str) -> str:
    """Expand runs of Chinese into space-separated bigrams so unicode61 can
    match them; non-CJK text passes through untouched."""
    def _expand(m: re.Match) -> str:
        s = m.group(0)
        return " ".join(s[i:i + 2] for i in range(len(s) - 1))

    return _CJK_RUN.sub(_expand, text or "")


def _match_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query (quoted tokens only)."""
    prepared = _cjk_ngrams(text)
    tokens = re.findall(r"[\w一-鿿][\w一-鿿\-.]*", prepared)
    tokens = [t for t in tokens if len(t) >= 2][:24]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


# ── indexing ──────────────────────────────────────────────────────────


def _issue_labels(issue) -> dict:
    """Deterministic retrieval labels (plan §6.2 P2). getattr-tolerant so
    detached rows / test doubles index fine."""
    from core.agent.knowledge import extract_failure_signatures

    status = getattr(issue, "status", "") or ""
    summary = getattr(issue, "action_summary_json", None)
    signatures = extract_failure_signatures(
        f"{issue.title or ''} {(issue.description or '')[:4000]}")
    return {
        "scenario_type": getattr(issue, "selected_scenario_type", "") or "",
        "resolution_kind": "false_positive" if status == "false_positive" else "resolve",
        "escalated": 1 if isinstance(summary, dict) and summary.get("escalations") else 0,
        # Comma-fenced so a LIKE '%,sig,%' filter can't partial-match labels.
        "signatures": ("," + ",".join(signatures) + ",") if signatures else "",
    }


def index_issue(issue, conn: Optional[sqlite3.Connection] = None) -> bool:
    """Upsert one resolved issue. Best-effort: failures only log."""
    if not (issue.resolution_description or "").strip():
        return False
    own = conn is None
    conn = conn or _connect()
    if conn is None:
        return False
    try:
        labels = _issue_labels(issue)
        conn.execute("DELETE FROM issue_fts WHERE issue_id = ?", (issue.id,))
        conn.execute(
            "INSERT INTO issue_fts (issue_id, issue_type, job_id, app_id, product_id,"
            " resolved_at, scenario_type, resolution_kind, escalated, signatures,"
            " title, description, resolution, title_display, resolution_display)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                issue.id, issue.type, issue.job_id or 0, issue.app_id or 0,
                issue.product_id or 0,
                issue.resolved_at.isoformat() if issue.resolved_at else "",
                labels["scenario_type"], labels["resolution_kind"],
                labels["escalated"], labels["signatures"],
                _cjk_ngrams(issue.title or ""),
                _cjk_ngrams((issue.description or "")[:4000]),
                _cjk_ngrams((issue.resolution_description or "")[:4000]),
                issue.title or "",
                (issue.resolution_description or "")[:2000],
            ),
        )
        conn.commit()
        return True
    except Exception:
        logger.opt(exception=True).warning("issue_fts index failed for issue {}", issue.id)
        return False
    finally:
        if own:
            conn.close()


def index_scenarios(rows: list, conn: Optional[sqlite3.Connection] = None) -> int:
    """Replace the scenario index. rows: (scenario_id, kind, entity_id,
    scenario_type, scenario_name, condition, steps_text, escalation_target)."""
    own = conn is None
    conn = conn or _connect()
    if conn is None:
        return 0
    try:
        conn.execute("DELETE FROM scenario_fts")
        conn.executemany(
            "INSERT INTO scenario_fts (scenario_id, kind, entity_id, scenario_type,"
            " scenario_name, condition, steps, escalation_target,"
            " name_display, condition_display, steps_display)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, kind, eid, stype, _cjk_ngrams(name), _cjk_ngrams(cond),
              _cjk_ngrams(steps), target, name, cond[:500], steps[:800])
             for sid, kind, eid, stype, name, cond, steps, target in rows],
        )
        conn.commit()
        return len(rows)
    except Exception:
        logger.opt(exception=True).warning("scenario_fts rebuild failed")
        return 0
    finally:
        if own:
            conn.close()


# ── search ────────────────────────────────────────────────────────────


def search_resolutions(query: str, *, issue_type: str = "", limit: int = 10,
                       conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """BM25-ranked resolution snippets. Caller must RBAC-filter by issue_id."""
    match = _match_query(query)
    if not match:
        return []
    own = conn is None
    conn = conn or _connect()
    if conn is None:
        return []
    try:
        sql = (
            "SELECT issue_id, issue_type, job_id, app_id, product_id, resolved_at,"
            " title_display, resolution_display, bm25(issue_fts) AS score"
            " FROM issue_fts WHERE issue_fts MATCH ?"
        )
        args: list = [match]
        if issue_type:
            sql += " AND issue_type = ?"
            args.append(issue_type)
        sql += " ORDER BY score LIMIT ?"
        args.append(max(1, limit))
        out = []
        for row in conn.execute(sql, args):
            out.append({
                "issue_id": row[0], "issue_type": row[1], "job_id": row[2],
                "app_id": row[3], "product_id": row[4], "resolved_at": row[5],
                "title": row[6], "resolution": row[7][:1000], "score": row[8],
            })
        return out
    except Exception:
        logger.opt(exception=True).warning("issue_fts search failed")
        return []
    finally:
        if own:
            conn.close()


def search_runbooks(query: str, *, limit: int = 10,
                    conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    match = _match_query(query)
    if not match:
        return []
    own = conn is None
    conn = conn or _connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT scenario_id, kind, entity_id, scenario_type, name_display,"
            " condition_display, steps_display, escalation_target, bm25(scenario_fts) AS score"
            " FROM scenario_fts WHERE scenario_fts MATCH ? ORDER BY score LIMIT ?",
            (match, max(1, limit)),
        ).fetchall()
        return [{
            "scenario_id": r[0], "kind": r[1], "entity_id": r[2],
            "scenario_type": r[3], "scenario_name": r[4],
            "condition": r[5][:500], "steps": r[6][:800],
            "escalation_target": r[7], "score": r[8],
        } for r in rows]
    except Exception:
        logger.opt(exception=True).warning("scenario_fts search failed")
        return []
    finally:
        if own:
            conn.close()


# ── KB (guide knowledge base) indexing + search ──────────────────────


def reindex_kb(conn: Optional[sqlite3.Connection] = None) -> int:
    """Full replace of the KB index from ``docs/kb/*.md`` (via kb_loader).
    Idempotent; safe to run on every startup. Returns chunks indexed."""
    from core.agent import kb_loader

    own = conn is None
    conn = conn or _connect()
    if conn is None:
        return 0
    try:
        chunks = kb_loader.searchable_chunks()
        conn.execute("DELETE FROM kb_fts")
        conn.executemany(
            "INSERT INTO kb_fts (tab, section, sub_view, title, body,"
            " title_display, body_display) VALUES (?,?,?,?,?,?,?)",
            [(c.tab, c.section, c.sub_view or "",
              _cjk_ngrams(c.title), _cjk_ngrams(c.text),
              c.title, c.text[:4000]) for c in chunks],
        )
        conn.commit()
        return len(chunks)
    except Exception:
        logger.opt(exception=True).warning("kb_fts reindex failed")
        return 0
    finally:
        if own:
            conn.close()


def search_kb(query: str, *, tab: str = "", limit: int = 8,
              conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """BM25-ranked KB chunks for a guide question. ``tab`` scopes the search to
    one page (the page assistant passes the current tab); empty = all pages."""
    match = _match_query(query)
    if not match:
        return []
    own = conn is None
    conn = conn or _connect()
    if conn is None:
        return []
    try:
        sql = (
            "SELECT tab, section, sub_view, title_display, body_display,"
            " bm25(kb_fts) AS score FROM kb_fts WHERE kb_fts MATCH ?"
        )
        args: list = [match]
        if tab:
            sql += " AND tab = ?"
            args.append(tab)
        sql += " ORDER BY score LIMIT ?"
        args.append(max(1, limit))
        return [{
            "tab": r[0], "section": r[1], "sub_view": r[2] or None,
            "title": r[3], "text": (r[4] or "")[:1500], "score": r[5],
        } for r in conn.execute(sql, args)]
    except Exception:
        logger.opt(exception=True).warning("kb_fts search failed")
        return []
    finally:
        if own:
            conn.close()


# ── tiered similar-issue retrieval for the diagnosis pipeline ────────
# Plan §6.2 P2: filter first (labels), rank second (BM25 within candidates).
# Tier ladder: same job → same app → same scenario type → same failure
# signature → same issue type; each tier widens only while results < limit.
# Within a tier, real resolutions outrank false-positive dismissals, then
# BM25 text relevance, then recency.


def _filter_rows(conn: sqlite3.Connection, filters: dict, *, exclude_id: int,
                 limit: int) -> list[dict]:
    clauses, args = ["issue_id != ?"], [exclude_id]
    for key in ("job_id", "app_id", "issue_type", "scenario_type"):
        if filters.get(key):
            clauses.append(f"{key} = ?")
            args.append(filters[key])
    if filters.get("signature"):
        clauses.append("signatures LIKE ?")
        args.append(f"%,{filters['signature']},%")
    args.append(max(1, limit))
    rows = conn.execute(
        "SELECT issue_id, issue_type, job_id, app_id, product_id, resolved_at,"
        " scenario_type, resolution_kind, escalated, title_display, resolution_display"
        f" FROM issue_fts WHERE {' AND '.join(clauses)}"
        " ORDER BY resolved_at DESC LIMIT ?",
        args,
    ).fetchall()
    return [{
        "issue_id": r[0], "issue_type": r[1], "job_id": r[2], "app_id": r[3],
        "product_id": r[4], "resolved_at": r[5], "scenario_type": r[6],
        "resolution_kind": r[7], "escalated": bool(r[8]),
        "title": r[9], "resolution": r[10][:1000],
    } for r in rows]


def retrieve_similar(session, issue, limit: int = 5) -> list[dict]:
    """Empty list → caller falls back to the structured-only ranking."""
    from core.agent.knowledge import extract_failure_signatures

    conn = _connect()
    if conn is None:
        return []
    try:
        query_text = f"{issue.title or ''} {(issue.description or '')[:500]}"
        text_scores = {h["issue_id"]: h["score"]
                       for h in search_resolutions(query_text, limit=50, conn=conn)}
        signatures = extract_failure_signatures(query_text)

        tiers: list[tuple[str, dict]] = []
        if issue.job_id:
            tiers.append(("同一 Job", {"job_id": issue.job_id}))
        if issue.app_id:
            tiers.append(("同一 App", {"app_id": issue.app_id}))
        scenario = getattr(issue, "selected_scenario_type", "") or ""
        if scenario:
            tiers.append((f"同场景类型({scenario})", {"scenario_type": scenario}))
        for sig in signatures:
            tiers.append((f"同错误签名({sig})", {"signature": sig}))
        tiers.append(("同类型", {"issue_type": issue.type}))

        out: list[dict] = []
        seen: set = set()
        for reason, filters in tiers:
            if len(out) >= limit:
                break
            candidates = _filter_rows(conn, filters, exclude_id=issue.id, limit=limit * 3)
            # Stable sort over the recency-ordered candidates: dismissals
            # last, then BM25 (lower = better; non-hits score 0 > any hit).
            candidates.sort(key=lambda h: (
                h["resolution_kind"] == "false_positive",
                text_scores.get(h["issue_id"], 0.0),
            ))
            for h in candidates:
                if h["issue_id"] in seen or len(out) >= limit:
                    continue
                seen.add(h["issue_id"])
                reasons = [reason]
                if h["issue_id"] in text_scores:
                    reasons.append("文本相似(BM25)")
                if h["resolution_kind"] == "false_positive":
                    reasons.append("注意：该记录是误报关单")
                out.append({
                    "issue_id": h["issue_id"], "type": h["issue_type"],
                    "title": h["title"], "resolved_at": h["resolved_at"] or None,
                    "resolution": h["resolution"],
                    "resolution_kind": h["resolution_kind"],
                    "match_reason": " + ".join(reasons),
                })
        return out
    finally:
        conn.close()
