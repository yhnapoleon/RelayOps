"""Guide turn — identity-aware "teach me how to use it" branch.

A ReAct agent mounted with the read-only ``ui_capability`` tools plus two
guide-flow affordances:

* ``guide_step`` events — clickable options (visible tabs) + a follow-up
  question, so a vague "教我用" becomes a guided drill-down instead of a wall of
  text; and **T2 high-risk deeplinks** (owner transfer / delete / approval /
  new-entity) that hand the user a "去 X 页面" jump — **never** a confirm_token
  (P14: high-risk ops are guide-only, decoupled from the actor's UI rights).
* ``assist_fill`` — deterministic onboarding fill: reuse the onboarding
  ``field_path`` setter, only on explicit user authorization, no LLM rewrite of
  the value (P7). On success it emits ``form_sync`` so the right pane refreshes.

Same event envelope as ``chat`` / ``write`` (meta → … → done). Degrades like the
others: if langgraph/LLM is missing the dispatcher still routed here, and the
turn surfaces a clean error rather than crashing.
"""
from __future__ import annotations

import uuid
from typing import Iterator, List, Optional

from core.auth.jwt import CurrentUser
from core.logging import get_logger
from core.agent.chat import (
    SHARED_LANG_RULE,
    _events_for_message,
    _get_checkpointer,
    _sanitize_answer,
    _strip_markdown_tables,
    _thread_key,
)
from core.agent.llm import get_chat_model
from core.agent.ui_capability import (
    build_ui_capability_tools,
    tabs_for_role,
    TAB_NARRATIVES,
    _resolve_tab_key,
)

logger = get_logger(__name__)

GUIDE_SYSTEM = """\
你是 RelayOps 运维助手的「引导」分支，帮用户学会使用平台、看懂表单字段。

## 职责
- 用户泛泛问「教我用 / 怎么开始」→ 先调 `guide_offer_tabs` 列出他当前身份能看到的
  页面作为可点选项，并反问他想先看哪个，不要一股脑全讲。
- 问某个具体页面的整体用法 → 调 `ui_explain_tab` 分步讲解；问表单字段 → 调 `ui_explain_field`
  讲解必填/枚举/联动。工具返回 error 或无记录时如实转达（措辞遵循语言规则，默认英文），**绝不**编造平台没有的功能。
- 问**具体的**「怎么做 X / X 按钮是干嘛的 / Y 组件在哪 / 这个流程怎么走」→ 先调 `kb_search`
  检索知识库（已能确定在哪个页面就传 `tab_key`，如 workbench/projects），**只用返回的 KB 片段作答**；
  检索无结果就如实说明没有相关记录（措辞遵循语言规则，默认英文，如 "I have no record of that"），绝不凭记忆编造按钮/位置/步骤。
- 用户想做高危操作（转让 owner / 删除 / 审批 handover / 新建项目等）→ 调
  `guide_high_risk_action`，给他「去对应页面」的引导，**你不能替他执行这些操作**。

## 纪律
- {lang_rule}
- 只讲工具返回的内容；权限以工具事实层为准（看不到的页面别讲）。
- 按用户身份裁剪：不要引导他去他根本看不到的页面。
- **`guide_offer_tabs` / `ui_list_tabs` 的返回是该用户可见页面的全集**：回答里绝不能出现
  其中没有的页面（如 audit / settings / applications 等平台根本没有的功能）——平台没有的别罗列。
- **列页面只靠 `guide_offer_tabs` 生成的可点选卡片**：卡片已经把页面展示给用户了，正文**不要**
  再用 markdown 表格或列表把这些页面复述一遍（更不能凭记忆补全/改名）；正文只说一句引导语
  （如"以上是你可见的页面，想先看哪个？"）即可。
- 调 `ui_explain_tab` / `ui_explain_field` 时，**tab_key/field 必须逐字使用工具返回里的 key**，
  不要改写（如把 `ai-assistant` 写成 `ai_assistant`）。工具返回 error/无记录就如实转达（措辞遵循语言规则，默认英文），绝不编。

## 「怎么 xx」双轨纪律（被问"怎么做 X / 如何 X / 能不能帮我做 X"）
1. **手动轨**：先调 `ui_explain_tab` 给在 UI 手动完成的真实步骤（没记录就说没记录）。
2. **能力轨**：再说明这事**能否在对话里由助手完成**——可写（如 onboarding 建档、关单）则告知
   走接入向导/写入模式生成待确认草案；只读则可直接对话查询；高危（转让 owner/删除/审批/改全局
   角色）只能去页面，调 `guide_high_risk_action` 给"去 X 页面"引导。
3. **问意向**：反问"要在对话里让助手帮你做，还是你自己去页面操作？"（高危/不能时跳过）。
4. 用户选助手来做 → onboarding 用 `assist_fill`、其余转对应流程；**未授权前不执行任何写/填动作**。

## onboarding 填表纪律
- 用户在右侧表单已打开时让你帮填值：先在对话里和他把要填的字段与取值**确认一致**，再写入。
- 一次填**一个**字段用 `assist_fill`；要填**多个**字段、或"把所有 X 改成 Y"这类批量改，
  先把每个目标字段展开成具体 `field_path`（如 `products[0].jobs[0].owner_contact`），
  用 **`assist_fill_fields` 一次性提交**（只刷新一次表单，别循环单条调用）。
- 取值一律照用户给的原文，不改写、不臆造；`field_path` 必须是表单里真实存在的路径，
  拿不准就先问用户或参考上下文里的表单 JSON，**绝不**猜路径。填完简述改了哪些字段。
- 邮件模板字段也按 field_path 写：`...scenarios[i].email_template.{{to|cc|subject|body}}`。
  body 是整段文本（含 `Label: value` 多行表格）。要批量改其中某几行（如把 Application/Group/Table
  统一改成某值），先从上下文表单里读出该 body 原文，**只替换对应行的取值、其余行原样保留**，
  再把整段新 body 作为一个 answer 写回 `...email_template.body`（不要丢掉 intro/签名等其它行）。

当前用户：{username}（角色 {role}）。
"""

