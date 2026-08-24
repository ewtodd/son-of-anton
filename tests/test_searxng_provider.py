"""SearXNG provider URL contract — the ``/search`` join must never double up.

The configured ``SEARXNG_URL`` may be a bare instance root or the full JSON
endpoint path; both forms must produce exactly one ``/search`` segment.
"""

from __future__ import annotations

from plugins.web.searxng.provider import _searxng_search_url


def test_search_url_appends_missing_endpoint() -> None:
    assert _searxng_search_url("http://host:8888") == "http://host:8888/search"
    assert _searxng_search_url("http://host:8888/") == "http://host:8888/search"


def test_search_url_keeps_explicit_endpoint() -> None:
    assert _searxng_search_url("http://host:8888/search") == "http://host:8888/search"


def test_search_url_tolerates_surrounding_whitespace() -> None:
    assert _searxng_search_url("  http://host:8888/search  ") == "http://host:8888/search"
