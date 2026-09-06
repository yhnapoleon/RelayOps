"""S4.5–4.8 — guide branch: routing, guide_step shape, T2 deeplink boundary,
and deterministic assist_fill (P7).

No real LLM: classification is keyword-deterministic; the guide tools are plain
functions exercised directly; assist_fill is checked against the onboarding
setter with the DB stubbed.
"""
import pytest

from core.agent import assistant, guide
from core.agent.schemas import ClarificationAnswer, OnboardingDraftPayload
from core.agent.validate import apply_answers
from core.auth.jwt import CurrentUser
from core.models.user import UserRole


def _actor(role=UserRole.RELAYOPS_MEMBER):
    return CurrentUser(username="dora", user_id=42, role=role)


def _find(tools, name):
    return next(t for t in tools if t.name == name)


# ── routing ──────────────────────────────────────────────────────────────


def test_classify_guide_keyword():
    assert assistant.classify_intent("教我怎么用这个平台", _actor()) == ("guide", "keyword")
    assert assistant.classify_intent("how do i use the schedules page", _actor()) == ("guide", "keyword")


def test_guide_does_not_steal_a_change_request():
    # Write is mode-only now: a typed change request must NOT route to guide
    # (it stays read-only qa, which nudges the user to the Write-mode toggle).
    intent, _ = assistant.classify_intent("把 #5 标记为误报", _actor())
    assert intent != "guide"
    assert assistant.classify_intent("把 #5 标记为误报", _actor(), mode="write") == ("write", "manual:write")


# ── guide_step: offer tabs ────────────────────────────────────────────────


def test_guide_offer_tabs_pushes_options():
    sink: list = []
    tools = guide._build_guide_tools(_actor(), draft_ref=None, guide_sink=sink)
    _find(tools, "guide_offer_tabs").invoke({})
    assert len(sink) == 1
    step = sink[0]
    assert step["_kind"] == "guide_step"
    keys = {o["key"] for o in step["options"]}
    # relayops_member sees actions/verification but not the admin panel.
    assert "actions" in keys and "relayops-admin-users" not in keys
    assert step["prompt"]


# ── T2 deeplink boundary (P14): guide, never a confirm_token ──────────────


def test_high_risk_action_deeplink_has_no_confirm_token():
    sink: list = []
    tools = guide._build_guide_tools(_actor(UserRole.ADMIN), draft_ref=None, guide_sink=sink)
    _find(tools, "guide_high_risk_action").invoke({"action_key": "transfer_owner"})
    step = sink[0]
    assert step["_kind"] == "guide_step"
    assert step["deeplink"]["tab"] == "projects"
    # The whole point of T2: a jump, not an executable token.
    assert "confirm_token" not in step
    assert "token" not in step.get("deeplink", {})


def test_high_risk_unknown_action_errors():
    sink: list = []
    tools = guide._build_guide_tools(_actor(), draft_ref=None, guide_sink=sink)
    out = _find(tools, "guide_high_risk_action").invoke({"action_key": "launch_missiles"})
    assert "error" in out and not sink


def test_all_deeplinks_target_real_tabs():
    from core.agent.ui_capability import TAB_VISIBILITY

    for spec in guide.GUIDE_DEEPLINKS.values():
        assert spec["tab"] in TAB_VISIBILITY


# ── assist_fill determinism (S4.7 / P7) ───────────────────────────────────


def test_assist_fill_only_touches_named_field():
    # P7: the onboarding setter writes the named field_path and nothing else.
    payload = OnboardingDraftPayload.model_validate({
        "project": {"name": "Keep", "description": "untouched"},
        "products": [{"name": "P", "apps": [{"cml_application_name": "A", "application_url": ""}]}],
    })
    apply_answers(payload, [ClarificationAnswer(
        field_path="products[0].apps[0].application_url", answer="https://x")])
    assert payload.products[0].apps[0].application_url == "https://x"
    # Everything else is byte-for-byte unchanged.
    assert payload.project.name == "Keep" and payload.project.description == "untouched"
    assert payload.products[0].apps[0].cml_application_name == "A"


def test_assist_fill_bad_field_path_raises():
    # A path that can't land on a real leaf must raise, never silently no-op or
    # write the wrong field (P7).
    payload = OnboardingDraftPayload.model_validate({"project": {"name": "X"}})
    with pytest.raises((ValueError, AttributeError)):
        apply_answers(payload, [ClarificationAnswer(field_path="project.nope", answer="v")])


def test_assist_fill_draft_passes_answers_to_setter(monkeypatch):
    import core.agent.draft_service as draft_service
    import core.models.database as db_module

    captured = {}

    class _Draft:
        id = 9
        status = "ready"
        payload = {"project": {"name": "X"}}

    monkeypatch.setattr(db_module, "get_db", lambda: object())
    monkeypatch.setattr(draft_service, "get_draft", lambda db, did, *, actor: _Draft())

    def _save(db, did, *, actor, payload, answers):
        captured["payload"] = payload
        captured["answers"] = answers
        return _Draft()

    monkeypatch.setattr(draft_service, "save_draft", _save)

    out = guide.assist_fill_draft(_actor(), 9, [{"field_path": "project.name", "answer": "New"}])
    assert out["filled"] == ["project.name"] and out["draft_id"] == 9
    # Current payload preserved; answers carried as ClarificationAnswer.
    assert captured["payload"] == {"project": {"name": "X"}}
    assert captured["answers"][0].field_path == "project.name"


def test_assist_fill_tool_errors_without_draft():
    sink: list = []
    tools = guide._build_guide_tools(_actor(), draft_ref=None, guide_sink=sink)
    out = _find(tools, "assist_fill").invoke({"field_path": "project.name", "answer": "v"})
    assert "error" in out and not sink
