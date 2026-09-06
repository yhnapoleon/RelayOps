"""Unified assistant dispatcher (v2 §2.2, Δ1).

A thin layer: classify the turn's intent, then **delegate** to the existing
capability entry point. There is no nested orchestration graph — qa keeps using
``chat.run_turn`` unchanged; write uses ``write.run_turn``. Later phases hang
diagnose / onboarding / guide off the same dispatch. The dispatcher itself does
not import langgraph; only the branches do (each with its own degradation).
"""
from __future__ import annotations

import re
import uuid
from typing import Iterator, List, Optional, Tuple

from core.auth.jwt import CurrentUser
from core.logging import get_logger
from core.agent import chat, write

logger = get_logger(__name__)

# Intents the dispatcher can route to. write + onboarding also have explicit
# input-box toggles (mode=...); the read-only chains are auto-classified.
_INTENTS = ("qa", "diagnose", "onboarding", "guide", "write", "capability")
# Read-only chains the light classifier may pick. A wrong guess here is cheap
# (still read-only); write is NEVER an auto outcome — it is reachable only via
# the explicit Write-mode toggle (mode='write').
_READONLY_LABELS = ("diagnose", "guide", "capability")

# Diagnose: explicit root-cause / why-failed asks. Onboarding: bringing a new
# entity online / importing a handover. Both are lower-risk than write.
_DIAGNOSE_PATTERNS = (
    r"诊断",
    r"\bdiagnose\b",
    r"根因|根本原因|排查",
    r"为什么.*(失败|挂|报错|offline)",
)
_ONBOARDING_PATTERNS = (
    r"接入|上线|录入|建档",
    r"\bonboard",
    r"新建.*(项目|product|job)",
    r"导入.*(交接|文档|handover)",
)
# Guide: "teach me to use the platform / where is X / how do I fill this field".
# Scoped to UI usage so runbook "how do I handle this issue" stays qa.
_GUIDE_PATTERNS = (
    r"怎么(用|使用|操作|填|找|进入)",
    r"教(我|程)",
    r"如何(使用|操作|找到|进入|新建)",
    r"在哪(里|儿)?(找|看|设置|改|配置)",
    r"\bhow (do i|to) use\b",
    r"\bguide me\b",
    r"walk me through",
    # UI/tab explanation — route to guide, NOT capability-blurb or qa-fabrication
    # (RELIABILITY_PLAN §12 A; 2nd-round 第9-18轮 disasters). Anchored on a UI
    # noun (tab/页面/page) so data questions ("有几个 job") never match.
    r"(tab|页面|标签页|page).{0,8}(怎么用|功能|作用|是做什么|做什么|干嘛|干什么)",
    r"(介绍|讲解|说明).{0,20}(tab|页面|标签页|page)",
    r"(有几个|有哪些|多少个|几个).{0,4}(tab|页面|标签页|page)",
    r"(我有哪些|可见|能看到).{0,6}(页面|tab|page)",
    r"\bwhat\b.{0,20}\b(tab|page)s?\b",
    r"\b(which|how many)\b.{0,12}\b(tab|page)s?\b",
    r"\bexplain\b.{0,20}\b(tab|page)s?\b",
)

# Fill-the-open-form requests — only consulted when an onboarding draft is open
# (draft_ref / form_snapshot present). Routes to the guide chain, which is the
# only read-only chain carrying the deterministic `assist_fill` / `assist_fill_fields`
# tools. Without an open form these never fire, so normal qa is unaffected.
_ONBOARDING_FILL_PATTERNS = (
    r"填(进|入|上|好|一下|进去|到表|表)",
    r"帮.{0,4}填",
    r"写(进|入|到).{0,6}(表|字段|form|field)",
    r"把.{0,30}(改|设|填|换)成",
    r"批量(改|填|替换|设置)",
    r"\bfill\b.{0,20}\b(form|field|in|out)\b",
    r"\bapply\b.{0,20}\b(form|field|change|to)\b",
    r"\bset (all|every|the)\b.{0,30}\bto\b",
)

