"""Vision transcription for image uploads (Demo-VLM-BeamSearch).

One job: turn a handover-document screenshot into the same plain-text shape
the rest of the onboarding pipeline expects (tables as ``cell | cell`` rows,
URLs verbatim). Runs inside the background pipeline — never in a request
handler — and only when the draft's source is an image sentinel.
"""

from __future__ import annotations

from core.agent.ingest import MAX_TEXT_CHARS, decode_image_sentinel
from core.agent.llm import get_vision_model
from core.logging import get_logger

logger = get_logger(__name__)

TRANSCRIBE_PROMPT = """\
这是一张项目交接（handover）文档的截图。请把图中全部文字逐字转写为纯文本：
- 表格转写成每行一条、单元格之间用 " | " 分隔；
- URL / 邮箱 / 标识符必须原样保留，一个字符都不能改；
- 保持原文语言，不要翻译、不要总结、不要补充任何图中没有的内容；
- 图里看不清的地方用 [无法辨认] 标注。
只输出转写结果，不要任何前后缀说明。"""


def transcribe_image_text(source_text: str) -> str:
    """Transcribe an image-sentinel source to plain text via the gateway VLM.

    Raises on any failure — the caller owns marking the draft failed (same
    contract as ``run_extraction``).
    """
    decoded = decode_image_sentinel(source_text)
    if decoded is None:
        raise ValueError("source is not an image sentinel")
    mime, payload = decoded
    vlm = get_vision_model(temperature=0)
    logger.info("Onboarding vision transcription: {} base64 chars ({})", len(payload), mime)
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": TRANSCRIBE_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{payload}"}},
        ],
    }
    result = vlm.invoke([message])
    content = getattr(result, "content", None) or ""
    if isinstance(content, list):  # some clients return content parts
        content = "\n".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    text = str(content).strip()
    if not text:
        raise ValueError("视觉模型没有返回任何转写文本")
    return text[:MAX_TEXT_CHARS]
