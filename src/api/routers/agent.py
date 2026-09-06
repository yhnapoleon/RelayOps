"""Onboarding-agent routes: ingest a handover document, review the extracted
draft, answer the agent's clarification questions, submit to create entities.

Spec: docs/AGENT_ONBOARDING_SPEC.md. Same permission bar as creating a
project by hand (ProjectEditorOrAdmin); drafts are private to their creator
(admins see all). LLM is used only in the background extraction step — if the
gateway isn't configured the ingest endpoint answers 503 up front.
"""

from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from api.deps.auth import CurrentUser
from api.deps.rbac import BusinessOwnerOrAdmin
from core.agent import draft_service
from core.agent.ingest import UnsupportedFormatError, extract_text
from core.agent.schemas import ClarificationAnswer
from core.models.database import get_db

router = APIRouter(prefix="/api/agent/onboarding", tags=["agent"])


class DraftSaveRequest(BaseModel):
    payload: dict
    answers: List[ClarificationAnswer] = Field(default_factory=list)


class DraftRefineRequest(BaseModel):
    """Multi-round review: the reviewer's current edits + clarification
    answers (applied deterministically first) and an optional free-text note
    steering the AI's second-pass fill."""

    payload: dict
    answers: List[ClarificationAnswer] = Field(default_factory=list)
    comment: str = ""


