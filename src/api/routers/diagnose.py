"""Issue diagnosis route — one-click "diagnose" on an Issue, SSE streaming.

Access mirrors issue visibility
(product scope / assignee / creator / admin), enforced inside collect_bundle
before the stream starts so permission failures surface as 403/404, not as
stream events.
"""

import json

from fastapi import APIRouter, Depends, HTTPException

from fastapi.responses import StreamingResponse

from api.deps.auth import CurrentUser, get_current_user
from core.agent.chat import chat_available
from core.agent.diagnose import check_access, run_diagnose
from core.models.database import get_db

router = APIRouter(prefix="/api/agent/diagnose", tags=["agent"])


@router.post("/{issue_id}")
def diagnose_issue(issue_id: int, current_user: CurrentUser = Depends(get_current_user)):
    usable, reason = chat_available()
    if not usable:
        raise HTTPException(status_code=503, detail=f"AI 助手不可用：{reason}")

    # Pre-flight access/existence check OUTSIDE the stream so the client gets
    # a clean 403/404 (DomainError handler) instead of a 200 + error event.
    session = get_db().get_session()
    try:
        check_access(session, current_user, issue_id)
    finally:
        session.close()

    def event_stream():
        for ev in run_diagnose(current_user, issue_id):
            yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
