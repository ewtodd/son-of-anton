"""web_search through a self-hosted SearXNG instance, web_extract through a
self-hosted page-extraction service.

``SEARXNG_URL`` (process env or the config-aware .env layer) points at the
SearXNG root or its ``/search`` endpoint. ``WEB_EXTRACT_URL`` points at the
extraction service, which answers ``POST /extract {"url": ...}`` with the
page's title and markdown (headless-browser rendering, PDFs, text types).
"""

import asyncio
import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from son_of_anton_constants import get_son_of_anton_home
from tools.registry import registry, tool_error
from tools.url_safety import async_is_safe_url, normalize_url_for_request, sensitive_query_param_name

logger = logging.getLogger(__name__)

DEFAULT_CHAR_LIMIT = 15000
MIN_CHAR_LIMIT = 2000
EXTRACT_TIMEOUT_S = 120


def _env_value(name: str) -> str:
    """Return ``name`` from the config-aware env, falling back to process env."""
    try:
        from son_of_anton_cli.config import get_env_value

        val = get_env_value(name)
    except Exception:
        val = None
    if val is None:
        val = os.getenv(name, "")
    return (val or "").strip()


def _searxng_url() -> str:
    return _env_value("SEARXNG_URL")


def _web_extract_service_url() -> str:
    return _env_value("WEB_EXTRACT_URL").rstrip("/")


def _searxng_search_url(base_url: str) -> str:
    """Join the instance URL with ``/search``; an explicit endpoint is kept as-is."""
    root = (base_url or "").strip().rstrip("/")
    if root.endswith("/search"):
        return root
    return f"{root}/search"