# Issue reference in free text: "#123" wins; else the first bare integer.
_ISSUE_HASH = re.compile(r"#(\d+)")
_ISSUE_BARE = re.compile(r"\b(\d{1,9})\b")

# Capability / "what can you do" — answered with a static blurb (no tools, no DB).
_CAPABILITY_PATTERNS = (
    r"你能(做|干|帮.{0,6}做|提供).{0,4}(什么|哪些|啥)",
    r"你(会|可以|能).{0,6}(做什么|干什么|帮我做什么|帮什么)",
    r"你的(能力|功能|作用)",
    r"有(哪些|什么).{0,4}功能",
    r"你是(谁|什么|干嘛的|做什么的)",
    r"\bwhat can (you|the assistant) (do|help)",
    r"\bwhat (are you( able)?|can you).{0,12}(do|help|capab)",
    r"\byour capabilit",
    r"\bhow can you help\b",
)

# Obvious data/lookup vocabulary → straight to qa, skipping the classifier so the
# dominant question path pays zero extra latency.
_QA_FASTPATH = re.compile(
    r"issue|工单|告警|alert|sla|mttr|\bjob\b|作业|\bapp\b|应用|product|产品|"
    r"project|项目|\brun\b|运行|失败|\bfail|error|报错|健康|health|漂移|drift|"
    r"值班|on.?call|duty|误报|false\s*positive|统计|趋势|分布|占比|图表|chart|"
    r"审批|approval|\bmmp\b|\bcml\b|owner|负责人|超时|breach|逾期|多少|几个",
    re.IGNORECASE,
)


def classify_intent(message: str, actor: CurrentUser, *, mode: Optional[str] = None,
                    draft_open: bool = False) -> Tuple[str, str]:
    """Return ``(intent, route_reason)``.

    Order: explicit ``mode`` toggle → high-precision keyword fast-paths (no
    model call) → obvious-data fast-path to qa → one cheap light-model
    classification constrained to the **read-only** chains → ``qa`` fallback.

    write is intentionally absent from auto-routing: it is reachable only via
    the explicit Write-mode toggle (``mode='write'``). A typed change request
    therefore stays read-only and the qa agent nudges the user to flip the
    toggle — nothing is ever silently routed into a write proposal (P1). The
    light classifier can only return a read-only label, so a misclassification
    is always harmless.
    """
    if mode in _INTENTS:
        return mode, f"manual:{mode}"

    text = (message or "").strip()
    low = text.lower()
    # Deterministic keyword fast-paths — zero latency, cover the clear cases.
    for pat in _DIAGNOSE_PATTERNS:
        if re.search(pat, low):
            return "diagnose", "keyword"
    for pat in _ONBOARDING_PATTERNS:
        if re.search(pat, low):
            return "onboarding", "keyword"
    for pat in _GUIDE_PATTERNS:
        if re.search(pat, low):
            return "guide", "keyword"
    for pat in _CAPABILITY_PATTERNS:
        if re.search(pat, low):
            return "capability", "keyword"
    # Open onboarding form + a "fill this in / set all X to Y" request → guide,
    # the chain that can actually write the draft. Checked before the qa fast-path
    # so words like "owner" don't divert an agreed batch-fill into read-only qa.
    if draft_open:
        for pat in _ONBOARDING_FILL_PATTERNS:
            if re.search(pat, low):
                return "guide", "form_fill"
    # Obvious data/lookup question → qa without a classifier round-trip.
    if _QA_FASTPATH.search(low):
        return "qa", "fastpath_qa"

    # Ambiguous free text → one light-model call, read-only labels only.
    intent = _llm_classify(text, actor)
    if intent is not None:
        return intent, "llm"
    return "qa", "fallback_qa"