@router.post("", status_code=201)
async def ingest_document(
    background: BackgroundTasks,
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    url: Optional[str] = Form(None),
    confluence_token: Optional[str] = Form(None),
    project_id: Optional[int] = Form(None),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    """Three sources, priority file > url > text. ``url`` fetches the
    Confluence page over REST with the caller's PAT (``confluence_token``,
    never stored) or the configured service token — tables and hyperlink
    targets survive, unlike copy-paste.

    ``project_id`` switches the draft to "add assets to an existing project":
    the extraction is folded into that project instead of creating a new one,
    and submit becomes an in-place update."""
    from core.agent.extraction import extraction_available  # optional deps

    usable, reason = extraction_available()
    if not usable:
        raise HTTPException(status_code=503, detail=f"AI 抽取不可用：{reason}")

    if project_id is not None:
        # Fail before we spend an LLM call on a project the caller can't edit.
        draft_service.assert_can_target_project(get_db(), project_id, actor=current_user)

    if file is not None:
        try:
            document = extract_text(file.filename or "", await file.read())
        except UnsupportedFormatError as exc:
            raise HTTPException(status_code=415, detail=str(exc))
        filename = file.filename or ""
    elif url and url.strip():
        from core.agent.ingest import html_to_text
        from core.integrations.confluence_connector import ConfluenceError, fetch_page_html

        try:
            title, html = fetch_page_html(url.strip(), user_token=confluence_token or "")
        except ConfluenceError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        document = html_to_text(html)
        filename = title or url.strip()
    elif text and text.strip():
        document, filename = text.strip(), "(pasted text)"
    else:
        raise HTTPException(
            status_code=422,
            detail="请上传 txt/md/html/pdf/png/jpg 文件、给一个 Confluence 页面 URL，或粘贴文本",
        )

    draft = draft_service.create_draft(
        get_db(), actor=current_user, filename=filename, text=document,
        target_project_id=project_id,
    )
    background.add_task(draft_service.extract_for_draft, draft.id)
    return draft_service.serialize_draft(draft)


@router.get("/target-projects")
def list_target_projects(current_user: CurrentUser = Depends(BusinessOwnerOrAdmin)):
    """Projects the caller may fold a document into (the ones they can already
    edit by hand), with asset counts for the picker."""
    return draft_service.list_target_projects(get_db(), actor=current_user)


@router.get("/capabilities")
def capabilities(current_user: CurrentUser = Depends(BusinessOwnerOrAdmin)):
    """What the import form should offer. ``confluence_service_token`` true =
    a shared service account is configured, so the per-user PAT field can be
    hidden (the reviewer types nothing for URL fetches)."""
    from core.config import get_config

    cfg = get_config()
    return {"confluence_service_token": bool(cfg.confluence_bearer_token)}


@router.get("")
def list_drafts(current_user: CurrentUser = Depends(BusinessOwnerOrAdmin)):
    drafts = draft_service.list_drafts(get_db(), actor=current_user)
    return [draft_service.serialize_draft(d) for d in drafts]


@router.get("/group/{group_id}")
def get_group(group_id: str, current_user: CurrentUser = Depends(BusinessOwnerOrAdmin)):
    """Sibling drafts produced by splitting one cross-project document."""
    drafts = draft_service.list_group(get_db(), group_id, actor=current_user)
    return [draft_service.serialize_draft(d) for d in drafts]


@router.get("/{draft_id}")
def get_draft(draft_id: int, current_user: CurrentUser = Depends(BusinessOwnerOrAdmin)):
    draft = draft_service.get_draft(get_db(), draft_id, actor=current_user)
    return draft_service.serialize_draft(draft, include_source=True)


@router.put("/{draft_id}")
def save_draft(
    draft_id: int,
    body: DraftSaveRequest,
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    draft = draft_service.save_draft(
        get_db(), draft_id, actor=current_user, payload=body.payload, answers=body.answers
    )
    return draft_service.serialize_draft(draft)


@router.post("/{draft_id}/refine", status_code=202)
def refine_draft(
    draft_id: int,
    body: DraftRefineRequest,
    background: BackgroundTasks,
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    """Send the reviewed draft back to the AI for a second-pass fill. The
    edits/answers are persisted deterministically first; the LLM only fills
    what's still empty (protective merge) and the draft returns to ready for
    the next review round."""
    from core.agent.extraction import extraction_available  # optional deps

    usable, reason = extraction_available()
    if not usable:
        raise HTTPException(status_code=503, detail=f"AI 补全不可用：{reason}")

    draft = draft_service.start_refine(
        get_db(), draft_id, actor=current_user,
        payload=body.payload, answers=body.answers, comment=body.comment,
    )
    background.add_task(draft_service.refine_for_draft, draft.id, body.comment)
    return draft_service.serialize_draft(draft)


@router.post("/{draft_id}/controlm", status_code=202)
async def import_controlm_sheet(
    draft_id: int,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: CurrentUser = Depends(BusinessOwnerOrAdmin),
):
    """Upload a Control-M job sheet (xlsx/csv/tsv/txt/html) onto an existing
    draft. The sheet's real Control-M job names + schedules are merged into the
    draft's jobs in the background (status → refining; poll until ready)."""
    from core.agent.controlm_sheet import UnsupportedSheetError, parse_sheet_to_text
    from core.agent.extraction import extraction_available

    usable, reason = extraction_available()
    if not usable:
        raise HTTPException(status_code=503, detail=f"AI 抽取不可用：{reason}")
    try:
        sheet_text = parse_sheet_to_text(file.filename or "", await file.read())
    except UnsupportedSheetError as exc:
        raise HTTPException(status_code=415, detail=str(exc))
    if not sheet_text.strip():
        raise HTTPException(status_code=422, detail="表格内容为空")

    draft = draft_service.start_controlm(get_db(), draft_id, actor=current_user)
    background.add_task(draft_service.controlm_for_draft, draft.id, sheet_text)
    return draft_service.serialize_draft(draft)


@router.post("/{draft_id}/split")
def split_draft(draft_id: int, current_user: CurrentUser = Depends(BusinessOwnerOrAdmin)):
    """Split a cross-project draft into one sibling draft per CML project. The
    reviewer triggers this from the project-level split card. Returns the
    sibling drafts (origin first), all sharing a new ``group_id``."""
    drafts = draft_service.split_draft(get_db(), draft_id, actor=current_user)
    return [draft_service.serialize_draft(d) for d in drafts]


@router.post("/{draft_id}/submit")
def submit_draft(draft_id: int, current_user: CurrentUser = Depends(BusinessOwnerOrAdmin)):
    draft = draft_service.submit_draft(get_db(), draft_id, actor=current_user)
    return draft_service.serialize_draft(draft)


@router.delete("/{draft_id}", status_code=204)
def delete_draft(draft_id: int, current_user: CurrentUser = Depends(BusinessOwnerOrAdmin)):
    """Discard a draft (owner or admin). An in-flight draft (extracting /
    refining / submitting) returns 422 until it settles."""
    draft_service.delete_draft(get_db(), draft_id, actor=current_user)
