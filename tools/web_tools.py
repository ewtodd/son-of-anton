"""web_search and web_extract, backed by a self-hosted SearXNG instance.

``SEARXNG_URL`` (process env or the config-aware .env layer) points at the
instance root or its ``/search`` endpoint. SearXNG aggregates search results
but does not fetch pages, so ``web_extract`` validates and safety-checks its
URLs and then reports that it cannot extract content.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from tools.registry import registry, tool_error
from tools.url_safety import async_is_safe_url, normalize_url_for_request, sensitive_query_param_name

logger = logging.getLogger(__name__)


def _searxng_url() -> str:
    """Return SEARXNG_URL from the config-aware env, falling back to process env."""
    try:
        from son_of_anton_cli.config import get_env_value

        val = get_env_value("SEARXNG_URL")
    except Exception:
        val = None
    if val is None:
        val = os.getenv("SEARXNG_URL", "")
    return (val or "").strip()


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


async def web_extract_tool(
    urls: List[Any],
    format: str = None,
    char_limit: Optional[int] = None,
) -> str:
    """Validate and safety-check URLs; SearXNG itself cannot extract pages.

    URLs carrying embedded secrets are refused outright, private/internal
    addresses are blocked per URL, and any URL that passes both gets the
    search-only error.
    """
    from agent.redact import _PREFIX_RE
    from urllib.parse import unquote

    results: List[Dict[str, Any]] = []
    any_safe = False
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
            if result["error"] is not None:
                continue
            if await async_is_safe_url(result["url"]):
                any_safe = True
            else:
                result["error"] = "Blocked: URL targets a private or internal network address"
        if any_safe:
            return json.dumps(
                {
                    "success": False,
                    "error": "SearXNG is a search-only backend and cannot extract URL content.",
                },
                ensure_ascii=False,
            )
        if not results:
            return tool_error("Content was inaccessible or not found")
        return json.dumps({"results": results}, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.debug("Error extracting content: %s", e)
        return tool_error(f"Error extracting content: {str(e)}")


def check_web_api_key() -> bool:
    """Registry ``check_fn``: the web tools are offered when SEARXNG_URL is set."""
    return bool(_searxng_url())


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
    "description": "Extract content from web page URLs. Returns clean page content in markdown/text (no LLM summarization — fast). Also works with PDF URLs (arxiv papers, documents) — pass the PDF link directly. Pages within the char budget (default 15000) return whole; larger pages return a head+tail window with a footer telling you the full text's saved file path and the read_file call to page through the omitted middle. Inline images appear as [IMAGE: alt] placeholders; real image URLs are kept as links. If a URL fails or times out, use the browser tool instead.",
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
    check_fn=check_web_api_key,
    requires_env=["SEARXNG_URL"],
    is_async=True,
    emoji="📄",
    max_result_size_chars=100_000,
)