def _llm_classify(text: str, actor: CurrentUser) -> Optional[str]:
    """One cheap, temperature-0 classification constrained to the read-only
    chains. Returns one of ``_READONLY_LABELS`` or ``"qa"``; ``None`` when the
    light model is unavailable (caller then falls back to qa — write is never a
    possible outcome here)."""
    try:
        from core.agent.llm import (
            AgentDependencyError,
            LlmNotConfiguredError,
            get_chat_model,
        )

        model = get_chat_model(tier="light", temperature=0)
        prompt = (
            "Classify the user's request into exactly one label and reply with "
            "only that word.\n"
            "- diagnose: wants the root cause of a specific failing issue/job.\n"
            "- guide: wants to learn how to USE the platform/UI — where a page "
            "is, how to operate a feature, what an onboarding form field means.\n"
            "- capability: asks what the assistant itself can do, or who it is.\n"
            "- qa: anything else — data lookups, analysis, explanations, and how "
            "to handle/resolve an issue (runbook guidance).\n\n"
            f"User: {text}\nLabel:"
        )
        reply = model.invoke(prompt)
        out = getattr(reply, "content", reply)
        if isinstance(out, list):
            out = "".join(p.get("text", "") for p in out if isinstance(p, dict))
        out = str(out).strip().lower()
        for label in _READONLY_LABELS:
            if label in out:
                return label
        return "qa"
    except (AgentDependencyError, LlmNotConfiguredError, NotImplementedError):
        return None
    except Exception:
        logger.opt(exception=True).warning("intent classification failed; falling back to qa")
        return None


def _parse_issue_ref(message: str) -> Optional[int]:
    """Best-effort issue id from free text: '#123' first, else the first bare
    integer. Returns None when nothing plausible is present."""
    m = _ISSUE_HASH.search(message or "")
    if m:
        return int(m.group(1))
    m = _ISSUE_BARE.search(message or "")
    return int(m.group(1)) if m else None


def run_turn(
    actor: CurrentUser,
    message: str,
    thread_id: Optional[str] = None,
    *,
    mode: Optional[str] = None,
    draft_ref: Optional[int] = None,
    form_snapshot: Optional[dict] = None,
    page_context: Optional[dict] = None,
    model=None,
) -> Iterator[dict]:
    """One turn → contract events. Classifies, then delegates to the matching
    capability entry point (each yields a full meta…done stream).

    ``mode`` / ``draft_ref`` / ``form_snapshot`` are optional; omitting them all
    behaves exactly like today's chat (read-only Q&A + auto routing).

    ``page_context`` (``{"tab": ..., "sub_view": ...}``) is set by the page
    assistant (floating ball): the turn is forced **read-only** (write mode is
    ignored) and grounded in the current page's KB, so the same guide/qa engine
    powers the FAB and the full-screen assistant."""
    # Page assistant: always read-only, grounded in the current page's KB. Force
    # mode off (never a write proposal from the ball) and classify on the raw
    # message (before the preamble) so routing isn't biased by injected text.
    page_preamble = _page_context_preamble(page_context) if page_context else None
    if page_context:
        mode = None
    draft_open = draft_ref is not None or bool((form_snapshot or {}).get("payload"))
    intent, reason = classify_intent(message, actor, mode=mode, draft_open=draft_open)
    logger.info("assistant route: intent={} reason={} user={} page={}",
                intent, reason, actor.user_id, (page_context or {}).get("tab"))
    # Both surfaces (full-screen assistant AND the floating ball) default to
    # English (spec). The shared prompts are Chinese-authored and only *softly*
    # default to English, so every turn gets a HARD per-turn directive keyed on
    # the user's actual language: English unless the raw message itself contains
    # Chinese. Computed on the raw message before any preamble is injected, so a
    # Chinese page-KB/form preamble can't flip an English user's answer to
    # Chinese. This overrides any drift toward the prompt's own language.
    directive = ("请用中文回答（用户在用中文）。" if _has_cjk(message)
                 else "Respond in English (the user wrote in English). "
                      "Do not answer in Chinese.")
    if page_context:
        parts = [p for p in (page_preamble, directive) if p]
        message = "\n\n".join(parts + [f"User question: {message}"])
    else:
        message = f"{directive}\n\n{message}"

    if intent == "write":
        yield from write.run_turn(actor, message, thread_id, model=model)
    elif intent == "capability":
        yield from _capability_turn(actor, thread_id, message)
    elif intent == "diagnose":
        issue_id = _parse_issue_ref(message)
        if issue_id is not None:
            yield from _diagnose_turn(actor, thread_id, issue_id, model=model)
        else:
            # No parseable issue ref → fall back to read-only Q&A (the chat
            # agent can ask which issue). Keeps the turn useful, not a dead end.
            yield from chat.run_turn(actor, message, thread_id, model=model)
    elif intent == "onboarding":
        yield from _onboarding_turn(actor, thread_id, draft_ref, form_snapshot)
    elif intent == "guide":
        from core.agent import guide

        # When the onboarding form is open, ground the guide turn in the live form
        # so it can resolve concrete field_paths for a batch `assist_fill_fields`
        # (e.g. "set every job's owner_contact") instead of guessing.
        msg = message
        if draft_open:
            preamble = _form_context_preamble(form_snapshot, draft_ref, actor)
            if preamble:
                msg = f"{preamble}\n\n用户的请求：{message}"
        yield from guide.run_turn(actor, msg, thread_id, draft_ref, model=model)
    else:
        # qa is otherwise form-blind. When the onboarding wizard is open the
        # user is most likely asking about what they're editing, so prepend a
        # compact digest of the live form — without it the split view is two
        # disconnected panes ("各答各的").
        preamble = _form_context_preamble(form_snapshot, draft_ref, actor)
        if preamble:
            message = f"{preamble}\n\n用户的问题：{message}"
        yield from chat.run_turn(actor, message, thread_id, model=model)


