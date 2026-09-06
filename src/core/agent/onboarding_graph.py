"""RelayOps module."""

from __future__ import annotations

import json
from typing import Callable, Iterable, List, Optional, Tuple

from pydantic import BaseModel

from core.logging import get_logger
from core.agent.ingest import IMAGE_SENTINEL
from core.agent.schemas import OnboardingDraftPayload, ValidationReport
from core.agent.validate import validate_payload

logger = get_logger(__name__)

try:
    from langgraph.graph import END, START, StateGraph  # type: ignore

    _LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover — optional-dependency fallback
    StateGraph = None  # type: ignore[assignment]
    _LANGGRAPH_AVAILABLE = False


# ── protective merge (the refine-mode hard guarantee) ─────────────────


def merge_preserving(old: BaseModel, new: BaseModel) -> BaseModel:
    """Fill-only merge of a refine output into the reviewed draft.

    * scalars: the old value wins whenever it is non-empty — refine may only
      fill blanks;
    * ``list[str]``: old wins when non-empty (no append — step lists are
      runbooks the reviewer may have deliberately trimmed);
    * ``list[BaseModel]``: pairwise merge by index; *new* extras are appended
      (refine found missed jobs/scenarios), *old* extras are kept (refine can
      never drop entities).

    ``warnings`` is the one exception: both sides are kept (deduped) since
    it's an advisory trail, not data.
    """
    merged = old.model_copy(deep=True)
    for name in type(old).model_fields:
        ov, nv = getattr(merged, name), getattr(new, name, None)
        if nv is None:
            continue
        if isinstance(ov, BaseModel):
            setattr(merged, name, merge_preserving(ov, nv))
        elif isinstance(ov, str):
            if not ov.strip() and isinstance(nv, str) and nv.strip():
                setattr(merged, name, nv.strip())
        elif isinstance(ov, list):
            if ov and all(isinstance(x, str) for x in ov):
                continue  # non-empty step list — reviewer's version stands
            if not ov:
                setattr(merged, name, list(nv))
                continue
            # list of models: pairwise + extras from both sides
            out = [merge_preserving(o, n) for o, n in zip(ov, nv)]
            out.extend(nv[len(ov):])
            out.extend(ov[len(nv):])
            setattr(merged, name, out)
    return merged


def _merge_payloads(old: OnboardingDraftPayload,
                    new: OnboardingDraftPayload) -> OnboardingDraftPayload:
    merged = merge_preserving(old, new)
    seen = set()
    combined: List[str] = []
    for w in [*old.warnings, *new.warnings]:
        if w not in seen:
            seen.add(w)
            combined.append(w)
    merged.warnings = combined
    return merged


# ── nodes ─────────────────────────────────────────────────────────────


def node_transcribe(state: dict) -> dict:
    """Image sentinel → plain text via the gateway VLM. No-op otherwise."""
    text = state["text"]
    if text.startswith(IMAGE_SENTINEL):
        from core.agent.vision import transcribe_image_text

        state["text"] = transcribe_image_text(text)
        state["transcribed"] = True
    return state


def node_extract(state: dict) -> dict:
    from core.agent.extraction import run_extraction

    state["payload"] = run_extraction(state["text"])
    return state


def node_refine(state: dict) -> dict:
    """Second-round fill: LLM sees the document + current draft + open
    questions + the reviewer's note, outputs a full draft; the protective
    merge keeps everything the reviewer already settled."""
    from core.agent.llm import get_chat_model
    from core.agent.prompts import ONBOARDING_REFINE_SYSTEM, ONBOARDING_REFINE_USER

    from core.agent.structured import invoke_structured

    base: OnboardingDraftPayload = state["payload"]
    open_questions = state.get("open_questions") or []
    chat = get_chat_model(temperature=0)
    logger.info("Onboarding refine: {} open questions, comment {} chars",
                len(open_questions), len(state.get("comment") or ""))
    proposal = invoke_structured(chat, OnboardingDraftPayload, [
        ("system", ONBOARDING_REFINE_SYSTEM),
        ("human", ONBOARDING_REFINE_USER.format(
            document=state["text"],
            draft_json=json.dumps(base.model_dump(), ensure_ascii=False, indent=1),
            open_questions="\n".join(f"- {q}" for q in open_questions) or "（无）",
            comment=(state.get("comment") or "").strip() or "（无）",
        )),
    ])
    state["payload"] = _merge_payloads(base, proposal)
    return state


def node_resolve_cml(state: dict) -> dict:
    """Settle the CML project binding AND its assets against the live platform:
    the LLM matches display-name ⇄ repo-name (project) and doc-job ⇄ real-job
    (assets) when string similarity can't — always within the fetched
    candidates, never over a value the document gave."""
    from core.agent.onboarding_enrich import resolve_cml_assets, resolve_cml_binding

    try:
        resolve_cml_binding(state["payload"], state["text"])
        resolve_cml_assets(state["payload"], state["text"])
    except Exception:  # noqa: BLE001 — binding match must never fail the draft
        logger.opt(exception=True).warning("onboarding: CML binding resolution failed")
    return state


def node_resolve_mmp(state: dict) -> dict:
    """Settle MMP project bindings against the live directory with an LLM
    fallback (the symmetric counterpart to node_resolve_cml). Deterministic
    tracks — url id / bare id / exact name — win untouched; only difflib's
    unsafe cases (a bare name hitting several @entity siblings) fall through to
    the LLM, constrained to the real candidates. Best-effort: MMP trouble must
    never fail the draft."""
    from core.agent.onboarding_enrich import resolve_mmp_binding

    try:
        resolve_mmp_binding(state["payload"], state["text"])
    except Exception:  # noqa: BLE001 — binding match must never fail the draft
        logger.opt(exception=True).warning("onboarding: MMP binding resolution failed")
    return state


