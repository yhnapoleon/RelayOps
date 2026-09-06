"""LLM extraction step — the only place in the onboarding flow that calls a model.

Single structured-output call at temperature 0, sandwiched by deterministic
code: URL-derived anchors (``doc_signals``) go in as a hint block and come
back out as overrides, so identifiers the document states in URLs never
depend on the model. If extraction quality needs work, this is the one file
to tune.
"""

from __future__ import annotations

from core.agent.doc_signals import (
    apply_doc_signals,
    derive_doc_signals,
    drop_hallucinated_assets,
    hints_block,
)
from core.agent.llm import AgentDependencyError, LlmNotConfiguredError, get_chat_model
from core.agent.prompts import ONBOARDING_EXTRACT_SYSTEM, ONBOARDING_EXTRACT_USER
from core.agent.schemas import OnboardingDraftPayload
from core.agent.structured import invoke_structured
from core.logging import get_logger

logger = get_logger(__name__)


def extraction_available() -> tuple[bool, str]:
    """(usable, human-readable reason when not)."""
    try:
        get_chat_model()
    except (AgentDependencyError, LlmNotConfiguredError, NotImplementedError) as exc:
        return False, str(exc)
    except Exception as exc:  # constructor-level surprises shouldn't 500 the probe
        return False, str(exc)
    return True, ""


def run_extraction(document_text: str) -> OnboardingDraftPayload:
    """One temperature-0 structured-output call. Raises on any failure —
    the caller (draft service) owns marking the draft failed."""
    signals = derive_doc_signals(document_text)
    chat = get_chat_model(temperature=0)
    logger.info(
        "Onboarding extraction: {} chars of document text, anchors: cml={} mmp={} apps={}",
        len(document_text), signals.cml_project_names, signals.mmp_project_ids,
        [a.subdomain for a in signals.app_links],
    )
    payload = invoke_structured(chat, OnboardingDraftPayload, [
        ("system", ONBOARDING_EXTRACT_SYSTEM),
        ("human", ONBOARDING_EXTRACT_USER.format(
            document=document_text, hints=hints_block(signals))),
    ])
    payload = apply_doc_signals(payload, signals)
    # Drop invented identifiers / pure-hallucination assets last, so the
    # signal-materialized stubs (which carry a real binding) survive.
    return drop_hallucinated_assets(payload, document_text)
