"""Page KB API — the guide branch's + page assistant's view over the KB.

Thin, role-aware wrapper on ``kb_loader`` (the parsed ``docs/kb/*.md`` chunks):

* :func:`page_overview` — compact purpose + layout + flow digest for one tab,
  used as a grounding preamble (guide turn / page assistant) so the model
  answers from real page facts and reaches for ``kb_search`` for detail.
* :func:`page_coach` — the auto-shown bubble copy for a tab (+ optional
  sub-view), served to the floating assistant.
* :func:`page_faqs` — the preset FAQ questions shown as chips.

Role gating reuses ``ui_capability.TAB_VISIBILITY`` / ``tabs_for_role`` so the
KB never surfaces a page the user can't see.
"""
from __future__ import annotations

import re
from typing import List, Optional

from core.agent import kb_loader
from core.agent.ui_capability import TAB_VISIBILITY, tabs_for_role
from core.models.user import normalize_role


def _visible(role: Optional[str], tab: str) -> bool:
    roles = TAB_VISIBILITY.get(tab)
    return bool(roles) and normalize_role(role) in roles


def page_title(tab: str) -> Optional[str]:
    page = kb_loader.load_page(tab)
    return page.title if page else None


def page_overview(tab: str, sub_view: Optional[str] = None, *, max_chars: int = 1800) -> Optional[str]:
    """Compact grounding block for a tab: purpose + layout + the relevant flow
    (the sub_view's, else all). Returns None when the tab has no KB page."""
    page = kb_loader.load_page(tab)
    if page is None:
        return None
    parts: List[str] = [f"# {page.title} (tab: {page.tab})"]

    purpose = page.section("purpose")
    if purpose and purpose.text:
        parts.append("## Purpose\n" + purpose.text)
    layout = page.section("layout")
    if layout and layout.text:
        parts.append("## Layout\n" + layout.text)

    flows = page.sections("flow")
    if sub_view:
        flows = [c for c in flows if c.sub_view == sub_view] or flows
    for f in flows:
        head = f"## Flow ({f.sub_view})" if f.sub_view else "## Flow"
        if f.text:
            parts.append(f"{head}\n{f.text}")

    text = "\n\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…"
    return text or None


def _bubbles(text: str) -> List[str]:
    """Split coach copy into individual bubbles on blank lines; a leading
    ``- ``/``* `` bullet is stripped so authors can use either style."""
    out: List[str] = []
    for para in re.split(r"\n\s*\n", text or ""):
        line = para.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        out.append(line)
    return out


def page_coach(role: Optional[str], tab: str, sub_view: Optional[str] = None) -> List[str]:
    """Bubble copy for a page (+ sub-view). Falls back to the ``default``
    coach section when the sub-view has none. Empty when not visible/no KB."""
    if not _visible(role, tab):
        return []
    page = kb_loader.load_page(tab)
    if page is None:
        return []
    chunk = None
    if sub_view:
        chunk = page.section("coach", sub_view)
    if chunk is None:
        chunk = page.section("coach", "default") or page.section("coach", None)
    return _bubbles(chunk.text) if chunk else []


_FAQ_Q = re.compile(r"^\s*[-*]\s*Q[:：]\s*(.+?)\s*$", re.IGNORECASE)


def page_faqs(role: Optional[str], tab: str, *, limit: int = 6) -> List[dict]:
    """Preset FAQ questions for a tab, as ``[{"q": ...}]`` (answers come from a
    live guide turn). Empty when the page isn't visible to the role."""
    if not _visible(role, tab):
        return []
    page = kb_loader.load_page(tab)
    if page is None:
        return []
    faq = page.section("faq")
    if faq is None:
        return []
    out: List[dict] = []
    for line in faq.text.splitlines():
        m = _FAQ_Q.match(line)
        if m:
            out.append({"q": m.group(1).strip()})
        if len(out) >= limit:
            break
    return out


def page_help(role: Optional[str], tab: str, sub_view: Optional[str] = None) -> dict:
    """Everything the floating assistant needs on tab switch (no LLM)."""
    return {
        "tab": tab,
        "title": page_title(tab) or tab,
        "coach": page_coach(role, tab, sub_view),
        "faqs": page_faqs(role, tab),
    }
