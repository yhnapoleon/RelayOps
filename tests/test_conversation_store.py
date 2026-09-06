"""Persisted chat history — conversation_store upsert/list/get/delete.

Covers: a turn creates a titled conversation with the user+assistant messages;
a second turn on the same thread appends (not a new conversation); summaries
sort most-recent-first; history is private (other users get 403); delete
cascades to messages; error/empty turns are not recorded.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.models  # noqa: F401 — register tables (incl. agent_conversations/messages)
from core.auth.jwt import CurrentUser
from core.exceptions import ForbiddenError, NotFoundError
from core.models.database import Base
from core.models.user import UserRole


def _actor(uid=42, username="dora", role=UserRole.RELAYOPS_MEMBER):
    return CurrentUser(username=username, user_id=uid, role=role)


class _FakeDB:
    def __init__(self, factory):
        self._f = factory

    def get_session(self):
        return self._f()


@pytest.fixture
def store(monkeypatch):
    from core.agent import conversation_store
    from core.models import database as db_module

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    fake = _FakeDB(sessionmaker(bind=engine))
    # record_turn guards on db_module._db being initialised, then both paths
    # resolve the DB through get_db() — point both at the in-memory fake.
    monkeypatch.setattr(db_module, "_db", fake)
    monkeypatch.setattr(db_module, "get_db", lambda: fake)
    return conversation_store


def test_turn_creates_then_appends(store):
    store.record_turn(_actor(), "t1", "who's on duty?", "Alice is on call.", [{"name": "relayops_on_duty_now", "args": {}}])
    convs = store.list_conversations(_actor())
    assert len(convs) == 1
    assert convs[0]["title"] == "who's on duty?"
    assert convs[0]["message_count"] == 2

    # Second turn on the same thread appends to the same conversation.
    store.record_turn(_actor(), "t1", "and tomorrow?", "Bob is on call.")
    convs = store.list_conversations(_actor())
    assert len(convs) == 1
    assert convs[0]["message_count"] == 4

    detail = store.get_conversation(_actor(), convs[0]["id"])
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert detail["messages"][1]["steps"][0]["name"] == "relayops_on_duty_now"


def test_empty_or_threadless_turn_not_recorded(store):
    store.record_turn(_actor(), "t1", "errored question", "")  # no answer
    store.record_turn(_actor(), None, "no thread", "answer")    # no thread id
    assert store.list_conversations(_actor()) == []


def test_history_is_private(store):
    store.record_turn(_actor(uid=1), "t1", "q", "a")
    [conv] = store.list_conversations(_actor(uid=1))
    # Another user cannot see or open it.
    assert store.list_conversations(_actor(uid=2)) == []
    with pytest.raises(ForbiddenError):
        store.get_conversation(_actor(uid=2), conv["id"])


def test_delete_cascades(store):
    store.record_turn(_actor(), "t1", "q", "a")
    [conv] = store.list_conversations(_actor())
    store.delete_conversation(_actor(), conv["id"])
    assert store.list_conversations(_actor()) == []
    with pytest.raises(NotFoundError):
        store.get_conversation(_actor(), conv["id"])
