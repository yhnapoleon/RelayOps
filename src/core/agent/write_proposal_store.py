"""Persisted store for pending write proposals (propose → confirm).

Replaces the design's original in-process cache (Δ3): a confirm request may
land on a different worker/replica than the one that produced the proposal, so
the token → proposal mapping lives in the DB (``agent_write_proposals``).

Guarantees:
  * **ownership** — only the actor who created the token can take it;
  * **expiry** — a TTL (default 15 min) past which the token is dead;
  * **single consume** — ``consume`` flips ``consumed_at`` atomically, so a
    committed proposal can't be replayed.

All functions take an explicit ``session`` so they unit-test against an
in-memory DB without any global wiring.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional, Tuple

from core.models.agent_entities import AgentWriteProposal
from core.agent.write_schemas import WriteProposal

DEFAULT_TTL_SECONDS = 900  # 15 minutes

# reason codes returned by take_valid → the API maps these to HTTP status.
TAKE_OK = "ok"
TAKE_NOT_FOUND = "not_found"   # 404
TAKE_FORBIDDEN = "forbidden"   # 403 — token belongs to another actor
TAKE_EXPIRED = "expired"       # 410
TAKE_CONSUMED = "consumed"     # 409 — already committed


def put(session, *, actor_id: int, proposal: WriteProposal, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Persist a proposal, return its fresh confirm token. Stamps the token onto
    the stored ``proposal`` JSON so the preview/confirm always echoes it back."""
    token = uuid.uuid4().hex
    proposal = proposal.model_copy(update={"confirm_token": token})
    now = datetime.utcnow()
    row = AgentWriteProposal(
        token=token,
        actor_id=actor_id,
        kind=proposal.kind,
        proposal=proposal.model_dump(),
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        consumed_at=None,
    )
    session.add(row)
    session.commit()
    return token


def take_valid(session, *, token: str, actor_id: int) -> Tuple[Optional[WriteProposal], str]:
    """Validate ownership + expiry + not-yet-consumed and return the proposal.

    Does NOT consume — the caller commits first, then calls :func:`consume`, so a
    service failure leaves the token usable for a retry. Returns
    ``(proposal | None, reason)`` where reason is one of the TAKE_* codes.
    """
    row = session.get(AgentWriteProposal, token)
    if row is None:
        return None, TAKE_NOT_FOUND
    if row.actor_id != actor_id:
        return None, TAKE_FORBIDDEN
    if row.consumed_at is not None:
        return None, TAKE_CONSUMED
    if row.expires_at is not None and datetime.utcnow() >= row.expires_at:
        return None, TAKE_EXPIRED
    return WriteProposal.model_validate(row.proposal), TAKE_OK


def consume(session, *, token: str) -> bool:
    """Mark the token consumed exactly once. Atomic compare-and-set on
    ``consumed_at IS NULL``; returns True iff this call claimed it (False if a
    concurrent confirm already did)."""
    now = datetime.utcnow()
    updated = (
        session.query(AgentWriteProposal)
        .filter(AgentWriteProposal.token == token, AgentWriteProposal.consumed_at.is_(None))
        .update({AgentWriteProposal.consumed_at: now}, synchronize_session=False)
    )
    session.commit()
    return updated == 1


def purge_expired(session, *, now: Optional[datetime] = None) -> int:
    """Delete expired-and-unconsumed and long-consumed rows. Returns the count
    removed. Meant for a periodic maintenance task; safe to skip in tests."""
    now = now or datetime.utcnow()
    removed = (
        session.query(AgentWriteProposal)
        .filter(
            (AgentWriteProposal.expires_at < now)
            | (AgentWriteProposal.consumed_at.isnot(None))
        )
        .delete(synchronize_session=False)
    )
    session.commit()
    return removed