def node_normalize_scenarios(state: dict) -> dict:
    from core.agent.scenario_enrich import normalize_scenario_types

    normalize_scenario_types(state["payload"])
    return state


def node_inject_email(state: dict) -> dict:
    """Skipped for an update-mode refine: the payload there is the whole target
    project, and a blanket fill would author templates onto runbooks the
    document never mentioned. ``draft_service`` re-runs the injection scoped to
    the assets the draft actually touches."""
    from core.agent.scenario_enrich import inject_email_actions

    if state.get("skip_inject_email"):
        return state
    inject_email_actions(state["payload"])
    return state


def node_finalize(state: dict) -> dict:
    """Offline validation + live CML/MMP cross-check/backfill. Enrichment
    stays best-effort: platform trouble must never fail the pipeline."""
    payload: OnboardingDraftPayload = state["payload"]
    report = validate_payload(payload)
    try:
        from core.agent.onboarding_enrich import enrich_validation_report

        enrich_validation_report(payload, report)
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning("onboarding: CML/MMP enrichment failed")
    state["report"] = report
    return state


def node_reconcile_projects(state: dict) -> dict:
    """Cross-project asset reconciliation (after finalize, so the report and
    the single-project backfill already exist). When the document references
    more than one CML project, decide which project each draft job/app truly
    lives in (against live CML), re-home mismatched assets onto their real
    project, and surface a split summary. Best-effort: a re-derive of the URL
    anchors here is cheap and keeps the node independent of the extract path
    (refine never re-runs apply_doc_signals).

    Skipped for an "add to an existing project" draft: the target project is
    the reviewer's explicit choice, so re-homing assets onto a *different* CML
    project — or proposing a split into sibling registrations — would fight it."""
    report = state.get("report")
    if report is None or state.get("skip_reconcile"):
        return state
    try:
        from core.agent.doc_signals import derive_doc_signals
        from core.agent.onboarding_enrich import reconcile_cross_project_assets

        signals = derive_doc_signals(state["text"])
        reconcile_cross_project_assets(
            state["payload"], report, signals.cml_project_names)
    except Exception:  # noqa: BLE001 — reconciliation must never fail the draft
        logger.opt(exception=True).warning("onboarding: cross-project reconcile failed")
    return state


_EXTRACT_NODES: Tuple[Callable, ...] = (
    node_transcribe, node_extract, node_resolve_cml, node_resolve_mmp,
    node_normalize_scenarios, node_inject_email, node_finalize,
    node_reconcile_projects,
)
_REFINE_NODES: Tuple[Callable, ...] = (
    node_refine, node_resolve_cml, node_resolve_mmp, node_normalize_scenarios,
    node_inject_email, node_finalize, node_reconcile_projects,
)


def _run_sequential(nodes: Iterable[Callable], state: dict) -> dict:
    for node in nodes:
        state = node(state)
    return state


_compiled_graph = None
_graph_build_failed = False


def _get_compiled_graph():
    """RelayOps  get compiled graph."""
    global _compiled_graph, _graph_build_failed
    if _compiled_graph is not None or _graph_build_failed or not _LANGGRAPH_AVAILABLE:
        return _compiled_graph
    try:
        graph = StateGraph(dict)
        for node in {*_EXTRACT_NODES, *_REFINE_NODES}:
            graph.add_node(node.__name__, node)
        graph.add_conditional_edges(
            START,
            lambda s: "node_refine" if s.get("mode") == "refine" else "node_transcribe",
        )
        graph.add_edge("node_transcribe", "node_extract")
        graph.add_edge("node_extract", "node_resolve_cml")
        graph.add_edge("node_refine", "node_resolve_cml")
        graph.add_edge("node_resolve_cml", "node_resolve_mmp")
        graph.add_edge("node_resolve_mmp", "node_normalize_scenarios")
        graph.add_edge("node_normalize_scenarios", "node_inject_email")
        graph.add_edge("node_inject_email", "node_finalize")
        graph.add_edge("node_finalize", "node_reconcile_projects")
        graph.add_edge("node_reconcile_projects", END)
        _compiled_graph = graph.compile()
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "onboarding: StateGraph build failed — using sequential runner")
        _graph_build_failed = True
    return _compiled_graph


def run_onboarding_pipeline(
    *,
    text: str,
    mode: str = "extract",
    payload: Optional[OnboardingDraftPayload] = None,
    comment: str = "",
    open_questions: Optional[List[str]] = None,
    skip_reconcile: bool = False,
    skip_inject_email: bool = False,
) -> dict:
    """Run the pipeline; returns the final state dict with keys
    ``payload`` (OnboardingDraftPayload), ``report`` (ValidationReport),
    ``text`` (possibly VLM-transcribed) and ``transcribed`` (bool).

    ``mode='refine'`` requires ``payload`` (the reviewed draft).
    ``skip_reconcile`` turns off the cross-project re-homing/split step and
    ``skip_inject_email`` the owner-email fill — both set for update drafts,
    whose target project is already pinned and whose payload includes assets
    the document never mentioned.
    """
    if mode == "refine" and payload is None:
        raise ValueError("refine mode requires the current payload")
    state: dict = {
        "text": text, "mode": mode, "payload": payload,
        "comment": comment, "open_questions": open_questions or [],
        "transcribed": False, "report": ValidationReport(),
        "skip_reconcile": skip_reconcile,
        "skip_inject_email": skip_inject_email,
    }
    graph = _get_compiled_graph()
    if graph is not None:
        return graph.invoke(state)
    return _run_sequential(
        _REFINE_NODES if mode == "refine" else _EXTRACT_NODES, state)
