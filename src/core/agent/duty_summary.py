"""LLM executive summary for the daily duty morning report (值班晨报).

The deterministic sections are built in :mod:`core.report.duty_report`;
this module only turns them into a short human-readable headline + risk
highlights, following the diagnose pattern (temperature=0, structured
output). The contract with the caller is one-directional degradation:
:func:`generate_summary` NEVER raises — any problem (agent extra not
installed, llm not configured, gateway timeout, schema violation)
collapses into ``(None, "skipped"/"failed")`` and the report ships
template-only.
"""

from __future__ import annotations

import json
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from core.config import get_config
from core.logging import get_logger

logger = get_logger(__name__)

# Per-section rows fed to the model — the prompt needs the shape of the
# problem, not every row; full lists are rendered deterministically.
_PROMPT_ROWS_PER_SECTION = 10
# Hard cap on the serialized prompt payload.
_PROMPT_CHAR_BUDGET = 15_000


class DutyReportSummary(BaseModel):
    """Structured output schema for the morning-report executive summary."""

    headline: str = Field(
        default="", description="2-3 sentence executive summary in English; lead with the most urgent item"
    )
    risk_highlights: List[str] = Field(
        default_factory=list,
        description="risk points ordered by urgency, each citing a specific Issue (#id), at most 5",
    )
    handover_notes: List[str] = Field(
        default_factory=list,
        description="2-3 concrete recommended actions for today's duty officer",
    )


DUTY_SUMMARY_SYSTEM = """\
You are the RelayOps duty morning-report assistant. The user message is a JSON
morning-report payload produced by deterministic system statistics, containing:
open Issues, SLA overdue / at-risk, MMP pending approval, anomalies and
recoveries from the past 24 hours, duty action records, and a stats block.

Write ALL output (headline, risk_highlights, handover_notes) in English.

Writing rules:
1. Write only from facts in the JSON; every number must come verbatim from the
   stats fields — do not compute or invent.
2. headline: 2-3 sentences on the most important thing right now, priority: SLA
   overdue > SLA at-risk > MMP pending approval > new anomalies. When all is
   well, say so plainly — don't dramatize.
3. risk_highlights: each cites a specific Issue (#id + title fragment), ordered
   by urgency, at most 5; return an empty list if there are no risks.
4. handover_notes: 2-3 concrete recommended actions for today's duty officer
   (e.g. "Prioritize #123, only 25 minutes left to SLA"); return an empty list
   when there's nothing to do.
5. Do not restate the full lists — the tables are rendered by the system; you
   only write the human-readable lead-in.
"""


def _compact(report_data: dict) -> dict:
    """Slim the report payload for the prompt: stats + top rows per section."""

    def _top(section: dict) -> dict:
        return {
            "total": section.get("total", 0),
            "items": section.get("items", [])[:_PROMPT_ROWS_PER_SECTION],
        }

    sla = report_data.get("sla", {})
    last_24h = report_data.get("last_24h", {})
    activity = report_data.get("duty_activity", {})
    return {
        "stats": report_data.get("stats", {}),
        "on_duty": report_data.get("on_duty", []),
        "sla_overdue": _top(sla.get("overdue", {})),
        "sla_at_risk": _top(sla.get("at_risk", {})),
        "mmp_pending": _top(report_data.get("mmp_pending", {})),
        "open_issues": _top(report_data.get("open_issues", {})),
        "created_24h": _top(last_24h.get("created", {})),
        "recovered_24h": _top(last_24h.get("recovered", {})),
        "notable_events": activity.get("notable_events", [])[:_PROMPT_ROWS_PER_SECTION],
    }


def generate_summary(
    report_data: dict, *, model=None
) -> Tuple[Optional[DutyReportSummary], str]:
    """Return ``(summary, status)`` with status in ``ok | skipped | failed``.

    ``skipped`` — summary disabled or LLM not configured (expected state);
    ``failed`` — the call was attempted and went wrong (logged). Either
    way the caller ships the deterministic report. NEVER raises.
    """
    cfg = get_config()
    if not (cfg.duty_report_llm_summary_enabled and cfg.llm_configured):
        return None, "skipped"
    try:
        if model is None:
            from core.agent.llm import get_chat_model

            model = get_chat_model(temperature=0)
        from core.agent.structured import invoke_structured

        payload = json.dumps(_compact(report_data), ensure_ascii=False, indent=1)
        if len(payload) > _PROMPT_CHAR_BUDGET:
            payload = payload[:_PROMPT_CHAR_BUDGET]
        result = invoke_structured(
            model, DutyReportSummary,
            [("system", DUTY_SUMMARY_SYSTEM), ("human", payload)],
        )
        return result, "ok"
    except Exception:
        logger.opt(exception=True).warning(
            "duty summary: LLM call failed; report degrades to template-only"
        )
        return None, "failed"
