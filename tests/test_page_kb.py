"""KB pipeline drift + retrieval tests (plan §1, fuzzy-leaping-firefly).

Guards that the structured KB (docs/kb/*.md) stays honest against the fact
layer: every documented page is a real tab, every documented button is a real
UI control, coach/faq are well-formed, and the FTS index actually retrieves.
No LLM. Uses the FTS5 sidecar (skips if this sqlite lacks FTS5).
"""
import pytest

from core.agent import kb_loader, page_kb, retrieval
from core.agent.kb_facts import known_buttons
from core.agent.ui_capability import TAB_VISIBILITY
from core.models.user import UserRole


# ── drift: KB pages ↔ real tabs ──────────────────────────────────────────


def test_kb_pages_cover_exactly_known_tabs():
    kb_loader.clear_cache()
    kb_tabs = {p.tab for p in kb_loader.load_pages()}
    assert kb_tabs == set(TAB_VISIBILITY)


def test_every_page_has_purpose_and_coach_default():
    for page in kb_loader.load_pages():
        assert page.section("purpose") and page.section("purpose").text, f"{page.tab}: no purpose"
        assert page.section("coach", "default"), f"{page.tab}: no coach:default"


def test_every_page_has_faq_questions():
    for page in kb_loader.load_pages():
        roles = TAB_VISIBILITY[page.tab]
        role = next(iter(roles))
        assert page_kb.page_faqs(role, page.tab), f"{page.tab}: no FAQ questions"


def test_coach_sub_views_are_declared():
    # A coach:<sub_view> must be either 'default' or a declared sub_view.
    for page in kb_loader.load_pages():
        allowed = set(page.sub_views) | {"default", None}
        for c in page.sections("coach"):
            assert c.sub_view in allowed, f"{page.tab}: coach '{c.sub_view}' not declared"


def test_documented_buttons_are_real_controls():
    # When a tab's buttons are inventoried in kb_facts, every KB button label
    # must be one of them (empty inventory = not yet catalogued → skipped).
    for page in kb_loader.load_pages():
        inv = {b.lower() for b in known_buttons(page.tab)}
        if not inv:
            continue
        btns = page.section("buttons")
        if not btns:
            continue
        for label in kb_loader.extract_button_labels(btns.text):
            assert label.lower() in inv, f"{page.tab}: button '{label}' not in kb_facts"


# ── retrieval ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def kb_index():
    if retrieval._connect() is None:  # FTS5 missing on this sqlite build
        pytest.skip("FTS5 unavailable")
    retrieval.reindex_kb()


def test_search_kb_finds_workbench_record_step(kb_index):
    hits = retrieval.search_kb("how do I record a handling step", tab="workbench", limit=5)
    assert hits and all(h["tab"] == "workbench" for h in hits)


def test_search_kb_tab_scope_filters(kb_index):
    hits = retrieval.search_kb("run verification", tab="verification", limit=5)
    assert hits and all(h["tab"] == "verification" for h in hits)


# ── role gating ────────────────────────────────────────────────────────────


def test_regular_user_gets_no_relayops_only_coach():
    # Role gating still hides coach for pages a role can't see. my-schedule is
    # Ops-only, so a regular user gets no coach for it. (My Actions / Open Issues
    # / workbench are now visible to all, so they're no longer the gated case.)
    assert page_kb.page_coach(UserRole.REGULAR_USER, "my-schedule") == []


def test_relayops_member_gets_workbench_coach():
    assert page_kb.page_coach(UserRole.RELAYOPS_MEMBER, "workbench", "page2")


def test_page_overview_workbench_has_sub_view_flow():
    ov = page_kb.page_overview("workbench", "page2")
    assert ov and "Record" in ov
