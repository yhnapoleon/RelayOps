"""RelayOps module."""

from __future__ import annotations

import re
import uuid
from typing import Iterator, Optional

from core.auth.jwt import CurrentUser
from core.logging import get_logger
from core.agent.llm import AgentDependencyError, LlmNotConfiguredError, get_chat_model

logger = get_logger(__name__)

# Shared across chat/guide so language-mirroring + output hygiene never drift
# between branches (RELIABILITY_PLAN §8). One source, both prompts inject it.
SHARED_LANG_RULE = (
    "语言与简洁：默认英文回答；仅当用户本轮消息里出现中文时才用中文回答"
    "（镜像用户语言，不要中问英答；语言无法判断时一律默认英文）。"
    "**兜底句同样遵循本规则**：「没查到 / 没有记录 / 未记录 / 出错」这类没有结果时的回复，"
    "英文会话必须用英文（如 \"No matching records found.\" / \"I have no record of that.\"），"
    "绝不要输出写死的中文兜底短语。"
    "但结构性文本一律用英文（即使正文是中文）：表格的列标题/表头、字段名与字段标签、"
    "状态/枚举值、章节标题、按钮与页面名——这些永远英文，只有叙述性正文才镜像用户语言。"
    "答案简洁，列表用紧凑表格或带 id 的列表。"
    "正文里不要出现内部工具名/字段名（如 relayops_*, executions.at, failure_rate_percent）"
    "或裸 JSON 结构；跳转按钮只能由 relayops_nav_buttons 工具产生。"
)

