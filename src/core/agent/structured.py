"""RelayOps module."""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Sequence, Tuple, Type

from pydantic import BaseModel, ValidationError

from core.config import get_config
from core.logging import get_logger

logger = get_logger(__name__)


class StructuredOutputError(Exception):
    """The model could not be coerced into the requested schema."""


def invoke_structured(
    model: Any,
    schema: Type[BaseModel],
    messages: Any,
    *,
    max_repair: int = 1,
) -> BaseModel:
    """Invoke ``model`` and return a validated ``schema`` instance.

    ``messages`` is whatever ``ChatModel.invoke`` accepts — the call sites pass
    a list of ``("system", ...)`` / ``("human", ...)`` tuples. ``max_repair`` is
    how many times a bad JSON reply is fed back for correction (json_prompt path
    only; 0 disables).

    Raises:
        StructuredOutputError: the model never produced schema-valid output.
        Exception: genuine transport/timeout errors propagate unchanged (so the
            existing try/except degradation at each call site still applies).
    """
    mode = _resolve_mode(model)
    if mode == "json_prompt":
        return _invoke_json_prompt(model, schema, messages, max_repair=max_repair)

    # native (with an auto-mode safety net into json_prompt)
    try:
        return _invoke_native(model, schema, messages)
    except (StructuredOutputError, ValidationError):
        raise
    except Exception as exc:  # noqa: BLE001 — inspect, then decide
        if mode == "auto" and _looks_like_unsupported_structured(exc):
            logger.warning(
                "native structured output rejected ({}); retrying via JSON-prompt", exc
            )
            return _invoke_json_prompt(model, schema, messages, max_repair=max_repair)
        raise


# ── mode resolution ───────────────────────────────────────────────────


def _resolve_mode(model: Any) -> str:
    """``native`` | ``json_prompt``. Config forces a mode; ``auto`` detects
    DeepSeek from the model and otherwise stays native."""
    configured = "auto"
    try:
        value = get_config().llm_structured_output_mode
        if isinstance(value, str) and value in ("auto", "native", "json_prompt"):
            configured = value
    except Exception:  # noqa: BLE001 — a stub config must not break extraction
        pass
    if configured != "auto":
        return configured
    return "json_prompt" if _is_deepseek(model) else "native"


def _is_deepseek(model: Any) -> bool:
    """Best-effort: DeepSeek clients carry a ``deepseek-*`` model name and/or an
    ``api.deepseek.com`` base URL. Test fakes have neither, so they stay native."""
    parts: List[str] = []
    for attr in ("model_name", "model"):
        v = getattr(model, attr, None)
        if isinstance(v, str):
            parts.append(v)
    for attr in ("openai_api_base", "base_url"):
        v = getattr(model, attr, None)
        if v:
            parts.append(str(v))
    for holder in ("client", "root_client", "async_client"):
        base = getattr(getattr(model, holder, None), "base_url", None)
        if base:
            parts.append(str(base))
    return "deepseek" in " ".join(parts).lower()


def _looks_like_unsupported_structured(exc: Exception) -> bool:
    """Heuristic: does this error read like the gateway rejecting structured
    output (vs a timeout / auth / network failure, which must propagate)?"""
    blob = f"{type(exc).__name__} {exc}".lower()
    needles = (
        "response_format",
        "json_schema",
        "structured output",
        "does not support",
        "not supported",
        "unsupported",
        "invalid_request",
        "tool call",
        "function call",
    )
    return any(n in blob for n in needles)


# ── native path ───────────────────────────────────────────────────────


def _invoke_native(model: Any, schema: Type[BaseModel], messages: Any) -> BaseModel:
    result = model.with_structured_output(schema).invoke(messages)
    if isinstance(result, schema):
        return result
    # with_structured_output may hand back a dict depending on version.
    return schema.model_validate(result)


# ── json_prompt path ──────────────────────────────────────────────────


