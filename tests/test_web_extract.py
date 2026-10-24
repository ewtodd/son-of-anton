"""web_extract contracts: what reaches the extraction service, and how long pages come back.

The service is replaced by an httpx MockTransport; URL safety is stubbed so the
tests never resolve names.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

import tools.web_tools as web_tools


def _serve(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict]:
    """Route the tool's service calls to ``handler``; returns the request bodies it saw."""
    seen: list[dict] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "body": json.loads(request.content)})
        return handler(request)

    monkeypatch.setenv("WEB_EXTRACT_URL", "http://extract.test:8890/")
    monkeypatch.setattr(
        web_tools, "_extract_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(record))
    )
    return seen


def _safe_urls(monkeypatch: pytest.MonkeyPatch, safe: bool) -> None:
    async def fake(_url: str) -> bool:
        return safe

    monkeypatch.setattr(web_tools, "async_is_safe_url", fake)


def _extract(urls, **kw) -> list[dict]:
    return json.loads(asyncio.run(web_tools.web_extract_tool(urls, "markdown", **kw)))["results"]


def test_service_pages_reach_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        return httpx.Response(200, json={"url": url, "title": f"T {url}", "content": f"body of {url}", "error": None})

    seen = _serve(monkeypatch, handler)
    _safe_urls(monkeypatch, True)
    results = _extract(["https://example.com/a", {"url": "https://example.com/b"}])

    assert [r["title"] for r in results] == ["T https://example.com/a", "T https://example.com/b"]
    assert [r["content"] for r in results] == ["body of https://example.com/a", "body of https://example.com/b"]
    assert all(r["error"] is None for r in results)
    assert {s["path"] for s in seen} == {"/extract"}
    assert sorted(s["body"]["url"] for s in seen) == ["https://example.com/a", "https://example.com/b"]


def test_private_urls_never_reach_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch, lambda request: httpx.Response(500))
    _safe_urls(monkeypatch, False)
    results = _extract(["http://10.0.0.2:4002/v1/models"])

    assert seen == []
    assert results[0]["error"].startswith("Blocked")
    assert results[0]["content"] == ""


def test_service_errors_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(
        200, json={"url": "", "title": "", "content": "partial", "error": "HTTP 403"}
    ))
    _safe_urls(monkeypatch, True)
    result = _extract(["https://example.com/walled"])[0]

    assert result["error"] == "HTTP 403"
    assert result["content"] == ""


def test_unreachable_service_is_an_error_per_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _serve(monkeypatch, handler)
    _safe_urls(monkeypatch, True)
    result = _extract(["https://example.com/a"])[0]

    assert "unavailable" in result["error"]


def test_short_page_returns_whole() -> None:
    content = "a short page\n" * 10
    assert web_tools._fit_to_budget("https://example.com/s", content, 2000) == content


def test_long_page_window_points_at_the_saved_full_text() -> None:
    content = "\n".join(f"line {i:05d} " + "x" * 40 for i in range(4000))
    out = web_tools._fit_to_budget("https://example.com/long", content, 5000)

    assert len(out) < 6000
    footer = re.search(r"saved at (\S+); the omitted part starts at line (\d+)", out)
    assert footer is not None
    saved, line = Path(footer.group(1).rstrip(";")), int(footer.group(2))
    assert saved.read_text(encoding="utf-8") == content
    head, tail = out.split("\n\n[... ", 1)[0], content[-1000:]
    assert content.startswith(head)
    assert tail in out
    # Reading the saved file from the footer's line onward covers everything the window left out.
    rest = "\n".join(content.splitlines()[line - 1:])
    assert content[len(head):len(content) - 1500] in rest


def test_char_budget_has_a_floor_and_a_default() -> None:
    assert web_tools._char_budget(None) == web_tools.DEFAULT_CHAR_LIMIT
    assert web_tools._char_budget("junk") == web_tools.DEFAULT_CHAR_LIMIT
    assert web_tools._char_budget(10) == web_tools.MIN_CHAR_LIMIT
    assert web_tools._char_budget(40000) == 40000


def test_offered_only_with_a_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_EXTRACT_URL", raising=False)
    assert web_tools.check_web_extract() is False
    monkeypatch.setenv("WEB_EXTRACT_URL", "http://extract.test:8890")
    assert web_tools.check_web_extract() is True