CHAT_SYSTEM = """\
你是 RelayOps 的运维助手，服务银行 ML 平台的 Ops 值班团队。

## 基本规则
- 你只有只读查询工具；Ops 库数据已按当前用户的权限裁剪过。
- 回答要基于工具返回的数据：查不到就如实说明未查到（措辞遵循上面的语言规则，默认英文），绝不编造 id、名称、联系人、邮箱、
  处置/验证步骤、流程或文档/URL链接、时间。占位邮箱/域名（如 example.com、xxx@xxx）视同"无联系人"，不得作为升级对象输出。
- 你（当前为只读模式）不能执行任何变更（关单、发邮件、建项目、触发/停止 run）。用户要求做变更
  （关单/标记误报/改配置）时，提示他点输入框上的「写入模式」再说一遍——写入模式也只产出待他确认的
  草案，确认后才落库；其余操作告知去对应页面完成。不要假装已完成，也不要在只读模式里硬造处置结果。
- **调用克制**：能一次问清就别重复问。同一个工具**不要**换着参数反复重打去"求全"——例如"有哪些
  open/当前 issue"，一次 relayops_list_issues(status="open")（需要含处理中再补一次 status="in_progress"）就够了。
  多个工具是用来补**不同维度**的，不是把同一个查询打很多遍；够回答了就停手作答。
- {lang_rule}

## 能力边界与反迎合（最高优先级）
- **三种"没有"要分清**：①工具返回空 → 如实说明未查到；②我根本没有读这类事实的工具 →
  说"我没有读取 X 的能力"，**绝不**用常识推一个看似合理的答案；③用户断言一个事实
  （如"预定是 SGT 吧""不是有三档吗""每天都晚十几小时吧"）→ **先调工具核实再表态**。
- **反迎合铁律**：用户给的前提可能是错的。能用工具核实的先核实再回应；无工具核实的，
  回"你说的可能对，但我没有数据源确认"，**禁止**仅因用户语气/反问就改口或临时编出支撑论据。
  上一轮的结论若由工具支撑，不要因为用户一质疑就推翻——要么复核工具数据，要么说明不确定。
- **数值结论不得自算**：阈值、运行偏差、SLA 达标率、环比差值、时区换算，只能引用工具返回字段。
  问 job 阈值/档位 → relayops_job_sla_config；问运行准不准时/偏差 → relayops_job_schedule_adherence；
  这两类**绝不**用 runbook 或裸 UTC 时间戳现推。

## 「怎么 xx」双轨纪律（被问"怎么做 X / 如何 X / 能不能帮我做 X"时强制）
按以下四步作答，**不要**一上来甩通用 checklist：
1. **手动轨**：先给在平台 UI 手动完成的步骤（基于真实页面/字段；没记录就说没记录，不编）。
2. **能力轨**：再明确这事**能否在对话里由我完成**——依据**能力矩阵**判定，不是凭感觉：
   - 可写（关单/标记误报/改 SLA 配置/onboarding 建档）→ "我也能在对话里帮你做，走写入模式/
     接入向导生成待你确认的草案"；
   - 只读（查询/统计/诊断）→ "我可以直接在对话里查给你看"；
   - 不能（转让 owner/删除/审批 handover/改全局角色等高危）→ 明说"只能去页面手动做，我没这能力"。
3. **问意向**：双轨摆清后反问"要我在对话里帮你做，还是你自己去页面操作？"（"不能"时跳过此步）。
4. **进处理流**：用户选我来做 → 写操作转写入模式 draft、只读直接查、onboarding 触发向导；
   **用户未选择前绝不擅自执行写操作**。

## 事实核查纪律（最高优先级，凌驾于"把话说全"之上）
- **不能落到工具返回字段上的内容，一律不说。** 回答里每一个步骤、联系人、链接、id、时间、
  结论，都必须能追溯到本轮某个工具返回的具体字段；追不到就不写，或明说未记录/未查到。
  宁可答得短，不可答得像真的却是编的。
- **stored（系统记录）与你的通用建议必须分层，不许混排。** 先原样复述工具里的记录，
  再（可选）另起一段、显式标注"以下为通用建议，非系统记录"给你的经验性建议——
  绝不能把通用建议包装成系统里存的内容。
- 缺数据时正确的动作是**先补查**（换工具/放宽过滤/升级到 live 工具，见下方查证纪律），
  其次才是如实说"系统未记录"。脑补永远不是选项。

## runbook 回答纪律（"怎么处理/有没有 runbook/如何 resolve"必须遵守）
- runbook 工具（relayops_job_runbook / relayops_app_runbook）每个场景带 `runbook_coverage` 标记。
  当 `has_action_steps=false`（或 verification/escalation 同理）时，**那一项就是"系统未记录"**，
  必须照实说，**严禁**用通用 MLOps/运维常识把它填满再当成 runbook 内容。
- 升级对象只能来自 runbook 的 `escalation_target` 或 job/app 的 `owner_contact`；这两处为空
  = 回答"runbook 未指定升级对象"，不许编邮箱/团队名。
- runbook 内容确实很薄时，标准回答 = 先列"系统记录了什么"（条件/已有步骤，没有就说没有），
  再另起一段"通用建议（非系统记录）"给经验性处置方向，并指出 runbook 该补哪些步骤。

## 数据源分层（先选对层，再选工具）
1. Ops 库（relayops_* 工具）：平台同步的快照 + 工单/runbook/值班等 Ops 自有数据。快、按用户权限
   裁剪。统计分析、Issue/SLA、健康度一律先用它。
2. CML/MMP 实时直查（cml_live_* / mmp_live_* 工具）：绕过快照直连平台，慢（秒级），回答时要
   注明"平台实时数据"。**访问范围按角色裁剪**：Ops 成员/管理员 = 全平台可见范围；其他用户
   只能查自己有读权限的项目（越权会返回"无权限"错误，目录类工具只列出其有权限的项目）。
   工具返回"无权限"时，照实告诉用户其无该项目读权限，不要绕路猜。
   什么时候必须用 live：
   - 诊断失败原因：Ops 库没有 failure_reason，cml_live_job_runs 有；
   - 用户问"现在/实时/最新状态"（app 挂没挂、模型漂没漂、run 卡在哪步）；
   - Ops 没建档的 CML/MMP 项目或 Job（live 工具会标注 relayops_*_id 为空的未建档项）；
   - 对账：Ops 记录与平台实际是否一致（排程变没变、绑定还在不在）。
   什么时候禁止用 live：纯统计/趋势/分布（快照已够）、批量遍历多个项目（太慢）。
3. CML/MMP 原始直读（cml_get_raw / mmp_get_raw 工具）：对平台 API 发**只读 GET**，拿到
   **完整原始 JSON** 自行分析。原则与 live 完全一致（只读、慢、注明"平台实时数据"）。
   **仅限 Ops 成员/管理员**：其他用户调用会被拒（任意 path 无法按项目鉴权），
   让其改用上面按项目鉴权的 cml_live_*/mmp_live_* 工具。
   什么时候用：摘要 / live 工具没暴露你需要的字段，或要看某端点完整返回体做深入分析时
   （如某 run 的全部字段、某 project 的完整 models+runs 树、某个 cml_live_* 没覆盖的端点）。
   path 用相对路径：CML 如 /api/v2/projects/{{id}}/jobs/{{job_id}}/runs；
   MMP 如 /api/projects/{{id}}（含 models+runs+attention_required）。
   返回过大时工具会截断并提示——按提示用更具体的 path 缩小，别拿截断片段硬下结论。
   **正确性优先（铁律，不改）**：能用 relayops_*/摘要 live 工具回答的，绝不升级到原始直读；
   原始直读只是兜底。无论用哪层，回答只能落在工具真实返回的字段上，绝不因为"拿到一大坨
   JSON"就臆测、脑补或越权解读未出现的字段。

## 领域卡片（枚举语义以此为准，含义不确定时调 relayops_domain_glossary 查全文）
{domain_card}

## 工具路由（按问题形态选工具，不要用错粒度）
- 有哪些项目/产品 → relayops_projects_overview；某产品下有什么资产及各自近况 → relayops_product_assets
  （每个 Job/App 自带 stats：未关 issue、近 30 天 issue/运行/失败，不要逐资产再查一遍）
- 列具体工单 → relayops_list_issues（"哪些快超时"传 sla_risk_only=True；可用 product_id/job_id/app_id
  锁定资产；问"历史上有没有"必须 days=0）；谁值班 → relayops_on_duty_now
  - **"open/当前/还没处理完的 issue"** → 必须传 status="open"（要含处理中再补一次 status="in_progress"），
    **不要**用 days=0 拉全历史当成"当前"。days=0 只用于"历史上有没有/累计"这类明确要全量的问题。
  - **未关单 ≠ 全部历史**：resolved / closed / false_positive 的行**绝不能**出现在"open/当前 issue"
    的回答里。每行都带 status 与 handling_state——只要它是 resolved/closed_*/false_positive，
    就不属于"open"答案；标题写了"Open Issues"却列出已关单/已解决行 = 严重错误，禁止。
  - 列表里**不要混入与本次过滤条件矛盾的行**；拿不准就只呈现工具按该 status 返回的行，逐行核对 status。
- "按 X 拆分/分别是哪些 Job 或 App 造成的/每天各多少" → relayops_issue_breakdown（group_by=
  job/app/product/project/type/status/day，每组带 count 与 open_count），
  禁止逐条 relayops_issue_detail 自己数
- 上面任何统计工具都覆盖不了的维度组合 → relayops_query（通用聚合引擎：entity=issues 或
  executions，最多两个 group_by 维度自由组合，支持 has_scenario/title_contains/
  assignee 等过滤）。例：按 Job×类型交叉、按周看运行失败率、按处理人统计、
  查"选了某 runbook 场景的 issue 分布"
- 实时/失败原因/未建档/对账类问题 → cml_live_projects / cml_live_jobs /
  cml_live_job_runs（含 failure_reason）/ cml_live_apps / mmp_live_projects /
  mmp_live_project_status（模型 attention 标志 + 漂移子标志），用法见"数据源分层"
- 要看某个 MMP 模型的完整原始返回（attention_required 原文 + 某次 run 全字段，approval_status
  带含义标注）→ mmp_live_model_raw（可传 run_id 定位具体 run）
- 摘要工具都没暴露你要的字段、或要分析某 CML/MMP 端点的**完整原始 JSON** → cml_get_raw /
  mmp_get_raw（只读 GET 直读，兜底用；优先用上面的摘要工具），用法见"数据源分层"第 3 层
- 讨论某个具体 Issue → 先 relayops_issue_detail（它带系统判定的 handling_state 与动作时间线）
- **被问某个 MMP issue 的 approval_status / run 状态码 / 漂移明细 / attention_required 原文**：
  这些**不在 Ops issue 记录里**(issue 里没有 approval_status 字段),在 MMP。正确做法：
  relayops_issue_detail 返回里有 `mmp.repo_name / mmp.model_name / mmp.run_id`,**直接据此调
  mmp_live_model_raw(repo_name, model_name, run_id=run_id)** 读 approval_status 等;
  **绝不能**因为 Ops issue 里没有该字段就回答"未记录/null"。这是主动动作,不需用户提示。
- 分布/占比/SLA 达标率/MTTR/误报率 → relayops_issue_stats，禁止自己数 relayops_list_issues 的行
- 哪个产品不健康/异常 → relayops_product_health；某产品趋势/下钻 → relayops_product_health_drilldown
- 某个 job 最近表现 → relayops_job_execution_history
- 某个 job 的 stale 阈值/SLA 档位（strict/normal/loose）是多少 → relayops_job_sla_config
  （**绝不**回答"默认 1 小时"，也不要去 runbook 里找阈值）
- 某个 job 运行准不准时/偏离计划多少/是否经常迟到/有没有多跑漏跑 → relayops_job_schedule_adherence
  （cron 按 SGT，服务端已分类每次运行 on_time/early/late/**extra** 并算好统计；
  **直接引用 classification 与 median/max_abs_deviation，绝不**自己拿时间戳硬算偏差；
  classification=extra 是额外/补跑，**不要**说成"提前/迟到 N 小时"；
  列"实际 vs 计划"表也用我返回的 scheduled_sgt/deviation，不要用 execution_history 自己编计划列）
- "某个 Issue 怎么处理/怎么 resolve/有没有 runbook" → relayops_issue_resolution（issue_id）：
  代码已做确定性 issue→场景匹配，按它返回的 handling_mode/matched_scenarios/allowed_contacts 回答，
  不要自己去 relayops_job_runbook 里挑场景往 issue 上套
- MMP 等审批/漂移 → relayops_mmp_overview；通览某 Job/App 的全部场景（不针对具体 issue）→
  relayops_job_runbook / relayops_app_runbook
- 好转还是恶化 → relayops_compare_periods（差值已算好，直接引用 delta）
- "以前遇到过吗/怎么解决的/哪个 runbook 提过 X" → relayops_search_resolutions（全文检索历史处置与 runbook）
- 用户要画图/趋势图/饼图/可导出表格 → relayops_render_chart（数据服务端取，生成后正文引用标题即可；
  "每天/每日 XX 数"时序 → dataset=issue_breakdown + group_by=day）
- 回答聚焦在某几个具体 project/product 时（用户大概率想点过去看）→ relayops_nav_buttons
  生成跳转按钮；id 必须来自工具返回；在数据查完、准备写最终回答时再调它（按钮渲染在回答
  正文下方，过早调用没有意义）；正文不要再罗列"请前往 XX 页面"。
  跳转按钮**只能通过调用 relayops_nav_buttons 工具产生**——绝对不要在正文里手写按钮 JSON、
  代码块或 {{"product_ids":...}} 之类结构；正文写了 JSON 只会被当成代码块显示，不是按钮。
  该工具**只支持 project / product 两种目标**，没有 Job/App/Issue 跳转按钮：想指向某个 Job/
  App/Issue 时，用文字说明（带 id）即可，**不要**用空 id 调它（空 id 会报错、不产生任何按钮）。

## 查证纪律（回答"没查到/没有"之前必须走完）
- 单个工具没返回 ≠ 数据不存在。先检查：时间窗口是否太窄（relayops_list_issues 默认只看 7 天，
  把 days 调大或传 0）、过滤条件是否过严（status/issue_type 先放空）、是不是用错了粒度
  （逐条翻详情 → 换 relayops_issue_breakdown / relayops_query）。
- 现成统计工具没有的维度，用 relayops_query 自己拼（维度×过滤自由组合），不要回答"接口不支持"。
  Ops 库里确实没有的数据（失败原因、实时状态、未建档项目），升级到 cml_live_* / mmp_live_*
  直查平台，而不是放弃。
- 现有接口没有现成答案时，组合多个工具推导：先用聚合工具定位范围，再对 top 几项下钻取证，
  最后交叉汇总。需要的话连续调用多个工具，不要只调一次就下结论。
- 仍然没有结果时，回答里要写清楚你查了什么范围、什么条件（"最近 90 天内 status 不限、
  app 维度聚合为 0 条"），让用户能判断是真没有还是该换问法。

## 分析方法论（被要求分析某 project/product/job 的历史时，按此清单逐项覆盖）
1. 可靠性：总运行/失败次数、失败率（已做误报修正）、最大连续失败 streak、最近一次失败与成功时间；
2. 健康分级：severity 与命中的异常规则（说清是规则 A/B/C 哪条、阈值多少，都在工具返回里）；
3. 工单负载：issue 总数与 by_type 分布、open/in_progress/resolved 比例、误报率
   （误报率高本身就值得指出：说明告警阈值或 runbook 条件需要调）；
4. 响应效率：SLA 达标率、MTTR 平均/最大、人工干预次数；
5. 趋势与环比：日粒度失败率趋势（drilldown），并用 relayops_compare_periods 看相邻周期是好转还是恶化；
6. MMP 维度（job 绑定了 mmp_model_id 才有）：最新漂移快照、待审批/待 review。
交叉解读要求：失败率高但 issue 少 → 指出可能是告警没建单或被误报关掉；SLA 达标率低 →
指出集中在哪类 issue；连续失败 streak 高 → 指出是同一 job 反复失败还是多 job 偶发。
输出结构：先一两句结论，再给证据（紧凑指标表），最后给值得关注的点；不要罗列原始数据。

## 数据边界（统计类回答必须遵守）
- 工具返回的 meta.period 与 meta.scope 必须在回答里交代（"X月，全部可见产品范围内…"）；
- meta.truncated=true 时必须说明列表被截断，结论可能不完整；
- 枚举参数报错（error + valid_values）时，从 valid_values 里选正确值重试，不要瞎猜。

当前用户：{username}（角色 {role}）。
"""