# T2 high-risk operations the guide may *deeplink* to (never execute). Each maps
# to the page the user should go to; ``prefill`` is advisory display context.
# No confirm_token is ever attached (P14).
GUIDE_DEEPLINKS = {
    "transfer_owner": {
        "tab": "projects", "label": "Go to My Projects · Members",
        "hint": "Use Transfer Business Owner in the project's Members tab; after the transfer the former owner becomes a Ops Member of that project.",
    },
    "delete_entity": {
        "tab": "projects", "label": "Go to My Projects",
        "hint": "Deletion is irreversible — delete it manually on the relevant asset page.",
    },
    "approve_handover": {
        "tab": "relayops-admin-handover", "label": "Go to Admin Panel · Handover Approval",
        "hint": "Review pending submissions on the approval page and approve/reject them.",
    },
    "create_project": {
        "tab": "projects", "label": "Go to My Projects · New Project",
        "hint": "Click New Project, fill in name/description, optionally bind CML/MMP; add products and assets afterwards.",
    },
    "create_product": {
        "tab": "projects", "label": "Go to My Projects",
        "hint": "Open the project, click Create Product, then add jobs/apps/scenarios under it.",
    },
    "change_global_role": {
        "tab": "relayops-admin-users", "label": "Go to Admin Panel · User Management",
        "hint": "Only admins can change global roles.",
    },
}


def build_guide_graph(actor: CurrentUser, draft_ref: Optional[int], *, model=None,
                      guide_sink: list):
    from langgraph.prebuilt import create_react_agent

    return create_react_agent(
        model if model is not None else get_chat_model(temperature=0),
        _build_guide_tools(actor, draft_ref, guide_sink=guide_sink),
        prompt=GUIDE_SYSTEM.format(username=actor.username, role=actor.role,
                                   lang_rule=SHARED_LANG_RULE),
        checkpointer=_get_checkpointer(),
    )


