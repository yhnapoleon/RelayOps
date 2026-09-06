"""Issue diagnosis — entry point, report schema and the one LLM call.

The "alert → action" gap closer. Evidence collection now lives in the staged
pipeline (``core/agent/diagnose_graph.py``): collect → triage decision tree →
branch-specific enrichment → history retrieval → per-section budget →
structured analysis → programmatic self-check (escalation targets and cited
evidence are verified against the bundle, not just prompted about).

This module keeps what the rest of the platform touches: ``check_access``
(router pre-flight), ``DiagnosisReport`` (字段即契约 — see
docs/AGENT_CHAT_CONTRACT.md), ``run_analysis`` (the temperature-0 structured
call) and ``run_diagnose`` (the SSE event stream).

The draft email is text in the report — nothing here can send mail, close
issues or touch any external system. Live CML reads are best-effort: an
unreachable CML degrades the evidence, never fails the diagnosis.
"""

from __future__ import annotations

import json
from typing import Iterator, List, Optional

from pydantic import BaseModel, Field

from core.auth.jwt import CurrentUser
from core.exceptions import ForbiddenError, NotFoundError
from core.logging import get_logger
from core.models.database import get_db
from core.models.entities import Issue

# Canonical issue-type → scenario-type routing lives in knowledge; re-exported
# here because the diagnosis docs/tests historically import it from this module.
from core.agent.knowledge import ISSUE_SCENARIO_HINTS, domain_card_for_issue  # noqa: F401
from core.agent.llm import get_chat_model
from core.agent.structured import invoke_structured
from core.agent.tools import _accessible_product_ids

logger = get_logger(__name__)

# Evidence bundle serialized into the prompt is capped — the staged pipeline
# budgets per section first, this is only the final hard stop.
_MAX_BUNDLE_CHARS = 30_000


# ── report schema (part of the frontend contract) ────────────────────


class RootCause(BaseModel):
    hypothesis: str = ""
    confidence: str = ""          # high | medium | low
    evidence: str = ""            # which collected facts support this


class EmailDraft(BaseModel):
    to: str = ""                  # from scenario escalation_target / owner_contact only
    subject: str = ""
    body: str = ""


class DiagnosisReport(BaseModel):
    summary: str = ""
    root_causes: List[RootCause] = Field(default_factory=list)
    recommended_steps: List[str] = Field(default_factory=list)
    verification_steps: List[str] = Field(default_factory=list)
    escalation_needed: bool = False
    escalation_target: str = ""
    escalation_reason: str = ""
    email_draft: Optional[EmailDraft] = None


# ── access pre-flight ─────────────────────────────────────────────────


def check_access(session, actor: CurrentUser, issue_id: int) -> Issue:
    """Issue visibility rule (product scope / assignee / creator / admin) —
    cheap enough for a router pre-flight; raises NotFound/Forbidden."""
    issue = session.query(Issue).filter(Issue.id == issue_id).first()
    if issue is None:
        raise NotFoundError("Issue not found")
    product_ids = _accessible_product_ids(session, actor)
    if product_ids is not None and not (
        issue.product_id in product_ids
        or issue.assignee_id == actor.user_id
        or issue.created_by == actor.user_id
    ):
        raise ForbiddenError("No access to this issue")
    return issue


# ── analyze (the one LLM call) ───────────────────────────────────────

