"""Persisted chat history — store and retrieve :class:`AgentConversation`.

One conversation per ``(user_id, thread_id)``; :func:`record_turn` appends the
user message + assistant answer of each completed turn so the user can reopen
the thread later. Conversations are private to their creator (admins do not see
others' chats here — this is personal history, not an audit log; the
:class:`AgentRun` kind=chat trail covers prompt regression).
"""

from __future__ import annotations

from typing import List, Optional

from core.auth.jwt import CurrentUser
from core.exceptions import ForbiddenError, NotFoundError
from core.logging import get_logger
from core.models.agent_entities import AgentConversation, AgentMessage

logger = get_logger(__name__)

_TITLE_MAX = 200


def record_turn(
    actor: CurrentUser,
    thread_id: Optional[str],
    question: str,
    answer: str,
    steps: Optional[List[dict]] = None,
) -> None:
    """Append one completed turn to its conversation (best-effort).

    Upserts the conversation by ``(user_id, thread_id)`` and adds two messages.
    Never raises and never opens a DB connection itself — like the AgentRun
    audit, it only piggybacks on an already-initialised singleton so a write to
    history can never be what blocks/retries a connection on the stream path.
    """
    if not thread_id or not (answer or "").strip():
        return
    try:
        from core.models import database as db_module

        if getattr(db_module, "_db", None) is None:
            return
        session = db_module.get_db().get_session()
        try:
            conv = (
                session.query(AgentConversation)
                .filter(
                    AgentConversation.user_id == actor.user_id,
                    AgentConversation.thread_id == thread_id,
                )
                .first()
            )
            if conv is None:
                conv = AgentConversation(
                    user_id=actor.user_id,
                    thread_id=thread_id,
                    title=_make_title(question),
                )
                session.add(conv)
                session.flush()  # assign conv.id for the FK below
            else:
                # Touch updated_at so history sorts most-recent-first.
                from datetime import datetime

                conv.updated_at = datetime.utcnow()
            session.add(
                AgentMessage(conversation_id=conv.id, role="user", content=(question or "")[:8000])
            )
            session.add(
                AgentMessage(
                    conversation_id=conv.id,
                    role="assistant",
                    content=(answer or "")[:16000],
                    steps=[{"name": s.get("name", ""), "args": s.get("args") or {}} for s in (steps or [])]
                    or None,
                )
            )
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.opt(exception=True).warning("conversation_store: record_turn failed")


def _make_title(question: str) -> str:
    text = " ".join((question or "").split())
    return text[:_TITLE_MAX] or "New conversation"


def list_conversations(actor: CurrentUser, limit: int = 100) -> List[dict]:
    """The caller's conversations, most-recently-updated first (summaries only)."""
    from core.models.database import get_db

    session = get_db().get_session()
    try:
        rows = (
            session.query(AgentConversation)
            .filter(AgentConversation.user_id == actor.user_id)
            .order_by(AgentConversation.updated_at.desc())
            .limit(limit)
            .all()
        )
        return [_summarize(c, message_count=len(c.messages)) for c in rows]
    finally:
        session.close()


def get_conversation(actor: CurrentUser, conv_id: int) -> dict:
    """Full conversation (with ordered messages). 404 if missing, 403 if not the
    caller's — history is private, so admins do not get a backdoor here."""
    from core.models.database import get_db

    session = get_db().get_session()
    try:
        conv = (
            session.query(AgentConversation).filter(AgentConversation.id == conv_id).first()
        )
        if conv is None:
            raise NotFoundError("Conversation not found")
        if conv.user_id != actor.user_id:
            raise ForbiddenError("Not your conversation")
        out = _summarize(conv, message_count=len(conv.messages))
        out["messages"] = [
            {
                "role": m.role,
                "content": m.content or "",
                "steps": m.steps or [],
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in conv.messages
        ]
        return out
    finally:
        session.close()


def delete_conversation(actor: CurrentUser, conv_id: int) -> None:
    from core.models.database import get_db

    session = get_db().get_session()
    try:
        conv = (
            session.query(AgentConversation).filter(AgentConversation.id == conv_id).first()
        )
        if conv is None:
            raise NotFoundError("Conversation not found")
        if conv.user_id != actor.user_id:
            raise ForbiddenError("Not your conversation")
        session.delete(conv)  # cascades to messages
        session.commit()
    finally:
        session.close()


def _summarize(conv: AgentConversation, *, message_count: int) -> dict:
    return {
        "id": conv.id,
        "thread_id": conv.thread_id,
        "title": conv.title or "New conversation",
        "message_count": message_count,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
    }
