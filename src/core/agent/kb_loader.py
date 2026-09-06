"""KB loader — parse the structured guide knowledge base under ``docs/kb/``.

The KB is the single narrative source for the guide branch and the page
assistant (design: fuzzy-leaping-firefly plan §1.2). One Markdown file per UI
tab; a lightweight, human-authored, three-source-grounded format:

    ---
    tab: workbench
    title: Issue Workbench
    sub_views: [page1, page2]
    ---
    ## purpose
    ...
    ## layout
    ...
    ## buttons
    ...
    ## flow: page1
    ...
    ## coach:default
    ...
    ## faq
    - Q: ...
      A: ...

Parsing rules (deliberately simple, no Markdown AST):
* YAML front-matter (``tab`` required; ``title`` / ``sub_views`` optional).
* Each ``## heading`` starts a section = one indexable chunk. A heading may
  carry a sub-view after a colon (``flow: page1`` / ``coach:default``) →
  ``section='flow'``, ``sub_view='page1'``. No colon → ``sub_view=None``.

The result is a flat list of chunks the FTS indexer (``retrieval.reindex_kb``)
and the KB API (``page_kb``) both read. Loading is cached; call
:func:`clear_cache` after editing a file in a long-lived process.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

import yaml

from core.logging import get_logger

logger = get_logger(__name__)

# Section kinds worth putting in the BM25 index. ``coach`` is short UI copy
# served verbatim to the bubble (never a search target); everything else is
# real explanatory knowledge a "how/where/what" question should retrieve.
SEARCHABLE_SECTIONS = frozenset({"purpose", "layout", "buttons", "flow", "faq"})

_HEADING = re.compile(r"^##\s+(.*)$")
_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class KBChunk:
    tab: str
    page_title: str
    section: str          # purpose / layout / buttons / flow / faq / coach
    sub_view: Optional[str]  # e.g. 'page1', 'default'; None when absent
    heading: str          # raw heading text after '## '
    text: str             # section body (stripped)

    @property
    def title(self) -> str:
        """Human title for the chunk (used as the FTS display title)."""
        if self.sub_view:
            return f"{self.page_title} · {self.section} ({self.sub_view})"
        return f"{self.page_title} · {self.section}"


@dataclass(frozen=True)
class KBPage:
    tab: str
    title: str
    sub_views: tuple
    chunks: tuple  # tuple[KBChunk, ...]

    def section(self, name: str, sub_view: Optional[str] = None) -> Optional[KBChunk]:
        for c in self.chunks:
            if c.section == name and c.sub_view == sub_view:
                return c
        return None

    def sections(self, name: str) -> List[KBChunk]:
        return [c for c in self.chunks if c.section == name]


def kb_dir() -> Path:
    """Directory holding the KB Markdown files. Config override
    ``agent.kb_docs_path`` (relative to CWD or absolute); else ``docs/kb``
    resolved from this module's location so it works regardless of CWD."""
    try:
        from core.config import get_config

        raw = getattr(get_config(), "_raw", {}) or {}
        agent_cfg = raw.get("agent") if isinstance(raw, dict) else None
        configured = (agent_cfg or {}).get("kb_docs_path") if isinstance(agent_cfg, dict) else None
        if configured:
            return Path(configured)
    except Exception:
        pass
    # src/core/agent/kb_loader.py → parents[3] == repo root
    return Path(__file__).resolve().parents[3] / "docs" / "kb"


def _parse_heading(raw: str) -> tuple[str, Optional[str]]:
    """'flow: page1' -> ('flow', 'page1'); 'coach:default' -> ('coach',
    'default'); 'purpose' -> ('purpose', None)."""
    if ":" in raw:
        left, right = raw.split(":", 1)
        section = left.strip().lower()
        sub = right.strip() or None
        return section, sub
    return raw.strip().lower(), None


def _parse_file(path: Path) -> Optional[KBPage]:
    text = path.read_text(encoding="utf-8")
    m = _FRONT_MATTER.match(text)
    if not m:
        logger.warning("kb: {} has no front-matter — skipped", path.name)
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        logger.warning("kb: {} bad front-matter: {}", path.name, exc)
        return None
    tab = str(meta.get("tab") or "").strip()
    if not tab:
        logger.warning("kb: {} missing 'tab' — skipped", path.name)
        return None
    page_title = str(meta.get("title") or tab).strip()
    sub_views = tuple(str(s).strip() for s in (meta.get("sub_views") or []))

    body = text[m.end():]
    chunks: List[KBChunk] = []
    cur_heading: Optional[str] = None
    buf: List[str] = []

    def _flush():
        if cur_heading is None:
            return
        section, sub_view = _parse_heading(cur_heading)
        chunks.append(KBChunk(
            tab=tab, page_title=page_title, section=section, sub_view=sub_view,
            heading=cur_heading, text="\n".join(buf).strip(),
        ))

    for line in body.splitlines():
        hm = _HEADING.match(line)
        if hm:
            _flush()
            cur_heading = hm.group(1).strip()
            buf = []
        elif cur_heading is not None:
            buf.append(line)
    _flush()

    return KBPage(tab=tab, title=page_title, sub_views=sub_views, chunks=tuple(chunks))


@lru_cache(maxsize=1)
def load_pages() -> tuple:
    """All KB pages, parsed and cached. Returns ``tuple[KBPage, ...]``."""
    d = kb_dir()
    if not d.is_dir():
        logger.warning("kb: directory {} not found — no KB loaded", d)
        return ()
    pages: List[KBPage] = []
    for path in sorted(d.glob("*.md")):
        if path.name.startswith("_"):  # _inventory / _templates etc. are not pages
            continue
        page = _parse_file(path)
        if page is not None:
            pages.append(page)
    return tuple(pages)


def load_page(tab: str) -> Optional[KBPage]:
    for page in load_pages():
        if page.tab == tab:
            return page
    return None


def all_chunks() -> List[KBChunk]:
    out: List[KBChunk] = []
    for page in load_pages():
        out.extend(page.chunks)
    return out


def searchable_chunks() -> List[KBChunk]:
    """Chunks that belong in the BM25 index (everything but ``coach``)."""
    return [c for c in all_chunks() if c.section in SEARCHABLE_SECTIONS and c.text]


_BUTTON_LABEL = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*", re.MULTILINE)


def extract_button_labels(text: str) -> List[str]:
    """Bolded button labels from a ``## buttons`` section body. Convention:
    each button is a list item ``- **<label>** — <what it does>``. Used by the
    drift test to assert every documented button is a real UI control."""
    return [m.group(1).strip() for m in _BUTTON_LABEL.finditer(text or "")]


def clear_cache() -> None:
    load_pages.cache_clear()