def _page_context_preamble(page_context: Optional[dict]) -> Optional[str]:
    """Grounding block for a page-assistant turn: the current tab, optional
    sub-view, and that page's KB overview. Tells the model to answer from this
    page and to use ``kb_search(tab_key=...)`` for specifics. English (the KB is
    English); the answer still mirrors the user's language via SHARED_LANG_RULE.
    Returns None when there's no tab or no KB page for it."""
    if not page_context:
        return None
    tab = (page_context.get("tab") or "").strip()
    if not tab:
        return None
    sub_view = (page_context.get("sub_view") or "").strip() or None
    try:
        from core.agent import page_kb

        title = page_kb.page_title(tab) or tab
        overview = page_kb.page_overview(tab, sub_view)
    except Exception:
        return None

    where = f'the "{title}" page (tab_key: {tab}'
    where += f", sub-view: {sub_view})" if sub_view else ")"
    lines = [
        f"[Page context] The user is currently on {where}. Prefer answering about "
        "this page. For how-to / where-is / what-a-button-does questions, call "
        f'kb_search with tab_key="{tab}" and answer only from the returned KB. '
        "If the question isn't about this page, answer normally.",
    ]
    if overview:
        lines.append("This page's KB overview:\n" + overview)
    return "\n\n".join(lines)


def _update_mode_lines(target_project_id: int, diff: Optional[dict]) -> str:
    """One line telling the model this draft *updates* a project, plus which
    assets are new vs amended (read off the same diff the badges use)."""
    nodes = (diff or {}).get("nodes") or {}
    name = (diff or {}).get("project_name") or f"#{target_project_id}"
    created, updated, unchanged = [], [], 0
    for path, node in nodes.items():
        if not re.fullmatch(r"products\[\d+\]\.(?:jobs|apps)\[\d+\]", path):
            continue
        change = (node or {}).get("change")
        if change == "new":
            created.append(path)
        elif change == "update":
            updated.append(path)
        else:
            unchanged += 1
    parts = [
        f"注意：这份草稿不是新建项目，而是把文档中的资产并入已存在的项目「{name}」"
        f"（project id {target_project_id}）。提交按钮是「Update the project」。"
        f"新增 {len(created)} 个资产、更新 {len(updated)} 个、保持不变 {unchanged} 个；"
        "不会删除任何东西——在预览里删掉一行只是把它移出本次审阅。"
    ]
    if created:
        parts.append("新增：" + "、".join(created[:15]))
    if updated:
        parts.append("将被改写：" + "、".join(updated[:15]))
    return " ".join(parts)


