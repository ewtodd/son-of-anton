"""Title-generation contracts — garbage must never become a session title.

A tiny title model stuck in a repetition loop (or a reply truncated by
max_tokens) must fall back to no title, never persist `{"title": "Response
Response ..."}` as the visible sidebar name.
"""

from __future__ import annotations

from agent.title_generator import (
    _extract_title_text,
    _is_degenerate_title,
)


def test_extract_title_from_truncated_json() -> None:
    content = '{"title" : " Only Response Response Response Response Response Respo'
    title = _extract_title_text(content)
    assert "{" not in title
    assert title.startswith("Only")


def test_degenerate_titles_are_rejected() -> None:
    assert _is_degenerate_title("Response Response Response Response") is True
    assert _is_degenerate_title('{"title" : "X"}') is True
    assert _is_degenerate_title("Fix login button on mobile") is False
    assert _is_degenerate_title("Friendly greeting") is False


def test_generate_title_rejects_loop_output(monkeypatch) -> None:
    import agent.title_generator as tg

    class _Msg:
        content = '{"title" : " Response Response Response Response Response Response Respo'

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    monkeypatch.setattr(tg, "_auto_title_enabled", lambda: True)
    monkeypatch.setattr(tg, "call_llm", lambda **kw: _Resp())
    assert tg.generate_title("what's your working dir") is None
