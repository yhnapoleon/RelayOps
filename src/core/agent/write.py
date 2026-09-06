"""Write turn — a ReAct agent mounted with propose-only ``draft_*`` tools.

Mirrors ``chat.run_turn``'s event envelope (meta → … → done) so the API layer
and the frontend treat it uniformly. The difference: the model can call a write
tool, which produces a :class:`WriteProposal`; the turn persists it (DB-backed
token) and emits a ``write_proposal`` event, then ends the turn. There is NO
langgraph ``interrupt`` (Δ2) — confirmation is a separate, stateless endpoint.
"""
from __future__ import annotations

import uuid
from typing import Iterator, Optional

from core.auth.jwt import CurrentUser
from core.logging import get_logger
from core.agent.chat import SHARED_LANG_RULE, _events_for_message, _get_checkpointer, _thread_key
from core.agent.llm import get_chat_model
from core.agent.write_schemas import WriteProposal
from core.agent.write_tools import build_write_tools

logger = get_logger(__name__)

WRITE_SYSTEM = """\
你是 RelayOps 运维助手的「写入模式」，服务银行 ML 平台的值班团队。

## 语言（最高优先级，先于一切）
{lang_rule}
- 工具返回的 title / impact_note / clarification 等文案可能是中文，那只是内部数据；
  你**面向用户的回答必须镜像用户本轮的语言**——用户用英文提问就用英文作答，绝不能因为
  工具文案或本提示是中文就擅自改用中文。

## 你拥有的两类工具
1. **只读查询工具**（relayops_* / cml_live_* / mmp_live_* 等，和只读模式完全相同）：用来
   **先查清楚到底要改哪些实体**。例如用户说"关掉产品 X 当前所有 open issue"，先
   `relayops_list_issues(status="open", product_id=...)`（要含处理中再补一次 status="in_progress"）
   拿到每个 issue_id，再据此发起写操作。需要解析 #id、确认实体存在/状态时也用它们。
2. **写操作工具**（draft_*）：它们**只产出"待确认的拟变更"**，不会真正落库——
   变更要等用户在界面上点确认后，才由确定性代码提交。

## 核心纪律
- 选对 draft 工具，从用户消息或**只读工具返回**里取实体 id 和理由，产出拟变更草案；
- **绝不**说"已完成/已关单/已修改"——你没有提交能力，草案要用户确认后系统才提交；
- draft 工具返回 error/clarification 时如实转达，不要编造，也不要假装成功；
- 用户没有明确的写操作意图时，说明你能代办什么即可，不要硬造变更；
- 只做用户明确要求的变更，**不要顺带改别的**；id、理由只能来自用户消息或工具返回，不得编造。

## 批量变更（关键：用户要一次改多个实体时）
当用户要求一次处理多个实体（如"关掉 #1 #2 #3"、"把产品 X 当前所有 open issue 都标记误报"）：
1. 需要时先用只读工具列出每个目标实体的 id；
2. **对每个实体分别调用一次对应的 draft 工具**（有 N 个就调 N 次）——每次产出一份独立草案，
   界面会为每份显示一张确认卡，用户可逐张确认或一键全确认；
3. **绝不**因为"一次只能改一个"就只改第一个、忽略其余的——那是错误的；也不要试图把多个实体
   塞进同一次工具调用。

## 工具选择（按用户要改什么选对 draft 工具）
- 关单/标记误报 → draft_resolve_issue_tool / draft_false_positive_tool；
  改 issue 状态(open↔in_progress)/记录步骤 → draft_update_issue_status_tool / draft_record_step_tool；
- **改某个 job 的排程 cron（"把 cron 推迟几小时/改成 X"）→ draft_edit_cron_tool**（接受用户给的新 cron）；
  **改某个 job 的 staleness 阈值（"收紧到 5 小时/设成 N 分钟"）→ draft_set_sla_threshold_tool**（接受用户给的分钟数）；
  让系统按历史自动重算 cron/阈值才用 draft_job_sla_tool；
- 编辑 project/product 描述 → draft_edit_project_tool / draft_edit_product_tool；改成员角色 → draft_set_member_role_tool。
- 没有对应 draft 工具的写操作（如转让 owner、删除、审批、改全局角色、改外部 Control-M 调度器本身）：
  如实说"这个我没有写入工具，只能去对应页面手动做"，**绝不**承诺一个不存在的草案。

当前用户：{username}（角色 {role}）。
"""