def _form_context_preamble(form_snapshot: Optional[dict], draft_ref: Optional[int],
                           actor: CurrentUser) -> Optional[str]:
    """Compact digest of the open onboarding form for grounding a qa turn.

    Prefers the client's **live** form state (``form_snapshot.payload`` —
    reflects unsaved edits); falls back to the saved draft when only a
    ``draft_ref`` is given. Returns ``None`` when there is no form to attach, so
    a normal turn (wizard closed) is completely unaffected."""
    import json

    snap = form_snapshot or {}
    payload = snap.get("payload")
    clarifications = snap.get("clarifications") or []
    answers = snap.get("answers") or {}
    dirty = bool(snap.get("dirty"))
    draft_id = draft_ref if draft_ref is not None else snap.get("draftId")
    group_id = snap.get("groupId")
    target_project_id = snap.get("targetProjectId")
    diff = snap.get("diff")
    controlm_text = None

    if payload is None and draft_ref is not None:
        try:
            from core.agent import draft_service
            from core.models.database import get_db

            draft = draft_service.get_draft(get_db(), draft_ref, actor=actor)
            payload = draft.payload
            clarifications = (draft.validation or {}).get("clarifications") or []
            draft_id = draft.id
            group_id = group_id or draft.group_id
            target_project_id = target_project_id or draft.target_project_id
            diff = diff or draft.diff
            controlm_text = draft.controlm_sheet_text
            dirty = False  # came from the server — by definition saved
        except Exception:
            return None
    if payload is None and not clarifications:
        return None

    lines = [
        "[上下文] 右侧 onboarding 表单当前内容如下。用户大概率在问与它相关的问题——若相关，"
        "请结合表单里的具体字段值作答（缺哪个字段、填得对不对）；若无关，照常回答，不要硬扯表单。",
    ]
    head = f"草稿 #{draft_id}" if draft_id else "草稿（尚未保存）"
    if dirty:
        head += "（含未保存的编辑，下面是用户当前界面上的值）"
    lines.append(head)
    # Two onboarding modes — don't let the assistant describe an "add assets to
    # an existing project" draft as a new project registration.
    if target_project_id:
        lines.append(_update_mode_lines(target_project_id, diff))
    if clarifications:
        paths = [c.get("field_path", "?") if isinstance(c, dict) else str(c) for c in clarifications]
        lines.append("待澄清字段：" + "、".join(paths[:20]))
    if answers:
        lines.append("用户正在填写的澄清答案：" + json.dumps(answers, ensure_ascii=False)[:800])
    lines.append("当前打开的表单内容(JSON)：" + json.dumps(payload or {}, ensure_ascii=False)[:3000])

    # The reviewer may have uploaded a Control-M job sheet onto this draft; the
    # merge folds only production scheduled jobs into the form, so attach the raw
    # sheet too (adhoc/rerun rows, run-after dependencies, alert thresholds) so
    # the assistant can answer about everything in it, not just what was merged.
    if controlm_text is None and draft_id is not None:
        controlm_text = _draft_controlm_text(draft_id, actor)
    if controlm_text and controlm_text.strip():
        lines.append("用户已为本草稿上传的 Control-M 作业表（已扁平化为文本，含未并入表单的"
                     "adhoc/rerun、run-after 依赖、告警阈值等）：\n" + controlm_text[:4000])

    # Cross-project split: this draft is one of several siblings created from
    # the same document. Attach the other siblings' forms so a question can be
    # answered against the whole document, not just the open pane.
    if group_id:
        siblings = _group_form_lines(group_id, draft_id, actor)
        if siblings:
            lines.append(
                f"本文档已拆分为同组的多份登记（group {group_id}），"
                "其余表单内容如下（用户的问题可能跨表单，请结合一并作答）：")
            lines.extend(siblings)
    return "\n".join(lines)


