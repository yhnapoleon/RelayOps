"""Draft lifecycle for the onboarding agent.

The ``onboarding_drafts`` row is the single source of truth: extraction fills
it, the preview page edits it (server re-validates on every save), and submit
consumes it. Chat/LLM state never feeds submit directly.

No LLM here except inside :func:`extract_for_draft` /
:func:`refine_for_draft`, which delegate to the onboarding pipeline
(``core.agent.onboarding_graph``) and run in a background thread (the API
responds immediately with the draft id and the UI polls).
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from core.auth.jwt import CurrentUser
from core.exceptions import ForbiddenError, NotFoundError, ValidationError
from core.logging import get_logger
from core.models.database import Database, get_db
from core.models.agent_entities import OnboardingDraft
from core.models.entities import Project
from core.models.user import UserRole
from core.services import project_service
from core.services.audit_service import log_audit
from core.agent import submit as submit_module
from core.agent.schemas import ClarificationAnswer, OnboardingDraftPayload
from core.agent.validate import apply_answers, validate_payload

logger = get_logger(__name__)


def _validate_and_enrich(payload, *, target_project_id: Optional[int] = None):
    """Offline validation + CML/MMP cross-check (candidate options for the
    clarification dropdowns). Scenarios added by hand in the preview get the
    same owner-email injection extraction applies (idempotent — authored
    templates are never touched). Enrichment is best-effort: any platform
    error leaves the plain validation report intact.

    Returns ``(report, diff)``. ``diff`` is None unless the draft targets an
    existing project, in which case it is recomputed here — after the email
    injection, so what the reviewer sees marked as changed is exactly what
    gets written — and never trusted from the previous round.

    In update mode the injection is scoped to the assets this draft actually
    touches (a first diff pass off the same snapshot decides which). The rest
    of the payload is the project's live state; back-filling templates and
    owner contacts across it would turn every untouched runbook into a
    proposed change."""
    snapshot = _snapshot_for(target_project_id)
    try:
        from core.agent.scenario_enrich import inject_email_actions

        inject_email_actions(payload, only_paths=_touched_paths(payload, snapshot))
    except Exception:
        logger.opt(exception=True).warning("onboarding: email injection failed")
    report = validate_payload(payload)
    try:
        from core.agent.onboarding_enrich import (
            enrich_validation_report,
            reconcile_cross_project_assets,
        )

        enrich_validation_report(payload, report)
        if target_project_id is None:
            # No source text on this path, so the second documented CML project
            # is not re-derivable — but once extraction re-homed an asset its
            # override lives in the payload, so reconciliation still partitions
            # from the bindings already present (and re-validates a reviewer's
            # manual edit). Pinned to one project in update mode → skipped.
            reconcile_cross_project_assets(payload, report, documented_projects=())
    except Exception:
        logger.opt(exception=True).warning("onboarding: CML/MMP enrichment failed")
    return report, _diff_from(payload, snapshot)


def _snapshot_for(target_project_id: Optional[int]):
    """The live project in draft shape, or None for ordinary onboarding (and
    when the project can't be read — the caller then degrades gracefully)."""
    if target_project_id is None:
        return None
    try:
        from core.agent.project_update import load_snapshot_for_project

        return load_snapshot_for_project(get_db(), target_project_id)
    except Exception:
        logger.opt(exception=True).warning(
            "onboarding: could not read project {} for the diff", target_project_id)
        return None


def _touched_paths(payload, snapshot):
    """Asset paths this draft adds or changes — None (= no restriction) when
    there is no snapshot, which is the ordinary create-a-project case."""
    if snapshot is None:
        return None
    from core.agent.project_update import annotate

    return {
        path for path, node in annotate(payload, snapshot).nodes.items()
        if node.change != "unchanged" and ".scenarios[" not in path and path != "project"
    }


def _diff_from(payload, snapshot):
    """The update-mode diff, or None to tell the caller to keep the previous
    one rather than blank the badges. Display-only: ``update_payload`` derives
    its own diff from a fresh snapshot, so a stale stored diff can never
    misdirect a write."""
    if snapshot is None:
        return None
    try:
        from core.agent.project_update import annotate

        return annotate(payload, snapshot).model_dump()
    except Exception:
        logger.opt(exception=True).warning("onboarding: diff computation failed")
        return None


def serialize_draft(draft: OnboardingDraft, *, include_source: bool = False) -> dict:
    out = {
        "id": draft.id,
        "group_id": draft.group_id,
        "status": draft.status,
        "created_by": draft.created_by,
        "source_filename": draft.source_filename or "",
        "target_project_id": draft.target_project_id,
        "payload": draft.payload,
        "validation": draft.validation,
        "diff": draft.diff,
        "result": draft.result,
        "error": draft.error,
        "submitted_project_id": draft.submitted_project_id,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
    }
    if include_source:
        out["source_text"] = draft.source_text
    return out


def _load_owned(session, draft_id: int, actor: CurrentUser) -> OnboardingDraft:
    draft = session.query(OnboardingDraft).filter(OnboardingDraft.id == draft_id).first()
    if draft is None:
        raise NotFoundError("Onboarding draft not found")
    if draft.created_by != actor.user_id and actor.role != UserRole.ADMIN:
        raise ForbiddenError("Not your draft")
    return draft


def create_draft(
    db: Database,
    *,
    actor: CurrentUser,
    filename: str,
    text: str,
    target_project_id: Optional[int] = None,
) -> OnboardingDraft:
    """``target_project_id`` switches the draft to "add assets to an existing
    project" mode. The caller must have proved the actor can edit that project
    (see :func:`assert_can_target_project`) before getting here."""
    if not text.strip():
        raise ValidationError("Document content is empty")
    session = db.get_session()
    try:
        draft = OnboardingDraft(
            created_by=actor.user_id,
            status=OnboardingDraft.STATUS_EXTRACTING,
            source_filename=filename or "",
            source_text=text,
            target_project_id=target_project_id,
        )
        session.add(draft)
        session.commit()
        session.refresh(draft)
        log_audit(
            user_id=actor.user_id,
            action="create",
            entity_type="onboarding_draft",
            entity_id=draft.id,
            new_value={
                "source_filename": filename,
                "chars": len(text),
                "target_project_id": target_project_id,
            },
        )
        return draft
    finally:
        session.close()


def list_target_projects(db: Database, *, actor: CurrentUser) -> List[dict]:
    """Projects the actor may fold a document into — the ones they can already
    edit by hand, minus system-managed ones (read-only)."""
    from core.models.entities import Application, Job, Product
    from core.models.user import is_elevated_role
    from core.services.support_group_service import is_project_editor

    projects = project_service.list_projects_for_user(
        db,
        user_id=actor.user_id,
        is_admin=is_elevated_role(actor.role),
        groups=actor.groups,
    )
    session = db.get_session()
    try:
        out: List[dict] = []
        for project in projects:
            if project.is_system == 1:
                continue
            if not (
                is_elevated_role(actor.role)
                or project.owner_id == actor.user_id
                or is_project_editor(session, project.id, actor.user_id)
            ):
                continue
            product_ids = [
                row.id for row in
                session.query(Product.id).filter(Product.project_id == project.id).all()
            ]
            out.append({
                "id": project.id,
                "name": project.name,
                "cml_project_name": project.cml_project_name or "",
                "product_count": len(product_ids),
                "job_count": (
                    session.query(Job).filter(Job.product_id.in_(product_ids)).count()
                    if product_ids else 0
                ),
                "app_count": (
                    session.query(Application)
                    .filter(Application.product_id.in_(product_ids)).count()
                    if product_ids else 0
                ),
            })
        return out
    finally:
        session.close()


def assert_can_target_project(db: Database, project_id: int, *, actor: CurrentUser) -> str:
    """Gate the ingest endpoint: the actor must be able to edit this project's
    assets, and the project must not be system-managed. Returns its name."""
    from core.models.user import is_elevated_role
    from core.services.support_group_service import is_project_editor

    session = db.get_session()
    try:
        project = session.query(Project).filter(Project.id == project_id).first()
        if project is None:
            raise NotFoundError("Project not found")
        if project.is_system == 1:
            raise ValidationError("System-managed project is read-only")
        if not (
            is_elevated_role(actor.role)
            or project.owner_id == actor.user_id
            or is_project_editor(session, project.id, actor.user_id)
        ):
            raise ForbiddenError("Not authorized to add assets to this project")
        return project.name or ""
    finally:
        session.close()


def extract_for_draft(draft_id: int) -> None:
    """Background step: pipeline (vision transcribe → LLM extraction →
    scenario normalize → email inject → validation) → ready | failed.

    For an update draft the extraction is then folded into the target project:
    the payload the reviewer sees is the *proposed* state of that project —
    every existing asset plus what the document adds — and the diff says which
    is which."""
    from core.agent.onboarding_graph import run_onboarding_pipeline  # optional deps

    db = get_db()
    session = db.get_session()
    try:
        draft = session.query(OnboardingDraft).filter(OnboardingDraft.id == draft_id).first()
        if draft is None:
            return
        target = draft.target_project_id
        try:
            state = run_onboarding_pipeline(
                text=draft.source_text, skip_reconcile=target is not None)
            if state.get("transcribed"):
                # Image upload — keep the VLM transcription as the readable
                # source text (the base64 sentinel has done its job).
                draft.source_text = state["text"]
            payload = state["payload"]
            report = state["report"]
            diff = None
            if target is not None:
                from core.agent.project_update import load_snapshot_for_project, merge_into_snapshot

                payload = merge_into_snapshot(payload, load_snapshot_for_project(db, target))
                report, diff = _validate_and_enrich(payload, target_project_id=target)
            draft.payload = payload.model_dump()
            draft.validation = report.model_dump()
            if diff is not None:
                draft.diff = diff
            draft.status = OnboardingDraft.STATUS_READY
            draft.error = None
        except Exception as exc:
            logger.opt(exception=True).error("Onboarding extraction failed for draft {}", draft_id)
            draft.status = OnboardingDraft.STATUS_FAILED
            draft.error = f"extraction: {exc}"
        session.commit()
    finally:
        session.close()


def start_refine(
    db: Database,
    draft_id: int,
    *,
    actor: CurrentUser,
    payload: dict,
    answers: Optional[List[ClarificationAnswer]] = None,
    comment: str = "",
) -> OnboardingDraft:
    """Kick the multi-round loop: persist the reviewer's edits + answers
    deterministically (same as save), then flip to ``refining`` — the caller
    schedules :func:`refine_for_draft` in the background."""
    parsed = OnboardingDraftPayload.model_validate(payload)
    parsed = apply_answers(parsed, answers)

    session = db.get_session()
    try:
        draft = _load_owned(session, draft_id, actor)
        if draft.status not in (OnboardingDraft.STATUS_READY, OnboardingDraft.STATUS_FAILED):
            raise ValidationError(f"Draft status is {draft.status}; cannot start AI completion")
        draft.payload = parsed.model_dump()
        draft.status = OnboardingDraft.STATUS_REFINING
        draft.error = None
        session.commit()
        session.refresh(draft)
        log_audit(
            user_id=actor.user_id,
            action="refine",
            entity_type="onboarding_draft",
            entity_id=draft.id,
            new_value={"comment": comment[:500], "answers": len(answers or [])},
        )
        return draft
    finally:
        session.close()


def start_controlm(db: Database, draft_id: int, *, actor: CurrentUser) -> OnboardingDraft:
    """Flip a ready/failed draft to ``refining`` so the UI shows progress while
    the background Control-M merge runs. The sheet text is passed straight to
    the background task (not stored on the draft)."""
    session = db.get_session()
    try:
        draft = _load_owned(session, draft_id, actor)
        if draft.status not in (OnboardingDraft.STATUS_READY, OnboardingDraft.STATUS_FAILED):
            raise ValidationError(f"Draft status is {draft.status}; cannot import a Control-M sheet")
        draft.status = OnboardingDraft.STATUS_REFINING
        draft.error = None
        session.commit()
        session.refresh(draft)
        return draft
    finally:
        session.close()


def controlm_for_draft(draft_id: int, sheet_text: str) -> None:
    """Background step: LLM-extract production jobs from the Control-M sheet,
    merge their real names + schedules into the draft, re-validate → ready."""
    from core.agent.controlm_sheet import extract_controlm_jobs, merge_controlm_into_draft

    db = get_db()
    session = db.get_session()
    try:
        draft = session.query(OnboardingDraft).filter(OnboardingDraft.id == draft_id).first()
        if draft is None or draft.status != OnboardingDraft.STATUS_REFINING:
            return
        try:
            payload = OnboardingDraftPayload.model_validate(draft.payload or {})
            cm_jobs = extract_controlm_jobs(sheet_text)
            # Source text carries the region ↔ MMP-model cross-reference the
            # LLM merge needs to fold sheet jobs into the right draft jobs.
            notes = merge_controlm_into_draft(payload, cm_jobs, draft.source_text or "")
            payload.warnings.extend(notes)
            report, diff = _validate_and_enrich(
                payload, target_project_id=draft.target_project_id)
            draft.payload = payload.model_dump()
            draft.controlm_sheet_text = sheet_text
            draft.validation = report.model_dump()
            if diff is not None:
                draft.diff = diff
            draft.status = OnboardingDraft.STATUS_READY
            draft.error = None
        except Exception as exc:
            logger.opt(exception=True).error("Control-M merge failed for draft {}", draft_id)
            draft.status = OnboardingDraft.STATUS_FAILED
            draft.error = f"controlm: {exc}"
        session.commit()
    finally:
        session.close()


def refine_for_draft(draft_id: int, comment: str = "") -> None:
    """Background step of the review loop: the LLM fills remaining gaps in
    the saved payload (protective merge — reviewer values survive), then the
    draft returns to ``ready`` for the next round of review."""
    from core.agent.onboarding_graph import run_onboarding_pipeline  # optional deps

    db = get_db()
    session = db.get_session()
    try:
        draft = session.query(OnboardingDraft).filter(OnboardingDraft.id == draft_id).first()
        if draft is None or draft.status != OnboardingDraft.STATUS_REFINING:
            return
        try:
            payload = OnboardingDraftPayload.model_validate(draft.payload or {})
            open_questions = [
                c.get("question", "")
                for c in ((draft.validation or {}).get("clarifications") or [])
                if c.get("question")
            ]
            target = draft.target_project_id
            state = run_onboarding_pipeline(
                text=draft.source_text, mode="refine", payload=payload,
                comment=comment, open_questions=open_questions,
                skip_reconcile=target is not None,
                skip_inject_email=target is not None,
            )
            report = state["report"]
            diff = None
            if target is not None:
                # The refine merge can append/re-order nodes, so the scoped
                # email fill and the diff both have to be re-derived from the
                # payload the LLM just produced — not carried over.
                report, diff = _validate_and_enrich(
                    state["payload"], target_project_id=target)
            draft.payload = state["payload"].model_dump()
            draft.validation = report.model_dump()
            if diff is not None:
                draft.diff = diff
            draft.status = OnboardingDraft.STATUS_READY
            draft.error = None
        except Exception as exc:
            logger.opt(exception=True).error("Onboarding refine failed for draft {}", draft_id)
            draft.status = OnboardingDraft.STATUS_FAILED
            draft.error = f"refine: {exc}"
        session.commit()
    finally:
        session.close()


def list_drafts(db: Database, *, actor: CurrentUser) -> List[OnboardingDraft]:
    session = db.get_session()
    try:
        q = session.query(OnboardingDraft)
        if actor.role != UserRole.ADMIN:
            q = q.filter(OnboardingDraft.created_by == actor.user_id)
        return q.order_by(OnboardingDraft.id.desc()).limit(50).all()
    finally:
        session.close()


def get_draft(db: Database, draft_id: int, *, actor: CurrentUser) -> OnboardingDraft:
    session = db.get_session()
    try:
        return _load_owned(session, draft_id, actor)
    finally:
        session.close()


# In-flight statuses own a background task that's still writing the row —
# deleting underneath it would race, so block until it settles.
_IN_FLIGHT = (
    OnboardingDraft.STATUS_EXTRACTING,
    OnboardingDraft.STATUS_REFINING,
    OnboardingDraft.STATUS_SUBMITTED,
)


def delete_draft(db: Database, draft_id: int, *, actor: CurrentUser) -> None:
    """Remove an onboarding draft (RBAC: owner or admin). Deleting a completed
    draft only discards the import record — the project it created lives on
    independently."""
    session = db.get_session()
    try:
        draft = _load_owned(session, draft_id, actor)
        if draft.status in _IN_FLIGHT:
            raise ValidationError(f"Draft is {draft.status}; wait for it to finish before deleting")
        log_audit(
            user_id=actor.user_id,
            action="delete",
            entity_type="onboarding_draft",
            entity_id=draft.id,
            old_value={"status": draft.status, "source_filename": draft.source_filename or ""},
        )
        session.delete(draft)
        session.commit()
    finally:
        session.close()


def save_draft(
    db: Database,
    draft_id: int,
    *,
    actor: CurrentUser,
    payload: dict,
    answers: Optional[List[ClarificationAnswer]] = None,
) -> OnboardingDraft:
    """Persist reviewer edits + clarification answers, then re-validate.
    A failed draft becomes editable again (back to ready). For an update draft
    the diff is re-derived here too, so the NEW / UPDATED / UNCHANGED badges
    always describe the payload as just saved."""
    parsed = OnboardingDraftPayload.model_validate(payload)
    parsed = apply_answers(parsed, answers)

    session = db.get_session()
    try:
        draft = _load_owned(session, draft_id, actor)
        if draft.status in (OnboardingDraft.STATUS_SUBMITTED, OnboardingDraft.STATUS_COMPLETED):
            raise ValidationError(f"Draft status is {draft.status}; no longer editable")
        report, diff = _validate_and_enrich(parsed, target_project_id=draft.target_project_id)
        draft.payload = parsed.model_dump()
        draft.validation = report.model_dump()
        if diff is not None:
            draft.diff = diff
        draft.status = OnboardingDraft.STATUS_READY
        draft.error = None
        session.commit()
        session.refresh(draft)
        return draft
    finally:
        session.close()


def split_draft(db: Database, draft_id: int, *, actor: CurrentUser) -> List[OnboardingDraft]:
    """One handover document, multiple CML projects: partition the draft's
    assets by the CML project each actually binds to and materialise one
    sibling draft per project (sharing a ``group_id``). The original row becomes
    the first sibling (keeps its id); the rest are new rows. Each is re-validated
    independently. Returns the sibling drafts (origin first)."""
    from core.agent.onboarding_enrich import partition_payload_by_cml_project

    session = db.get_session()
    try:
        draft = _load_owned(session, draft_id, actor)
        if draft.status in (OnboardingDraft.STATUS_SUBMITTED, OnboardingDraft.STATUS_COMPLETED):
            raise ValidationError(f"Draft status is {draft.status}; no longer editable")
        if draft.target_project_id is not None:
            raise ValidationError(
                "This draft adds assets to an existing project; splitting it into "
                "separate registrations does not apply")
        if draft.group_id:
            raise ValidationError("This draft is already part of a split group")
        if not draft.payload:
            raise ValidationError("Draft has nothing to split")

        payload = OnboardingDraftPayload.model_validate(draft.payload)
        parts = partition_payload_by_cml_project(payload)
        if len(parts) < 2:
            raise ValidationError(
                "The draft's assets all bind to one CML project; nothing to split")

        group_id = uuid.uuid4().hex
        siblings: List[OnboardingDraft] = []

        # First partition reuses the original row (preserves its id + audit).
        _, first_payload = parts[0]
        first_report, _ = _validate_and_enrich(first_payload)
        draft.group_id = group_id
        draft.payload = first_payload.model_dump()
        draft.validation = first_report.model_dump()
        draft.status = OnboardingDraft.STATUS_READY
        draft.error = None
        siblings.append(draft)

        for _, part_payload in parts[1:]:
            report, _ = _validate_and_enrich(part_payload)
            sib = OnboardingDraft(
                created_by=draft.created_by,
                group_id=group_id,
                status=OnboardingDraft.STATUS_READY,
                source_filename=draft.source_filename or "",
                source_text=draft.source_text,
                payload=part_payload.model_dump(),
                validation=report.model_dump(),
            )
            session.add(sib)
            siblings.append(sib)

        session.commit()
        for sib in siblings:
            session.refresh(sib)
        log_audit(
            user_id=actor.user_id,
            action="split",
            entity_type="onboarding_draft",
            entity_id=draft.id,
            new_value={"group_id": group_id, "projects": [p[0] for p in parts]},
        )
        return siblings
    finally:
        session.close()


def list_group(db: Database, group_id: str, *, actor: CurrentUser) -> List[OnboardingDraft]:
    """All sibling drafts sharing a split ``group_id`` (RBAC: owner or admin)."""
    session = db.get_session()
    try:
        q = session.query(OnboardingDraft).filter(OnboardingDraft.group_id == group_id)
        if actor.role != UserRole.ADMIN:
            q = q.filter(OnboardingDraft.created_by == actor.user_id)
        drafts = q.order_by(OnboardingDraft.id.asc()).all()
        if not drafts:
            raise NotFoundError("Onboarding draft group not found")
        return drafts
    finally:
        session.close()


def submit_draft(db: Database, draft_id: int, *, actor: CurrentUser) -> OnboardingDraft:
    """Materialise the draft's saved payload (and nothing else): a new project
    for an ordinary draft, or an in-place update of ``target_project_id`` when
    the reviewer chose an existing project."""
    session = db.get_session()
    try:
        draft = _load_owned(session, draft_id, actor)
        if draft.status != OnboardingDraft.STATUS_READY:
            raise ValidationError(f"Draft status is {draft.status}; only a ready draft can be submitted")
        if not draft.payload:
            raise ValidationError("Draft has nothing to submit")
        target = draft.target_project_id
        payload = OnboardingDraftPayload.model_validate(draft.payload)
        report = validate_payload(payload)
        if report.errors:
            draft.validation = report.model_dump()
            session.commit()
            raise ValidationError("Validation failed; please fix the highlighted fields on the preview page first")
        draft.status = OnboardingDraft.STATUS_SUBMITTED
        session.commit()
    finally:
        session.close()

    try:
        if target is not None:
            from core.agent.project_update import update_payload

            # Re-check at write time: the draft may have sat in review while
            # the actor's access to the project changed.
            assert_can_target_project(db, target, actor=actor)
            result = update_payload(db, payload=payload, project_id=target, actor=actor)
        else:
            result = submit_module.submit_payload(db, payload=payload, actor=actor)
    except Exception as exc:
        _mark(db, draft_id, status=OnboardingDraft.STATUS_FAILED, error=f"submit: {exc}")
        raise

    session = db.get_session()
    try:
        draft = session.query(OnboardingDraft).filter(OnboardingDraft.id == draft_id).first()
        draft.status = OnboardingDraft.STATUS_COMPLETED
        draft.result = result
        draft.submitted_project_id = result.get("project_id")
        session.commit()
        session.refresh(draft)
        log_audit(
            user_id=actor.user_id,
            action="update" if target is not None else "submit",
            entity_type="onboarding_draft",
            entity_id=draft_id,
            new_value=result,
        )
        return draft
    finally:
        session.close()


def _mark(db: Database, draft_id: int, *, status: str, error: Optional[str]) -> None:
    session = db.get_session()
    try:
        draft = session.query(OnboardingDraft).filter(OnboardingDraft.id == draft_id).first()
        if draft is not None:
            draft.status = status
            draft.error = error
            session.commit()
    finally:
        session.close()