def _build_guide_tools(actor: CurrentUser, draft_ref: Optional[int], *, guide_sink: list) -> list:
    """ui_capability tools + guide-flow tools (push guide_step/form_sync into
    ``guide_sink``; the run loop drains it into contract events)."""
    from langchain_core.tools import tool

    tools = list(build_ui_capability_tools(actor))
    _guide_state: dict = {}  # per-turn flags (e.g. card already shown)

    @tool
    def kb_search(query: str, tab_key: str = "") -> dict:
        """Search the platform's UI knowledge base for how a page/button/flow
        works. Use for SPECIFIC "how do I X / where is Y / what does the Z
        button do" questions. Pass ``tab_key`` to scope to one page (e.g.
        'workbench', 'projects') when you already know where — the page
        assistant sets the current tab. Returns the top matching KB sections;
        answer ONLY from them, and if there are no results say you have no
        record rather than inventing UI behaviour."""
        from core.agent import retrieval

        scoped = _resolve_tab_key(tab_key) if tab_key else None
        hits = retrieval.search_kb(query, tab=scoped or "", limit=6)
        if not hits:
            return {"results": [],
                    "note": "No KB match. Say you have no record of that rather than guessing."}
        return {"results": [{"tab": h["tab"], "section": h["section"],
                             "sub_view": h["sub_view"], "title": h["title"],
                             "text": h["text"]} for h in hits]}

    @tool
    def guide_offer_tabs() -> dict:
        """Offer the user the pages they can see as clickable options plus a
        follow-up question. Use for vague "teach me / where do I start" asks
        instead of dumping everything. Only PRESENTS choices."""
        options = [
            {"key": t, "label": TAB_NARRATIVES.get(t, {}).get("title", t),
             "desc": TAB_NARRATIVES.get(t, {}).get("summary", "")}
            for t in tabs_for_role(actor.role)
        ]
        if not _guide_state.get("offered"):  # push the clickable card once per turn
            guide_sink.append({"_kind": "guide_step", "options": options,
                               "prompt": "Which page would you like to explore first?"})
            _guide_state["offered"] = True
        # Return the REAL tabs so the model never has to invent a list. This IS the
        # complete, authoritative set for this user.
        return {
            "tabs": [{"key": o["key"], "title": o["label"], "summary": o["desc"]} for o in options],
            "note": "These pages are already shown to the user as clickable buttons. "
                    "This list is the COMPLETE, authoritative set — never add or invent a "
                    "page not in it, and do NOT repeat them as a markdown table.",
        }

    @tool
    def guide_high_risk_action(action_key: str) -> dict:
        """Guide the user to perform a HIGH-RISK action themselves (you cannot do
        it). ``action_key`` ∈ transfer_owner / delete_entity / approve_handover /
        create_project / create_product / change_global_role. Produces a "go to
        page" deeplink (no auto-execute, no confirm)."""
        spec = GUIDE_DEEPLINKS.get(action_key)
        if spec is None:
            return {"error": f"Unknown high-risk action '{action_key}' — no deeplink available."}
        guide_sink.append({
            "_kind": "guide_step", "options": [],
            "prompt": spec["hint"],
            "deeplink": {"tab": spec["tab"], "label": spec["label"]},
        })
        return {"guided": action_key, "tab": spec["tab"],
                "note": "A deeplink (no confirm) was shown to the user."}

    @tool
    def assist_fill(field_path: str, answer: str) -> dict:
        """Fill ONE onboarding draft field deterministically, with the user's
        explicit value. Only call this when the user has clearly authorized
        filling this specific field. ``field_path`` addresses the field (e.g.
        'products[0].apps[0].application_url'); ``answer`` is the exact value to
        write (no rewriting). Requires an active onboarding draft."""
        if draft_ref is None:
            return {"error": "No onboarding draft in progress — open or create one on the right first."}
        try:
            result = assist_fill_draft(actor, draft_ref, [{"field_path": field_path, "answer": answer}])
        except Exception as exc:  # bad field_path etc. — surface, don't guess
            return {"error": str(exc)}
        guide_sink.append({"_kind": "form_sync", "draft_id": draft_ref, "reason": "refilled"})
        return result

    @tool
    def assist_fill_fields(fields: List[dict]) -> dict:
        """Fill MULTIPLE onboarding draft fields in ONE deterministic write, once
        the user has explicitly agreed on the values in chat. Use this instead of
        repeated ``assist_fill`` calls when applying an agreed batch — e.g. "set
        every job's owner_contact to X", or filling several different fields at
        once. ``fields`` is a list of objects ``{"field_path": "...", "answer":
        "..."}``; each ``answer`` is written verbatim (no rewriting), each
        ``field_path`` addresses one field (e.g. 'products[0].jobs[1].owner_contact').
        Requires an active onboarding draft. Prefer a single call with every
        change so the form refreshes only once. A bad field_path raises and
        nothing is written — fix it and retry, never guess."""
        if draft_ref is None:
            return {"error": "No onboarding draft in progress — open or create one on the right first."}
        if not fields:
            return {"error": "No fields given to fill."}
        try:
            answers = [{"field_path": f["field_path"], "answer": f.get("answer", "")} for f in fields]
        except (TypeError, KeyError) as exc:  # malformed list item — surface, don't guess
            return {"error": f"Each entry needs a 'field_path' (and an 'answer'): {exc}"}
        try:
            result = assist_fill_draft(actor, draft_ref, answers)
        except Exception as exc:  # bad field_path etc. — surface, don't guess
            return {"error": str(exc)}
        guide_sink.append({"_kind": "form_sync", "draft_id": draft_ref, "reason": "refilled"})
        return result

    tools.extend([kb_search, guide_offer_tabs, guide_high_risk_action, assist_fill, assist_fill_fields])
    return tools