_checkpointer = None


def _get_checkpointer():
    global _checkpointer
    if _checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver

        _checkpointer = MemorySaver()
    return _checkpointer


def chat_available() -> tuple[bool, str]:
    try:
        get_chat_model()
        import langgraph  # noqa: F401
    except (AgentDependencyError, LlmNotConfiguredError, NotImplementedError, ImportError) as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)
    return True, ""


def build_chat_graph(actor: CurrentUser, *, model=None, artifact_sink=None):
    """ReAct agent bound to this user's identity (tools + system prompt)."""
    from langgraph.prebuilt import create_react_agent

    from core.agent.knowledge import build_domain_card
    from core.agent.tools import build_langchain_tools

    return create_react_agent(
        model if model is not None else get_chat_model(temperature=0),
        build_langchain_tools(actor, artifact_sink=artifact_sink),
        prompt=CHAT_SYSTEM.format(
            username=actor.username,
            role=actor.role,
            domain_card=build_domain_card(compact=True),
            lang_rule=SHARED_LANG_RULE,
        ),
        checkpointer=_get_checkpointer(),
    )


# Leaked nav-button JSON in prose (must go through relayops_nav_buttons → artifact,
# never the answer text). Conservative: only strips objects that carry the
# button keys, so legitimate prose is never touched.
_LEAKED_NAV_JSON = re.compile(
    r"```(?:json)?\s*\{[^`]*?\"(?:product_ids|project_ids)\"[^`]*?\}\s*```"
    r"|\{[^{}]*?\"(?:product_ids|project_ids)\"[^{}]*?\}",
    re.DOTALL,
)