def _draft_controlm_text(draft_id: int, actor: CurrentUser) -> Optional[str]:
    """The Control-M sheet text stored on a draft (best-effort; None on any
    load failure or when none was uploaded)."""
    try:
        from core.agent import draft_service
        from core.models.database import get_db

        draft = draft_service.get_draft(get_db(), draft_id, actor=actor)
        return draft.controlm_sheet_text
    except Exception:
        return None


def _group_form_lines(group_id: str, current_draft_id, actor: CurrentUser) -> List[str]:
    """One JSON line per sibling draft in a split group, excluding the open one
    (already shown). Best-effort: any load failure yields no extra context."""
    import json

    try:
        from core.agent import draft_service
        from core.models.database import get_db

        siblings = draft_service.list_group(get_db(), group_id, actor=actor)
    except Exception:
        return []
    out: List[str] = []
    for sib in siblings:
        if current_draft_id is not None and sib.id == current_draft_id:
            continue
        proj = ((sib.payload or {}).get("project") or {})
        name = proj.get("cml_project_name") or proj.get("name") or f"#{sib.id}"
        out.append(f"— 表单 #{sib.id}（CML project：{name}）："
                   + json.dumps(sib.payload or {}, ensure_ascii=False)[:2000])
    return out


_CAPABILITY_ZH = """\
我是 RelayOps 的运维助手，能力如下（数据范围按你的页面权限裁剪）：

**查询（默认只读）**
- 项目/产品资产、Job/App 状态与 runbook、Issue 与 SLA 风险、当前值班
- 统计分析：分布/占比、SLA 达标率、健康度、环比趋势，并能生成可导出图表
- 平台实时直查（CML/MMP）：失败原因、实时状态、模型漂移/审批
- 针对某个 Issue 的处置建议，以及历史相似处置检索
- 诊断：给我一个 issue（如 #71），我能定位失败根因

**接入（Onboarding）**：点输入框上的「Onboarding」打开右侧向导，上传或粘贴交接文档，我陪你逐条补全建档。

**变更（需你确认）**：我默认只读、不改任何东西。要关单 / 标记误报 / 改配置，请先点输入框上的「写入模式」——我只产出一份待确认的草案，你点确认后才由系统提交。

直接问我即可，例如：“现在有哪些快超时的 issue”“产品 X 最近健康吗”“#71 为什么失败”。"""

_CAPABILITY_EN = """\
I'm the RelayOps operations assistant. Here's what I can do (data scope matches your page permissions):

**Look things up (read-only by default)**
- Project/product assets, Job/App status & runbooks, Issues & SLA risk, current on-call
- Analysis: distributions, SLA attainment, health, period-over-period trends, plus exportable charts
- Live platform reads (CML/MMP): failure reasons, real-time status, model drift/approvals
- Resolution guidance for a given Issue, and search over past resolutions
- Diagnosis: give me an issue (e.g. #71) and I'll trace the root cause

**Onboarding**: click "Onboarding" by the input box to open the wizard, upload or paste a handover doc, and I'll walk you through completing it.

**Changes (you confirm)**: I'm read-only and change nothing by default. To resolve/close an issue, mark a false positive, or edit config, switch on "Write mode" by the input box — I'll only draft a proposal that the system commits after you confirm.

Just ask, e.g. "which issues are close to breaching SLA?", "is product X healthy lately?", "why did #71 fail?"."""


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in (text or ""))


def _capability_turn(actor: CurrentUser, thread_id: Optional[str], message: str) -> Iterator[dict]:
    """Answer a "what can you do / who are you" question with a static blurb —
    no tools, no DB. Meta questions shouldn't spend tool calls; the reply mirrors
    the assistant's real scope and points at the Write/Onboarding toggles."""
    thread_id = (thread_id or "").strip() or uuid.uuid4().hex
    yield {"event": "meta", "data": {"thread_id": thread_id, "intent": "capability"}}
    yield {"event": "answer", "data": {"text": _CAPABILITY_ZH if _has_cjk(message) else _CAPABILITY_EN}}
    yield {"event": "done", "data": {}}