def _invoke_json_prompt(
    model: Any, schema: Type[BaseModel], messages: Any, *, max_repair: int
) -> BaseModel:
    directive = _json_directive(schema)
    base = _augment_with_directive(messages, directive)
    convo = base
    last_err: Optional[Exception] = None
    raw = ""
    for attempt in range(max_repair + 1):
        resp = model.invoke(convo)
        raw = _content_to_text(resp)
        try:
            data = _loads_lenient(raw)
            return schema.model_validate(data)
        except (json.JSONDecodeError, ValueError, ValidationError) as exc:
            last_err = exc
            if attempt >= max_repair:
                break
            logger.info(
                "structured JSON parse failed for {} (attempt {}); asking model to repair",
                schema.__name__, attempt + 1,
            )
            convo = base + [
                ("assistant", raw[:6000]),
                ("human", _repair_instruction(exc)),
            ]
    logger.warning(
        "structured output for {} unusable after {} attempt(s); raw head: {!r}",
        schema.__name__, max_repair + 1, raw[:500],
    )
    raise StructuredOutputError(
        f"模型未能返回符合 {schema.__name__} 的 JSON（已尝试 {max_repair + 1} 次）。"
        f"最后一次解析错误：{last_err}"
    )


def _json_directive(schema: Type[BaseModel]) -> str:
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    return (
        "Return ONLY a single JSON object that strictly conforms to the JSON "
        "Schema below. Output raw JSON with no surrounding text, no explanation, "
        "and no Markdown code fences (do not wrap it in ``` ). "
        "只输出纯 JSON，不要任何解释、注释或 ``` 代码块。\n"
        f"JSON Schema:\n{schema_json}"
    )


def _repair_instruction(err: Exception) -> str:
    return (
        "你上一条输出无法解析为符合该 Schema 的 JSON，错误如下：\n"
        f"{str(err)[:1500]}\n\n"
        "请只输出修正后的纯 JSON 对象——不要 Markdown、不要 ``` 代码块、不要任何"
        "解释文字，且必须严格符合前面给出的 JSON Schema。"
    )


def _augment_with_directive(messages: Any, directive: str) -> List[Tuple[str, str]]:
    """Fold ``directive`` into the first system message (or prepend one). Returns
    a fresh list of ``(role, content)`` tuples."""
    pairs = _as_role_content(messages)
    out: List[Tuple[str, str]] = []
    injected = False
    for role, content in pairs:
        if role == "system" and not injected:
            content = f"{content}\n\n{directive}"
            injected = True
        out.append((role, content))
    if not injected:
        out.insert(0, ("system", directive))
    return out


def _as_role_content(messages: Any) -> List[Tuple[str, str]]:
    """Normalize the call sites' input (a list of 2-tuples, or a bare string)
    into ``[(role, content), ...]``."""
    if isinstance(messages, str):
        return [("human", messages)]
    pairs: List[Tuple[str, str]] = []
    for m in messages:
        if isinstance(m, (tuple, list)) and len(m) == 2:
            pairs.append((str(m[0]), m[1]))
        else:  # opaque message object — carry a stringified copy
            pairs.append(("human", str(getattr(m, "content", m))))
    return pairs


def _content_to_text(resp: Any) -> str:
    """Extract assistant text from an ``AIMessage`` (or a raw string/list)."""
    content = getattr(resp, "content", resp)
    if isinstance(content, list):
        parts: List[str] = []
        for p in content:
            if isinstance(p, dict):
                parts.append(p.get("text", ""))
            else:
                parts.append(str(p))
        content = "".join(parts)
    return str(content)


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_]*\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _loads_lenient(text: str) -> Any:
    """Parse JSON that may be wrapped in a Markdown code fence or padded with
    stray prose. Raises ``json.JSONDecodeError`` when nothing parses."""
    t = text.strip()
    m = _FENCE_RE.match(t)
    if m:
        t = m.group(1).strip()
    else:
        # tolerate an unbalanced leading fence / trailing fence
        t = re.sub(r"^```[a-zA-Z0-9_]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        # last resort: slice from the first opening bracket to its last closer
        sliced = _outermost_json(t)
        if sliced is not None:
            return json.loads(sliced)
        raise


def _outermost_json(t: str) -> Optional[str]:
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not starts:
        return None
    start = min(starts)
    open_ch = t[start]
    close_ch = "}" if open_ch == "{" else "]"
    end = t.rfind(close_ch)
    if end <= start:
        return None
    return t[start : end + 1]