DIAGNOSE_SYSTEM = """\
你是 RelayOps 的 Issue 诊断助手，读完证据包后输出结构化诊断报告。规则：

1. 每个根因假设都必须引用证据包里的具体事实（哪次运行、哪条 runbook 条件、哪个历史工单），
   confidence 取 high/medium/low；没有证据支撑的猜测不要写。系统会程序化校验你引用的
   实体是否真的在证据包里，引用不存在的实体该条会被删除。
2. recommended_steps 要把 runbook 的步骤"实例化"成针对当前 Issue 的具体动作（带上 job/app
   名称、时间点、历史工单里被验证过的做法），可执行、按顺序。
3. 升级判断：只有 runbook 场景里给了 escalation_target、且证据显示超出值班可处理范围
   （外部系统问题、需要项目组改逻辑、反复失败）才 escalation_needed=true；
   escalation_target 只能从证据包的 escalation_target / owner_contact 字段里逐字选取，
   编造的联系人会被程序删除。
4. email_draft 只在 escalation_needed=true 时给：收件人=escalation_target，正文包含
   Issue 摘要、已做排查、需要对方做什么、SLA 时间；语言与 runbook 场景原文一致（通常英文）。
5. 注意 issue.handling_state（系统判定的处置状态）：已升级等待外部 → 建议聚焦跟催与
   备选方案；已退回项目组 → 聚焦需要项目组做什么；MMP 治理类 → 处置在 MMP 平台完成，
   给出深链指引而不是技术排查步骤。
6. 整体用英文（Write the report in English），简洁；summary 两三句话讲清"什么坏了、最可能为什么、现在做什么"。

## 本次 Issue 类型的领域语义
{domain_slice}
"""

DIAGNOSE_USER = """\
本次诊断已按以下取证清单收集证据：
{checklist}
{violations_block}
证据包如下（JSON）：

{bundle}
"""


def build_prompt(bundle: dict, *, checklist: Optional[list] = None,
                 violations: Optional[list] = None) -> str:
    text = json.dumps(bundle, ensure_ascii=False, indent=1)
    if len(text) > _MAX_BUNDLE_CHARS:
        text = text[:_MAX_BUNDLE_CHARS] + "\n…(truncated)"
    violations_block = ""
    if violations:
        violations_block = (
            "\n上一轮输出存在以下问题，必须修正（不要重复犯）：\n"
            + "\n".join(f"- {v}" for v in violations) + "\n"
        )
    return DIAGNOSE_USER.format(
        checklist="\n".join(f"- {c}" for c in (checklist or [])) or "-（默认）",
        violations_block=violations_block,
        bundle=text,
    )


def run_analysis(bundle: dict, *, issue_type: str = "", checklist: Optional[list] = None,
                 violations: Optional[list] = None, model=None) -> DiagnosisReport:
    chat = model if model is not None else get_chat_model(temperature=0)
    system = DIAGNOSE_SYSTEM.format(domain_slice=domain_card_for_issue(issue_type))
    return invoke_structured(chat, DiagnosisReport, [
        ("system", system),
        ("human", build_prompt(bundle, checklist=checklist, violations=violations)),
    ])


# ── pipeline (SSE event stream, mirrors the chat contract style) ─────


def run_diagnose(actor: CurrentUser, issue_id: int, *, model=None) -> Iterator[dict]:
    try:
        from core.agent.diagnose_graph import run_pipeline

        payload = None
        for step in run_pipeline(actor, issue_id, model=model):
            if "stage" in step:
                yield {"event": "stage", "data": {"stage": step["stage"]}}
            elif "payload" in step:
                payload = step["payload"]
        if payload is None:
            raise RuntimeError("diagnosis pipeline produced no report")
        _persist_run(actor, issue_id, payload)
        yield {"event": "report", "data": payload}
    except (NotFoundError, ForbiddenError):
        raise  # router maps these to 404/403 before the stream starts
    except Exception as exc:
        logger.opt(exception=True).error("diagnose failed for issue {}", issue_id)
        yield {"event": "error", "data": {"message": str(exc)}}
    finally:
        yield {"event": "done", "data": {}}


# ── concise diagnose (chat / floating-ball path) ─────────────────────
#
# A short, grounded, action-first brief — no root-cause hypotheses, no email
# draft, no evidence dump. It reuses the same evidence bundle as the structured
# pipeline (so it's grounded) but replaces the heavy structured report with a
# ~150-word markdown answer. Routed to by the unified assistant's diagnose intent
# and triggered from the Issue Workbench via the page assistant.