def build_write_graph(actor: CurrentUser, *, model=None, proposal_sink: list,
                      artifact_sink: Optional[list] = None):
    """ReAct agent for write mode. Bound with BOTH the read-only query tools
    (so the model can look up which entities to act on — essential for batch
    requests like "close all open issues for product X") and the propose-only
    ``draft_*`` write tools (the security boundary: only T1 ops are registered)."""
    from langgraph.prebuilt import create_react_agent

    from core.agent.tools import build_langchain_tools

    tools = build_langchain_tools(actor, artifact_sink=artifact_sink) + build_write_tools(
        actor, proposal_sink=proposal_sink
    )
    return create_react_agent(
        model if model is not None else get_chat_model(temperature=0),
        tools,
        prompt=WRITE_SYSTEM.format(username=actor.username, role=actor.role,
                                   lang_rule=SHARED_LANG_RULE),
        checkpointer=_get_checkpointer(),
    )


def run_turn(
    actor: CurrentUser,
    message: str,
    thread_id: Optional[str] = None,
    *,
    model=None,
) -> Iterator[dict]:
    """One write turn → contract events. Emits one ``write_proposal`` (each with
    its own persisted confirm token) per proposed change — so a batch request
    ("close these 3 issues") yields three independently-confirmable cards. Never
    commits."""
    thread_id = (thread_id or "").strip() or uuid.uuid4().hex
    yield {"event": "meta", "data": {"thread_id": thread_id, "intent": "write"}}

    try:
        proposal_sink: list = []
        artifact_sink: list = []
        graph = build_write_graph(
            actor, model=model, proposal_sink=proposal_sink, artifact_sink=artifact_sink
        )
        config = {"configurable": {"thread_id": _thread_key(actor, thread_id)}}
        answer = ""
        for update in graph.stream({"messages": [("human", message)]}, config, stream_mode="updates"):
            for _node, payload in (update or {}).items():
                for msg in (payload or {}).get("messages", []) or []:
                    for event in _events_for_message(msg):
                        if event["event"] == "_answer":
                            answer = event["data"]["text"]
                        else:
                            yield event
            # Read tools may emit chart/table artifacts — drain them to SSE so
            # row data never re-enters the model context (mirrors chat.run_turn).
            while artifact_sink:
                yield {"event": "artifact", "data": artifact_sink.pop(0)}

        proposals = _collect_proposals(proposal_sink)
        for proposal in proposals:
            token = _persist_proposal(actor, proposal)
            stamped = proposal.model_copy(update={"confirm_token": token})
            yield {"event": "write_proposal", "data": stamped.model_dump()}

        if proposals and not answer:
            if len(proposals) == 1:
                answer = f"Please review and confirm this change on the right: {proposals[0].title}"
            else:
                answer = (
                    f"Please review and confirm these {len(proposals)} changes on the right "
                    "(you can confirm them individually or all at once)."
                )

        _persist_turn(actor, thread_id, message, answer, proposals)
        yield {"event": "answer", "data": {"text": answer}}
    except Exception as exc:
        logger.opt(exception=True).error("write turn failed (thread {})", thread_id)
        yield {"event": "error", "data": {"message": str(exc)}}
    finally:
        yield {"event": "done", "data": {}}


def _collect_proposals(sink: list) -> list[WriteProposal]:
    """All distinct proposals the model produced this turn, in order. Deduped on
    (kind, entity, field→new_value) so a model that double-calls the same draft
    tool doesn't surface two identical confirm cards."""
    out: list[WriteProposal] = []
    seen: set = set()
    for item in sink:
        if not (isinstance(item, dict) and item.get("kind")):
            continue
        proposal = WriteProposal.model_validate(item)
        sig = (
            proposal.kind,
            proposal.entity_type,
            proposal.entity_id,
            tuple((c.field_path, c.new_value) for c in proposal.changes),
        )
        if sig in seen:
            continue
        seen.add(sig)
        out.append(proposal)
    return out


def _persist_proposal(actor: CurrentUser, proposal: WriteProposal) -> str:
    from core.models.database import get_db
    from core.agent import write_proposal_store as store

    session = get_db().get_session()
    try:
        return store.put(session, actor_id=actor.user_id, proposal=proposal)
    finally:
        session.close()


def _persist_turn(actor: CurrentUser, thread_id: str, question: str, answer: str,
                  proposals: list[WriteProposal]) -> None:
    """Audit (kind=write). Best-effort; never blocks the stream."""
    try:
        from core.models import database as db_module
        from core.models.agent_entities import AgentRun

        if getattr(db_module, "_db", None) is None:
            return
        session = db_module.get_db().get_session()
        try:
            session.add(AgentRun(
                kind="write", user_id=actor.user_id,
                output={
                    "thread_id": thread_id, "question": question[:2000], "answer": answer[:8000],
                    # Keep the legacy single-proposal key for back-compat readers;
                    # add the full list for batch turns.
                    "proposal": proposals[0].model_dump() if proposals else None,
                    "proposals": [p.model_dump() for p in proposals],
                },
            ))
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.opt(exception=True).warning("write: AgentRun persist failed")