def assist_fill_draft(actor: CurrentUser, draft_id: int, answers: List[dict]) -> dict:
    """Deterministic onboarding fill (S4.7 / P7): write each ``{field_path,
    answer}`` onto the *current* draft payload via the onboarding setter, leaving
    every other field untouched. No LLM rewrites the value. Raises on a bad
    field_path (a wrong answer can never land on the wrong field)."""
    from core.agent import draft_service
    from core.agent.schemas import ClarificationAnswer
    from core.models.database import get_db

    db = get_db()
    draft = draft_service.get_draft(db, draft_id, actor=actor)
    parsed = [ClarificationAnswer(field_path=a["field_path"], answer=a["answer"]) for a in answers]
    updated = draft_service.save_draft(db, draft_id, actor=actor, payload=draft.payload or {}, answers=parsed)
    return {
        "filled": [a["field_path"] for a in answers],
        "draft_id": updated.id,
        "status": updated.status,
    }


def run_turn(
    actor: CurrentUser,
    message: str,
    thread_id: Optional[str] = None,
    draft_ref: Optional[int] = None,
    *,
    model=None,
) -> Iterator[dict]:
    """One guide turn → contract events. Emits ``guide_step`` (options + prompt,
    or a T2 deeplink without a confirm_token) and ``form_sync`` (after a
    deterministic assist_fill). Never commits a high-risk action."""
    thread_id = (thread_id or "").strip() or uuid.uuid4().hex
    yield {"event": "meta", "data": {"thread_id": thread_id, "intent": "guide"}}

    try:
        guide_sink: list = []
        graph = build_guide_graph(actor, draft_ref, model=model, guide_sink=guide_sink)
        config = {"configurable": {"thread_id": _thread_key(actor, thread_id)}}
        answer = ""
        called: set = set()
        for update in graph.stream({"messages": [("human", message)]}, config, stream_mode="updates"):
            for _node, payload in (update or {}).items():
                for msg in (payload or {}).get("messages", []) or []:
                    for event in _events_for_message(msg):
                        if event["event"] == "_answer":
                            answer = event["data"]["text"]
                        else:
                            if event["event"] == "tool_call":
                                called.add(event["data"].get("name", ""))
                            yield event
            # Drain guide-flow events produced by tools this update.
            while guide_sink:
                yield from _emit_guide_event(guide_sink.pop(0))

        answer = _sanitize_answer(answer)
        # If the tab list was already shown as a card, any markdown table in the
        # answer is a fabricated page list — strip it.
        if called & {"guide_offer_tabs", "ui_list_tabs"}:
            answer = _strip_markdown_tables(answer)
        yield {"event": "answer", "data": {"text": answer}}
    except Exception as exc:
        logger.opt(exception=True).error("guide turn failed (thread {})", thread_id)
        yield {"event": "error", "data": {"message": str(exc)}}
    finally:
        yield {"event": "done", "data": {}}


def _emit_guide_event(item: dict) -> Iterator[dict]:
    kind = item.get("_kind")
    if kind == "guide_step":
        data = {k: v for k, v in item.items() if k != "_kind"}
        yield {"event": "guide_step", "data": data}
    elif kind == "form_sync":
        yield {"event": "form_sync", "data": {"draft_id": item.get("draft_id"),
                                              "reason": item.get("reason", "refilled")}}


def guide_available() -> tuple[bool, str]:
    from core.agent.chat import chat_available

    return chat_available()