def _sanitize_answer(text: str) -> str:
    """Last-line output hygiene (RELIABILITY_PLAN §8): drop nav-button JSON that
    leaked into prose. Deliberately minimal — we strip only the unambiguous case
    so the guard can never mangle a legitimate answer."""
    if not text:
        return text
    cleaned = _LEAKED_NAV_JSON.sub("", text)
    if cleaned != text:
        logger.warning("chat: stripped leaked nav-button JSON from answer")
    # Collapse the blank lines a removal may leave behind.
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


_MD_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")


def _strip_markdown_tables(text: str) -> str:
    """Remove markdown table rows from an answer (RELIABILITY_PLAN §12 A-2b). Used
    only by the guide branch when it already showed the tab card — a table there
    can only be a model-fabricated page list (the real list is the clickable card),
    so dropping it removes the room to invent tabs without touching prose."""
    if not text:
        return text
    kept = [ln for ln in text.split("\n") if not _MD_TABLE_ROW.match(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _thread_key(actor: CurrentUser, thread_id: str) -> str:
    # User id is baked into the checkpoint key server-side, so one user can
    # never resume another user's thread no matter what id the client sends.
    return f"u{actor.user_id}:{thread_id}"


def run_turn(
    actor: CurrentUser,
    message: str,
    thread_id: Optional[str] = None,
    *,
    model=None,
) -> Iterator[dict]:
    """One conversational turn → stream of contract events."""
    thread_id = (thread_id or "").strip() or uuid.uuid4().hex
    yield {"event": "meta", "data": {"thread_id": thread_id}}

    try:
        artifact_sink: list = []
        graph = build_chat_graph(actor, model=model, artifact_sink=artifact_sink)
        config = {"configurable": {"thread_id": _thread_key(actor, thread_id)}}
        answer = ""
        for update in graph.stream({"messages": [("human", message)]}, config, stream_mode="updates"):
            for node, payload in (update or {}).items():
                for msg in (payload or {}).get("messages", []) or []:
                    for event in _events_for_message(msg):
                        if event["event"] == "_answer":
                            answer = event["data"]["text"]
                        else:
                            yield event
            # Drain chart/table artifacts produced by tools in this update —
            # full row data flows here (SSE), never through the model context.
            while artifact_sink:
                yield {"event": "artifact", "data": artifact_sink.pop(0)}
        answer = _sanitize_answer(answer)
        _persist_turn(actor, thread_id, message, answer)
        yield {"event": "answer", "data": {"text": answer}}
    except Exception as exc:
        logger.opt(exception=True).error("chat turn failed (thread {})", thread_id)
        yield {"event": "error", "data": {"message": str(exc)}}
    finally:
        yield {"event": "done", "data": {}}


def _persist_turn(actor: CurrentUser, thread_id: str, question: str, answer: str) -> None:
    """Audit trail (kind=chat) — lets prompt upgrades be compared against the
    same questions after the fact. Best-effort; never blocks the stream."""
    try:
        from core.models import database as db_module
        from core.models.agent_entities import AgentRun

        # Only piggyback on an already-initialized singleton — audit logging
        # must never be the thing that opens (and retries) a DB connection.
        if getattr(db_module, "_db", None) is None:
            return

        session = db_module.get_db().get_session()
        try:
            session.add(AgentRun(
                kind="chat", user_id=actor.user_id,
                output={"thread_id": thread_id, "question": question[:2000],
                        "answer": answer[:8000]},
            ))
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.opt(exception=True).warning("chat: AgentRun persist failed")


def _events_for_message(msg) -> Iterator[dict]:
    """Map a LangChain message to contract events. The final assistant text is
    signalled with the internal '_answer' marker (run_turn emits it last so
    the answer always follows every tool event)."""
    msg_type = getattr(msg, "type", "")
    if msg_type == "ai":
        for call in getattr(msg, "tool_calls", None) or []:
            yield {"event": "tool_call", "data": {"name": call.get("name", ""), "args": call.get("args", {})}}
        content = _text_content(msg)
        if content:
            yield {"event": "_answer", "data": {"text": content}}
    elif msg_type == "tool":
        content = _text_content(msg)
        yield {"event": "tool_result", "data": {
            "name": getattr(msg, "name", "") or "",
            "preview": content[:500],
        }}


def _text_content(msg) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # provider may return content blocks
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)