def _diagnose_turn(actor: CurrentUser, thread_id: Optional[str], issue_id: int, *, model=None) -> Iterator[dict]:
    """Delegate to the CONCISE diagnosis (grounded, action-first markdown answer
    — see diagnose.run_concise_diagnose), wrapped in the chat event envelope
    (meta first). The verbose structured report (run_diagnose / DiagnosisReport)
    stays available on the dedicated /api/agent/diagnose endpoint. Access is
    checked up front so a permission failure becomes a clean error event."""
    from core.agent.diagnose import check_access, run_concise_diagnose
    from core.exceptions import ForbiddenError, NotFoundError
    from core.models.database import get_db

    thread_id = (thread_id or "").strip() or uuid.uuid4().hex
    yield {"event": "meta", "data": {"thread_id": thread_id, "intent": "diagnose", "issue_id": issue_id}}

    session = get_db().get_session()
    try:
        check_access(session, actor, issue_id)
    except (NotFoundError, ForbiddenError) as exc:
        yield {"event": "error", "data": {"message": str(exc)}}
        yield {"event": "done", "data": {}}
        return
    finally:
        session.close()

    yield from run_concise_diagnose(actor, issue_id, model=model)  # ends with its own 'done'


def _onboarding_turn(actor: CurrentUser, thread_id: Optional[str], draft_ref: Optional[int],
                     form_snapshot: Optional[dict]) -> Iterator[dict]:
    """Open or refresh the onboarding wizard in the right pane. With no draft
    yet → guide the user to upload/paste (form_sync 'opened'). With a draft →
    read the server-side payload, summarise its state, and refresh the pane."""
    from core.agent import draft_service
    from core.exceptions import ForbiddenError, NotFoundError
    from core.models.database import get_db

    thread_id = (thread_id or "").strip() or uuid.uuid4().hex
    yield {"event": "meta", "data": {"thread_id": thread_id, "intent": "onboarding"}}

    if draft_ref is None:
        yield {"event": "form_sync", "data": {"draft_id": None, "reason": "opened"}}
        yield {"event": "answer", "data": {"text":
            "要接入新的 project/product/job：请在右侧上传交接文档或粘贴文本，我会抽取成草稿，"
            "再陪你逐条补全。"}}
        yield {"event": "done", "data": {}}
        return

    try:
        draft = draft_service.get_draft(get_db(), draft_ref, actor=actor)
    except (NotFoundError, ForbiddenError):
        # Stale/inaccessible ref → degrade to guiding a new draft.
        yield {"event": "form_sync", "data": {"draft_id": None, "reason": "opened"}}
        yield {"event": "answer", "data": {"text": "找不到那份草稿（可能已删除或无权访问），我们可以新建一份。"}}
        yield {"event": "done", "data": {}}
        return

    clarifications = ((draft.validation or {}).get("clarifications")) or []
    stale = _snapshot_is_stale(draft, form_snapshot)
    yield {"event": "form_sync", "data": {"draft_id": draft.id, "reason": "refreshed"}}

    parts = [f"草稿 #{draft.id} 当前状态：{draft.status}。"]
    if clarifications:
        parts.append(f"还有 {len(clarifications)} 个待澄清字段，我可以逐条带你确认。")
    else:
        parts.append("没有待澄清的字段。")
    if stale:
        parts.append("注意：右侧表单似乎有未保存的编辑，建议先保存再让我基于它操作。")
    yield {"event": "answer", "data": {"text": " ".join(parts)}}
    yield {"event": "done", "data": {}}


def _snapshot_is_stale(draft, form_snapshot: Optional[dict]) -> bool:
    """True when the client's form snapshot fingerprint disagrees with the
    server-side draft payload — i.e. the client has unsaved edits, so chat must
    not act on a stale form (design v2 §8 / Req 17.2)."""
    if not form_snapshot:
        return False
    client_hash = form_snapshot.get("hash")
    if not client_hash:
        return False
    return client_hash != _payload_hash(draft.payload)


def _payload_hash(payload) -> str:
    import hashlib
    import json

    blob = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def assistant_available() -> tuple[bool, str]:
    return chat.chat_available()
