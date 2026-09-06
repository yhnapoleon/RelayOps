"""Turn an uploaded handover source into plain text for extraction.

Scope:
  * pasted text, .txt, .md / .markdown, .csv / .tsv — decoded as-is (UTF-8,
    GBK fallback); CSV/TSV are plain text, so no parser/extra dependency is
    needed — the delimited rows go to the LLM verbatim
  * .html / .htm — stdlib ``html.parser``; tables become ``cell | cell`` rows
    so column semantics survive, and ``<a href>`` becomes ``text (URL)`` so
    hyperlink targets are never lost (Confluence page fetches reuse this via
    :func:`html_to_text`)
  * .pdf — text layer via ``pypdf`` (agent extra). Scanned PDFs with no text
    layer are rejected with a hint to upload page screenshots instead.
  * images (.png/.jpg/.jpeg/.webp) — stored as a base64 sentinel in the
    draft's source_text; the background pipeline transcribes them through
    the gateway vision model (Demo-VLM-BeamSearch) before extraction. The
    request handler itself never calls the LLM.
"""

from __future__ import annotations

import base64
import re
from html.parser import HTMLParser
from typing import Optional, Tuple

# Hard cap on the text handed to extraction — keeps a runaway document from
# blowing up the LLM context. ~200k chars is far beyond any sane handover doc.
MAX_TEXT_CHARS = 200_000

_TEXT_EXTENSIONS = (".txt", ".md", ".markdown", ".csv", ".tsv")
_HTML_EXTENSIONS = (".html", ".htm")
_IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
# Keep image uploads sane — the VLM context and the drafts table both suffer
# past this point.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Marker prefix for "this draft's source is an image awaiting vision
# transcription". Stored in OnboardingDraft.source_text so the existing
# draft lifecycle (background task + polling) carries the VLM step with no
# schema change; the pipeline replaces it with the transcription.
IMAGE_SENTINEL = "__RELAYOPS_VLM_IMAGE__:"

_SKIP_TAGS = {"script", "style", "head", "noscript"}
_BREAK_TAGS = {"p", "div", "br", "li", "ul", "ol", "table", "section", "article",
               "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}


class UnsupportedFormatError(ValueError):
    """File extension we don't parse — caller should ask for pasted text."""


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list = []
        self._skip_depth = 0
        self._href: str = ""
        self._cells: list = []      # current table row's cells
        self._cell: list = []       # current cell fragments
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell = []
        elif tag == "tr":
            self._cells = []
        elif tag == "li":
            self._emit("\n- ")
        elif tag in _BREAK_TAGS:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a":
            # Render "text (URL)" so the target survives into plain text —
            # this is exactly the "copied the link name, lost the URL" hole.
            if self._href and not self._href.startswith(("#", "javascript:")):
                self._emit(f" ({self._href})")
            self._href = ""
        elif tag in ("td", "th"):
            self._in_cell = False
            self._cells.append("".join(self._cell).strip())
        elif tag == "tr":
            if any(self._cells):
                self._emit("\n" + " | ".join(self._cells))
            self._cells = []
        elif tag in _BREAK_TAGS:
            self._emit("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._emit(data)

    def _emit(self, fragment: str) -> None:
        if self._in_cell:
            self._cell.append(fragment)
        else:
            self._out.append(fragment)

    def text(self) -> str:
        raw = "".join(self._out)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        out: list = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# Confluence Server/DC wraps the page body in <div id="main-content"> and puts
# everything else (global nav, space sidebar, page tree, comments, footer) in
# its own chrome. A whole-page "save as HTML" / "view source" export therefore
# drowns the handover content in hundreds of lines of menu noise. When we can
# see that container, keep only it (cut at the comments/footer that follow).
_CONFLUENCE_START = re.compile(r"<[a-zA-Z]+[^>]*\bid=[\"']main-content[\"']", re.IGNORECASE)
_CONFLUENCE_END = re.compile(
    r"<[a-zA-Z]+[^>]*\bid=[\"'](?:comments-section|footer)[\"']", re.IGNORECASE
)


def _confluence_main_content(html: str) -> str:
    """Slice a full Confluence page export down to its #main-content body.
    Returns the original html unchanged when the container isn't present
    (non-Confluence HTML, or an already-clean export-view body)."""
    start = _CONFLUENCE_START.search(html)
    if not start:
        return html
    body = html[start.start():]
    end = _CONFLUENCE_END.search(body)
    return body[: end.start()] if end else body


def html_to_text(html: str) -> str:
    """Flatten HTML (uploaded file or fetched Confluence page) to plain text
    with table rows as ``cell | cell`` and links as ``text (URL)``. A whole-page
    Confluence export is first sliced to its main-content body so the global
    navigation chrome doesn't bury the handover content for the LLM."""
    parser = _HtmlToText()
    parser.feed(_confluence_main_content(html))
    return parser.text()[:MAX_TEXT_CHARS]


def _extract_pdf_text(data: bytes) -> str:
    """RelayOps  extract pdf text."""
    try:
        from pypdf import PdfReader  # agent extra
    except ImportError as exc:
        raise UnsupportedFormatError(
            "PDF parsing requires pypdf (part of the agent extra); please install it and retry, or paste the text"
        ) from exc
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise UnsupportedFormatError(f"PDF parse failed: {exc}") from exc
    text = "\n\n".join(p.strip() for p in pages if p.strip()).strip()
    if len(text) < 50:
        raise UnsupportedFormatError(
            "This PDF has no text layer (scanned / image-only PDF) — please screenshot the pages as png/jpg and upload, "
            "they'll be transcribed by the vision model"
        )
    return text


def encode_image_sentinel(filename: str, data: bytes) -> str:
    """Pack an uploaded image as the sentinel string the pipeline's vision
    step understands: ``__RELAYOPS_VLM_IMAGE__:<mime>;base64,<payload>``."""
    name = (filename or "").lower()
    mime = next((m for ext, m in _IMAGE_EXTENSIONS.items() if name.endswith(ext)), "")
    if not mime:
        raise UnsupportedFormatError(f"Unsupported image format: {filename!r}")
    if len(data) > MAX_IMAGE_BYTES:
        raise UnsupportedFormatError(
            f"Image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)}MB limit; please compress or screenshot by page"
        )
    return f"{IMAGE_SENTINEL}{mime};base64,{base64.b64encode(data).decode('ascii')}"


def decode_image_sentinel(text: str) -> Optional[Tuple[str, str]]:
    """``(mime, base64_payload)`` when ``text`` is an image sentinel, else None."""
    if not text.startswith(IMAGE_SENTINEL):
        return None
    rest = text[len(IMAGE_SENTINEL):]
    mime, sep, payload = rest.partition(";base64,")
    if not sep or not payload:
        return None
    return mime, payload


def extract_text(filename: str, data: bytes) -> str:
    """Return plain text (or an image sentinel) for extraction, or raise
    UnsupportedFormatError."""
    name = (filename or "").lower()
    if name.endswith(tuple(_IMAGE_EXTENSIONS)):
        return encode_image_sentinel(name, data)   # no char cap — base64
    if name.endswith(".pdf"):
        text = _extract_pdf_text(data)
    elif name.endswith(_HTML_EXTENSIONS):
        return html_to_text(_decode(data))
    elif not name or name.endswith(_TEXT_EXTENSIONS):
        text = _decode(data).strip()
    else:
        raise UnsupportedFormatError(
            f"Unsupported file format: {filename!r} (supported: txt/md/csv/tsv/html/pdf/png/jpg; "
            "for other formats paste the text directly)"
        )
    return text[:MAX_TEXT_CHARS]