def _searxng_search(query: str, limit: int) -> Dict[str, Any]:
    base_url = _searxng_url().rstrip("/")
    if not base_url:
        return {"success": False, "error": "SEARXNG_URL is not set"}
    try:
        resp = httpx.get(
            _searxng_search_url(base_url),
            params={"q": query, "format": "json", "pageno": 1},
            timeout=15,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("SearXNG HTTP error: %s", exc)
        return {"success": False, "error": f"SearXNG returned HTTP {exc.response.status_code}"}
    except httpx.RequestError as exc:
        logger.warning("SearXNG request error: %s", exc)
        return {"success": False, "error": f"Could not reach SearXNG at {base_url}: {exc}"}
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("SearXNG response parse error: %s", exc)
        return {"success": False, "error": "Could not parse SearXNG response as JSON"}

    raw_results = data.get("results", [])
    # SearXNG may return a score field; sort descending and cap to limit.
    ranked = sorted(raw_results, key=lambda r: float(r.get("score", 0)), reverse=True)[:limit]
    web_results = [
        {
            "title": str(r.get("title", "")),
            "url": str(r.get("url", "")),
            "description": str(r.get("content", "")),
            "position": i + 1,
        }
        for i, r in enumerate(ranked)
    ]
    logger.info(
        "SearXNG search '%s': %d results (from %d raw, limit %d)",
        query, len(web_results), len(raw_results), limit,
    )
    return {"success": True, "data": {"web": web_results}}


def web_search_tool(query: str, limit: int = 5) -> str:
    """Search the web; returns JSON ``{"success", "data": {"web": [...]}}``."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = min(max(limit, 1), 100)
    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)
        logger.info("Web search via searxng: '%s' (limit: %d)", query, limit)
        return json.dumps(_searxng_search(query, limit), indent=2, ensure_ascii=False)
    except Exception as e:
        logger.debug("Error searching web: %s", e)
        return tool_error(f"Error searching web: {str(e)}")


def _web_extract_url(value: Any) -> Optional[str]:
    """Return a usable URL from a model-supplied extract item.

    Models sometimes forward a complete web-search result instead of its URL.
    Accept the two common URL keys, but reject missing/non-string values.
    """
    if isinstance(value, dict):
        value = value.get("url") or value.get("href")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _char_budget(char_limit: Any) -> int:
    try:
        return max(int(char_limit), MIN_CHAR_LIMIT)
    except (TypeError, ValueError):
        return DEFAULT_CHAR_LIMIT


def _fit_to_budget(url: str, content: str, char_limit: int) -> str:
    """Return ``content`` whole when it fits ``char_limit``.

    A longer page is saved whole under the home's cache, and a head+tail
    window of about ``char_limit`` characters is returned; its footer names
    the saved file and the line the omitted middle starts on.
    """
    if len(content) <= char_limit:
        return content
    path = get_son_of_anton_home() / "cache" / "web_extract" / f"{hashlib.sha256(url.encode()).hexdigest()[:16]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    head = content[: int(char_limit * 0.7)]
    tail = content[len(content) - int(char_limit * 0.3):]
    omitted = len(content) - len(head) - len(tail)
    middle_line = head.count("\n") + 1
    return (
        f"{head}\n\n[... {omitted} characters omitted ...]\n\n{tail}\n\n"
        f"[Truncated: the page has {len(content)} characters. The full text is saved at {path}; "
        f"the omitted part starts at line {middle_line} (read_file with offset={middle_line}).]"
    )


def _extract_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=EXTRACT_TIMEOUT_S)


async def _extract_via_service(client: httpx.AsyncClient, service_url: str, url: str) -> Dict[str, Any]:
    """POST one URL to the extraction service; returns its title, content and error."""
    try:
        resp = await client.post(f"{service_url}/extract", json={"url": url})
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("web_extract service error for %s: %s", url, exc)
        return {"title": "", "content": "", "error": f"Extraction service unavailable ({exc.__class__.__name__})"}
    except ValueError:
        return {"title": "", "content": "", "error": "Extraction service returned invalid JSON"}
    return {
        "title": str(data.get("title") or ""),
        "content": str(data.get("content") or ""),
        "error": data.get("error") or None,
    }


async def web_extract_tool(
    urls: List[Any],
    format: str = None,
    char_limit: Optional[int] = None,
) -> str:
    """Extract pages through the WEB_EXTRACT_URL service.

    URLs carrying embedded secrets are refused outright and private/internal
    addresses are blocked per URL before anything is sent to the service;
    the rest are extracted concurrently and fitted to the character budget.
    """
    from agent.redact import _PREFIX_RE
    from urllib.parse import unquote

    results: List[Dict[str, Any]] = []
    for index, item in enumerate(urls):
        _url = _web_extract_url(item)
        if _url is None:
            results.append({
                "url": "",
                "title": "",
                "content": "",
                "error": (
                    f"Invalid URL item at index {index}: expected a URL string "
                    "or an object with a string 'url' or 'href' field"
                ),
            })
            continue
        normalized_url = normalize_url_for_request(_url)
        if any(
            _PREFIX_RE.search(u)
            for u in (_url, unquote(_url), normalized_url, unquote(normalized_url))
        ):
            return json.dumps({
                "success": False,
                "error": "Blocked: URL contains what appears to be an API key or token. "
                         "Secrets must not be sent in URLs.",
            })
        sensitive_query_key = sensitive_query_param_name(normalized_url)
        if sensitive_query_key:
            return json.dumps({
                "success": False,
                "error": (
                    "Blocked: URL contains a credential-like query parameter "
                    f"({sensitive_query_key}). Web extract backends are third-party "
                    "readers; remove the sensitive query parameter or use a local "
                    "browser session when this access is explicitly required."
                ),
            })
        results.append({"url": normalized_url, "title": "", "content": "", "error": None})

    try:
        for result in results:
            if result["error"] is None and not await async_is_safe_url(result["url"]):
                result["error"] = "Blocked: URL targets a private or internal network address"
        safe = [r for r in results if r["error"] is None]
        if safe:
            service_url = _web_extract_service_url()
            if not service_url:
                return tool_error("web_extract is not configured: WEB_EXTRACT_URL is unset")
            budget = _char_budget(char_limit)
            async with _extract_client() as client:
                extracted = await asyncio.gather(
                    *(_extract_via_service(client, service_url, r["url"]) for r in safe)
                )
            for result, page in zip(safe, extracted):
                result["title"] = page["title"]
                result["error"] = page["error"]
                if not page["error"]:
                    result["content"] = _fit_to_budget(result["url"], page["content"], budget)
        if not results:
            return tool_error("Content was inaccessible or not found")
        return json.dumps({"results": results}, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.debug("Error extracting content: %s", e)
        return tool_error(f"Error extracting content: {str(e)}")


def check_web_api_key() -> bool:
    """Registry ``check_fn``: web_search is offered when SEARXNG_URL is set."""
    return bool(_searxng_url())


def check_web_extract() -> bool:
    """Registry ``check_fn``: web_extract is offered when WEB_EXTRACT_URL is set."""
    return bool(_web_extract_service_url())


WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "Search the web for information. Returns up to 5 results by default with titles, URLs, and descriptions. The query is passed through to the configured backend, so operators such as site:domain, filetype:pdf, intitle:word, -term, and \"exact phrase\" may work when the backend supports them.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web. You may include backend-supported operators such as site:example.com, filetype:pdf, intitle:word, -term, or \"exact phrase\"."
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5
            }
        },
        "required": ["query"]
    }
}

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "Extract content from web page URLs. Returns clean page content in markdown/text (no LLM summarization — fast). Also works with PDF URLs (arxiv papers, documents) — pass the PDF link directly. Pages within the char budget (default 15000) return whole; larger pages return a head+tail window with a footer telling you the full text's saved file path and the read_file call to page through the omitted middle. Pages are rendered in a browser first, so script-built pages work too. Inline images appear as [IMAGE: alt] placeholders; link targets are left out. Sites that block automated readers return an error.",
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5
            },
            "char_limit": {
                "type": "integer",
                "description": "Optional per-page character budget sent back (default 15000). Pages larger than this are head+tail truncated with the full text stored to disk. Raise it when you need more of a long page inline.",
                "minimum": 2000
            }
        },
        "required": ["urls"]
    }
}

registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **kw: web_search_tool(args.get("query", ""), limit=args.get("limit", 5)),
    check_fn=check_web_api_key,
    requires_env=["SEARXNG_URL"],
    emoji="🔍",
    max_result_size_chars=100_000,
)
registry.register(
    name="web_extract",
    toolset="web",
    schema=WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [],
        "markdown",
        char_limit=args.get("char_limit"),
    ),
    check_fn=check_web_extract,
    requires_env=["WEB_EXTRACT_URL"],
    is_async=True,
    emoji="📄",
    max_result_size_chars=100_000,
)
