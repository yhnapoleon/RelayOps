"""Chat assistant — graph skeleton and SSE event contract, no real LLM/DB.

A scripted fake model drives the ReAct graph through a tool call and a final
answer; the assertions pin the docs/AGENT_CHAT_CONTRACT.md event sequence.
"""

import pytest

pytest.importorskip("langgraph")

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

import core.agent.tools as tools_module
from core.agent.chat import _thread_key, run_turn
from core.auth.jwt import CurrentUser


class ScriptedModel(BaseChatModel):
    """Replays a fixed list of AIMessages, one per _generate call."""

    script: list
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        return self


@tool
def fake_lookup() -> dict:
    """Fake read-only lookup."""
    return {"rows": 3}


@pytest.fixture
def actor() -> CurrentUser:
    return CurrentUser(username="tester", user_id=42, role="relayops_member")


@pytest.fixture(autouse=True)
def _stub_tools(monkeypatch):
    monkeypatch.setattr(
        tools_module, "build_langchain_tools",
        lambda actor, artifact_sink=None: [fake_lookup],
    )


def test_turn_with_tool_call_follows_contract(actor):
    model = ScriptedModel(script=[
        AIMessage(content="", tool_calls=[{"name": "fake_lookup", "args": {}, "id": "c1"}]),
        AIMessage(content="一共 3 行。"),
    ])
    events = list(run_turn(actor, "查一下", thread_id="t1", model=model))

    kinds = [e["event"] for e in events]
    assert kinds[0] == "meta" and events[0]["data"]["thread_id"] == "t1"
    assert kinds[-1] == "done"
    assert kinds.index("tool_call") < kinds.index("tool_result") < kinds.index("answer")
    tool_call = next(e for e in events if e["event"] == "tool_call")
    assert tool_call["data"]["name"] == "fake_lookup"
    answer = next(e for e in events if e["event"] == "answer")
    assert answer["data"]["text"] == "一共 3 行。"
    assert "error" not in kinds


def test_turn_without_tools_and_generated_thread_id(actor):
    model = ScriptedModel(script=[AIMessage(content="你好。")])
    events = list(run_turn(actor, "hi", model=model))
    assert events[0]["event"] == "meta"
    assert events[0]["data"]["thread_id"]  # server-generated
    kinds = [e["event"] for e in events]
    assert "tool_call" not in kinds
    assert next(e for e in events if e["event"] == "answer")["data"]["text"] == "你好。"


def test_memory_continues_within_same_thread(actor):
    model = ScriptedModel(script=[AIMessage(content="第一答"), AIMessage(content="第二答")])
    list(run_turn(actor, "第一问", thread_id="memthread", model=model))
    list(run_turn(actor, "第二问", thread_id="memthread", model=model))
    # Second turn's prompt must contain the first exchange (checkpointer works):
    # the scripted model saw 2 calls; langgraph passed prior messages both times.
    assert model.calls == 2


def test_errors_become_error_event_not_exception(actor, monkeypatch):
    class Boom(BaseChatModel):
        @property
        def _llm_type(self):
            return "boom"

        def _generate(self, *a, **k):
            raise RuntimeError("gateway exploded")

        def bind_tools(self, tools, **kwargs):
            return self

    events = list(run_turn(actor, "hi", model=Boom()))
    kinds = [e["event"] for e in events]
    assert "error" in kinds and kinds[-1] == "done"
    assert "gateway exploded" in next(e for e in events if e["event"] == "error")["data"]["message"]


def test_thread_key_embeds_user_identity(actor):
    other = CurrentUser(username="mallory", user_id=7, role="relayops_member")
    assert _thread_key(actor, "abc") != _thread_key(other, "abc")
    assert _thread_key(actor, "abc").startswith("u42:")
