"""Entities for the onboarding agent (drafts + run audit)."""

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from core.models.database import Base


class AgentRun(Base):
    """Audit trail of one agent execution (currently: issue diagnoses).

    Lets Ops answer "why did the AI recommend that" after the fact — the
    report a responder acted on is replayable from here, independent of any
    chat session memory.
    """

    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(32), nullable=False)  # e.g. "diagnose"
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    issue_id = Column(Integer, nullable=True)
    output = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AgentWriteProposal(Base):
    """A pending write proposal (propose → confirm), persisted so it survives
    across workers/replicas — the confirm request may land on a different
    process than the one that produced it (the old in-process cache could not).

    Bound to its creator (``actor_id``), a TTL (``expires_at``) and single
    consumption (``consumed_at`` set exactly once on commit). The ``proposal``
    JSON is a ``WriteProposal`` dump; the token is its primary key.
    """

    __tablename__ = "agent_write_proposals"

    token = Column(String(64), primary_key=True)
    actor_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(32), nullable=False)
    proposal = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)


class AgentConversation(Base):
    """A persisted chat conversation — the viewable record of a thread.

    Keyed by ``(user_id, thread_id)`` so each SSE thread maps to exactly one
    conversation row and turns append to it. Unlike :class:`AgentRun`
    (kind=chat) — a flat audit trail for prompt regression — this is the
    user-facing history: a titled, ordered list of messages they can reopen.
    Private to its creator (cascades on user delete).
    """

    __tablename__ = "agent_conversations"
    __table_args__ = (UniqueConstraint("user_id", "thread_id", name="uq_agent_conv_user_thread"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    thread_id = Column(String(64), nullable=False)
    title = Column(String(200), nullable=True, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages = relationship(
        "AgentMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AgentMessage.id",
    )


class AgentMessage(Base):
    """One message inside an :class:`AgentConversation` (user turn or the
    assistant's answer). The assistant's tool steps are kept as a JSON list of
    ``{name, args}`` so the history view can redraw the "queried N sources"
    block; full tool payloads and artifacts are intentionally not persisted."""

    __tablename__ = "agent_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(
        Integer, ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False
    )
    role = Column(String(16), nullable=False)  # "user" | "assistant"
    content = Column(Text, nullable=False, default="")
    steps = Column(JSON, nullable=True)  # assistant only: [{name, args}]
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("AgentConversation", back_populates="messages")


class OnboardingDraft(Base):
    """One handover-document ingestion: extracted payload, validation state,
    review edits and the final submit outcome. The draft row — not any chat
    context — is the single source of truth for what gets created.

    Status flow: extracting → ready ⇄ refining → submitted → completed | failed.
    ``refining`` is the multi-round review loop (reviewer answers fed back to
    the LLM for a second-pass fill). ``failed`` covers extraction, refine and
    submit failures; ``error`` says which. A failed draft stays editable
    (back to ready via the next save).

    Two modes, decided at ingest and fixed for the draft's life:
    ``target_project_id`` NULL creates a brand-new project on submit; set, the
    document is folded into that existing project instead — new assets created,
    existing ones diffed and patched (see core.agent.project_update).
    """

    STATUS_EXTRACTING = "extracting"
    STATUS_READY = "ready"
    STATUS_REFINING = "refining"
    STATUS_SUBMITTED = "submitted"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    __tablename__ = "onboarding_drafts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # When one handover document spans multiple CML projects the reviewer can
    # split it into sibling drafts (one per project) that share this id; null
    # for an ordinary single-project draft.
    group_id = Column(String(64), nullable=True, index=True)
    status = Column(String(32), nullable=False, default=STATUS_EXTRACTING)
    source_filename = Column(String(512), nullable=True, default="")
    source_text = Column(Text, nullable=False, default="")
    # The most recently imported Control-M sheet, flattened to text — kept so the
    # chat assistant can answer about it (adhoc/rerun rows, dependencies, alert
    # thresholds) that the merge into jobs intentionally drops.
    controlm_sheet_text = Column(Text, nullable=True)
    # Set = "add assets to this existing project" mode. No FK cascade on
    # purpose: deleting the project must not erase the import record, and the
    # submit path re-checks the project exists anyway.
    target_project_id = Column(Integer, nullable=True, index=True)
    payload = Column(JSON, nullable=True)        # OnboardingDraftPayload dict
    validation = Column(JSON, nullable=True)     # ValidationReport dict
    # ProjectDiff dict — update mode only; derived, refreshed on every save.
    diff = Column(JSON, nullable=True)
    result = Column(JSON, nullable=True)         # submit report (created ids + binding status)
    error = Column(Text, nullable=True)
    submitted_project_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
