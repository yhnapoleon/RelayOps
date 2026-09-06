"""Chat assistant routes — SSE streaming over the contract in
docs/AGENT_CHAT_CONTRACT.md. Any logged-in user may chat; the tool layer
scopes all data to what that user could see in the UI anyway."""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.deps.auth import CurrentUser, get_current_user
from core.agent import conversation_store
from core.agent.assistant import assistant_available, run_turn
from core.exceptions import ForbiddenError, NotFoundError

router = APIRouter(prefix="/api/agent/chat", tags=["agent"])


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    thread_id: Optional[str] = Field(default=None, max_length=64)
    # Additive (v2): omitting all of these behaves exactly like the old chat
    # (read-only Q&A + auto intent routing). Old clients are unaffected.
    mode: Optional[str] = None
    draft_ref: Optional[int] = None
    form_snapshot: Optional[dict] = None
    # Set by the page assistant (floating ball): {"tab": ..., "sub_view": ...}.
    # Forces the turn read-only and grounds it in the current page's KB.
    page_context: Optional[dict] = None


@router.get("/health")
def chat_health(current_user: CurrentUser = Depends(get_current_user)):
    usable, reason = assistant_available()
    return {"available": usable, "reason": reason}


@router.get("/page-help/{tab_key}")
def page_help(
    tab_key: str,
    sub_view: Optional[str] = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Coach bubbles + preset FAQs for the current page, role-scoped. No LLM —
    works even when the assistant is unavailable (only the chat needs the model).
    Optional ``sub_view`` narrows the coach to a sub-state (e.g. workbench page2)."""
    from core.agent import page_kb

    return page_kb.page_help(current_user.role, tab_key, sub_view)


@router.post("")
def chat(body: ChatRequest, current_user: CurrentUser = Depends(get_current_user)):
    usable, reason = assistant_available()
    if not usable:
        raise HTTPException(status_code=503, detail=f"AI 助手不可用：{reason}")

    def event_stream():
        # Accumulate the turn as it streams so it can be persisted to the
        # user's history once complete — done here (not per-branch) so every
        # intent (qa / diagnose / guide / onboarding / capability) is recorded
        # uniformly. Best-effort: record_turn never raises.
        thread_id = body.thread_id
        steps: list[dict] = []
        answer = ""
        for ev in run_turn(
            current_user, body.message, body.thread_id,
            mode=body.mode, draft_ref=body.draft_ref, form_snapshot=body.form_snapshot,
            page_context=body.page_context,
        ):
            if ev["event"] == "meta":
                thread_id = ev["data"].get("thread_id") or thread_id
            elif ev["event"] == "tool_call":
                steps.append({"name": ev["data"].get("name", ""), "args": ev["data"].get("args") or {}})
            elif ev["event"] == "answer":
                answer = ev["data"].get("text", "") or answer
            data = json.dumps(ev["data"], ensure_ascii=False)
            yield f"event: {ev['event']}\ndata: {data}\n\n"
        conversation_store.record_turn(current_user, thread_id, body.message, answer, steps)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations")
def list_conversations(current_user: CurrentUser = Depends(get_current_user)):
    """The caller's saved conversations (summaries, most recent first)."""
    return conversation_store.list_conversations(current_user)


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: int, current_user: CurrentUser = Depends(get_current_user)
):
    """Full transcript of one saved conversation (ordered messages)."""
    try:
        return conversation_store.get_conversation(current_user, conversation_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: int, current_user: CurrentUser = Depends(get_current_user)
):
    try:
        conversation_store.delete_conversation(current_user, conversation_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
