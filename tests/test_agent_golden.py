"""Golden-question harness for the chat assistant (integration, opt-in).

Needs a live LLM gateway and a populated DB, so it never runs in plain CI:

    RELAYOPS_GOLDEN=1 python -m pytest tests/test_agent_golden.py -q

Assertions are deterministic (tool selection / answer keypoints); the prose
quality is reviewed by a human reading the printed transcript (-s).
"""

import os
from pathlib import Path

import pytest
import yaml

GOLDEN = Path(__file__).parent / "agent_golden" / "cases.yaml"

pytestmark = pytest.mark.skipif(
    os.environ.get("RELAYOPS_GOLDEN") != "1",
    reason="golden run needs a live LLM + DB; set RELAYOPS_GOLDEN=1 to enable",
)


def _cases():
    return yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))


def _actor():
    from core.auth.jwt import CurrentUser
    from core.models.user import UserRole

    return CurrentUser(username=os.environ.get("RELAYOPS_GOLDEN_USER", "admin"),
                       user_id=int(os.environ.get("RELAYOPS_GOLDEN_USER_ID", "1")),
                       role=UserRole.ADMIN)


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["id"])
def test_golden_case(case):
    from core.agent.chat import chat_available, run_turn

    available, reason = chat_available()
    if not available:
        pytest.skip(f"LLM unavailable: {reason}")

    events = list(run_turn(_actor(), case["question"]))
    called = [e["data"]["name"] for e in events if e["event"] == "tool_call"]
    answer = next((e["data"]["text"] for e in events if e["event"] == "answer"), "")
    errors = [e["data"]["message"] for e in events if e["event"] == "error"]

    print(f"\n[{case['id']}] tools={called}\n{answer}\n")

    assert not errors, errors
    assert answer.strip(), "empty answer"
    for tool in case.get("expected_tools", []):
        assert tool in called, f"expected tool {tool} not called (called: {called})"
    for tool in case.get("forbid_tools", []):
        assert tool not in called, f"forbidden tool {tool} was called"
    for needle in case.get("answer_contains", []):
        assert needle in answer, f"answer missing keypoint {needle!r}"
    # Anti-sycophancy / no-fabrication guards: none of these may appear.
    for banned in case.get("forbid_substrings", []):
        assert banned not in answer, f"answer must not contain {banned!r}"
    # "At least one of" — useful when several phrasings are acceptable.
    any_of = case.get("answer_contains_any", [])
    if any_of:
        assert any(n in answer for n in any_of), \
            f"answer missing all of {any_of!r}"