CONCISE_SYSTEM = """\
You are RelayOps's issue-diagnosis assistant. Using ONLY the evidence bundle,
write a SHORT, action-first brief for the on-duty engineer.

Hard rules (grounding — do not break):
- Use only facts present in the bundle. Never invent ids, names, contacts,
  steps, resolutions, or times. If a section has no data, say so plainly.
- Do NOT write root-cause hypotheses or long analysis. No email draft.

Output GitHub-flavored markdown, ≤150 words, in exactly these sections:
**Issue** — one sentence: the asset and what happened, plus handling_state.
**Similar cases** — up to 3 bullets from similar_resolved_issues, each one line:
  `#<id> — <how it was resolved>`. If none, write "None on record."
**Recommended actions** — a short numbered list, primarily the matched runbook
  scenario's action_steps instantiated for this issue (name the job/app). If the
  runbook has no action steps, say so and point to the scenario's
  escalation_target / owner_contact (verbatim from the bundle) or returning to
  the product owner.

Write in English. Keep it tight — this replaces a verbose report on purpose.

## Domain semantics for this issue type
{domain_slice}
"""

CONCISE_USER = "Evidence bundle (JSON):\n\n{bundle}"


def _concise_answer(bundle: dict, issue_type: str, *, model=None) -> str:
    chat = model if model is not None else get_chat_model(temperature=0)
    text = json.dumps(bundle, ensure_ascii=False)
    if len(text) > _MAX_BUNDLE_CHARS:
        text = text[:_MAX_BUNDLE_CHARS] + "\n…(truncated)"
    reply = chat.invoke([
        ("system", CONCISE_SYSTEM.format(domain_slice=domain_card_for_issue(issue_type))),
        ("human", CONCISE_USER.format(bundle=text)),
    ])
    out = getattr(reply, "content", reply)
    if isinstance(out, list):
        out = "".join(p.get("text", "") for p in out if isinstance(p, dict))
    return str(out).strip()


def run_concise_diagnose(actor: CurrentUser, issue_id: int, *, model=None) -> Iterator[dict]:
    """Concise, grounded diagnose as chat-contract events (tool step + markdown
    ``answer`` + ``done``). Mirrors ``run_diagnose``'s stream position (no meta —
    the caller emits it). Access is enforced via the evidence collector."""
    try:
        from core.agent.diagnose_graph import collect_evidence

        state = collect_evidence(actor, issue_id, model=model)
        bundle = state["bundle"]
        # One transparency step so the panel shows it grounded the answer.
        yield {"event": "tool_call", "data": {"name": "relayops_issue_evidence", "args": {"issue_id": issue_id}}}
        yield {"event": "tool_result", "data": {"name": "relayops_issue_evidence", "preview": ""}}
        answer = _concise_answer(bundle, state.get("issue_type", ""), model=model)
        _persist_run(actor, issue_id, {"concise": True, "answer": answer[:8000]})
        yield {"event": "answer", "data": {"text": answer}}
    except (NotFoundError, ForbiddenError):
        raise  # caller (assistant._diagnose_turn) checks access & maps these first
    except Exception as exc:
        logger.opt(exception=True).error("concise diagnose failed for issue {}", issue_id)
        yield {"event": "error", "data": {"message": str(exc)}}
    finally:
        yield {"event": "done", "data": {}}


def _persist_run(actor: CurrentUser, issue_id: int, payload: dict) -> None:
    """Audit trail — a diagnosis the user acted on must be replayable."""
    try:
        from core.models.agent_entities import AgentRun

        session = get_db().get_session()
        try:
            session.add(AgentRun(
                kind="diagnose", user_id=actor.user_id, issue_id=issue_id,
                output=payload,
            ))
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.opt(exception=True).warning("diagnose: AgentRun persist failed (issue {})", issue_id)
