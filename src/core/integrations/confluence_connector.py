"""RelayOps module."""

from __future__ import annotations

import re
from typing import Any, Optional, Tuple
from urllib.parse import unquote_plus, urlparse

import httpx

from core.config import get_config
from core.logging import get_logger

logger = get_logger(__name__)


class ConfluenceError(Exception):
    """Page fetch failed — message is safe to surface to the reviewer."""


_PAGE_ID_QUERY_RE = re.compile(r"[?&]pageId=(\d+)")
_PAGES_PATH_RE = re.compile(r"/pages/(\d+)(?:/|$)")
_DISPLAY_RE = re.compile(r"/display/([^/]+)/([^/?#]+)")


def parse_page_locator(url: str) -> Tuple[str, Optional[str], Optional[Tuple[str, str]]]:
    """Split a Confluence URL into (base_url, page_id, (space, title)).

    Exactly one of ``page_id`` / ``(space, title)`` is non-None.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfluenceError(f"不是合法的 Confluence 页面 URL：{url!r}")

    m = _PAGE_ID_QUERY_RE.search(url)
    if m:
        base = f"{parsed.scheme}://{parsed.netloc}"
        return base, m.group(1), None
    m = _PAGES_PATH_RE.search(parsed.path)
    if m:
        # Context root may precede /spaces|/pages — keep everything before.
        prefix = parsed.path[: parsed.path.find("/spaces/")] if "/spaces/" in parsed.path else \
            parsed.path[: parsed.path.find("/pages/")]
        base = f"{parsed.scheme}://{parsed.netloc}{prefix}".rstrip("/")
        return base, m.group(1), None
    m = _DISPLAY_RE.search(parsed.path)
    if m:
        prefix = parsed.path[: parsed.path.find("/display/")]
        base = f"{parsed.scheme}://{parsed.netloc}{prefix}".rstrip("/")
        return base, None, (m.group(1), unquote_plus(m.group(2)))
    raise ConfluenceError(
        "无法从 URL 识别 Confluence 页面（支持 pageId=…、/pages/<id>/、/display/SPACE/Title 形式）"
    )


def _client(verify: Any, timeout: float) -> httpx.Client:
    return httpx.Client(verify=verify, timeout=timeout, follow_redirects=True)


def _get_json(client: httpx.Client, url: str, token: str, params: Optional[dict] = None) -> Any:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        resp = client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise ConfluenceError(f"Confluence 连接失败：{exc}") from exc
    if resp.status_code in (401, 403):
        raise ConfluenceError(
            f"Confluence 返回 {resp.status_code}：token 无效或没有该页面的访问权限"
        )
    if resp.status_code == 404:
        raise ConfluenceError("Confluence 返回 404：页面不存在或不可见")
    if resp.status_code >= 400:
        raise ConfluenceError(f"Confluence 返回 {resp.status_code}：{resp.text[:200]}")
    try:
        return resp.json()
    except Exception as exc:  # HTML login page etc.
        raise ConfluenceError(
            "Confluence 返回了非 JSON 内容（多半是被重定向到登录页 — 请检查 token）"
        ) from exc


def fetch_page_html(url: str, *, user_token: str = "") -> Tuple[str, str]:
    """Fetch one Confluence page → ``(title, rendered_html)``.

    ``user_token`` (per-request, never stored) wins over the configured
    service token. Raises :class:`ConfluenceError` with a reviewer-facing
    message on any failure.
    """
    cfg = get_config()
    token = (user_token or "").strip() or cfg.confluence_bearer_token
    if not token:
        raise ConfluenceError(
            "没有可用的 Confluence token：请在表单里粘贴你的个人访问令牌（PAT），"
            "或让管理员配置 confluence.bearer_token"
        )

    derived_base, page_id, space_title = parse_page_locator(url)
    base = cfg.confluence_base_url or derived_base
    verify: Any = cfg.confluence_ca_bundle_path or cfg.confluence_verify_ssl
    timeout = float(cfg.confluence_timeout_seconds)

    with _client(verify, timeout) as client:
        if page_id is None:
            space, title = space_title  # type: ignore[misc]
            data = _get_json(client, f"{base}/rest/api/content", token, params={
                "spaceKey": space, "title": title, "limit": 1,
            })
            results = data.get("results") or []
            if not results:
                raise ConfluenceError(
                    f"在空间 {space} 里找不到标题为「{title}」的页面（或无权访问）"
                )
            page_id = str(results[0].get("id"))

        data = _get_json(
            client, f"{base}/rest/api/content/{page_id}", token,
            params={"expand": "body.export_view,body.view,body.storage"},
        )

    title = str(data.get("title") or "")
    body = data.get("body") or {}
    # export_view is fully rendered (macros expanded, tables real HTML);
    # view and storage are progressively worse fallbacks.
    for key in ("export_view", "view", "storage"):
        html = ((body.get(key) or {}).get("value") or "").strip()
        if html:
            return title, html
    raise ConfluenceError("页面取到了，但正文为空（body.export_view/view/storage 都没有内容）")
