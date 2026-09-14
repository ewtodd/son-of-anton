"""Compaction fires on real provider usage and shows what it kept.

Contracts (modelled on opencode's session compaction):

  * the turn-start gate compares the provider's last reported prompt size —
    never a character-count estimate — while a real reading exists;
  * right after a compaction (the ``-1`` sentinel) nothing fires until the
    provider reports the new shape;
  * with no reading at all (resumed session, usage-less provider) the
    estimate is the fallback, the same rule the tool-loop gate uses;
  * the summary the model carries forward can be recovered from the
    compacted transcript without its transport scaffolding, and is handed to
    the host's ``compaction_summary_callback`` exactly when it changed.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent import turn_context as tc
from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    ContextCompressor,
    _SUMMARY_END_MARKER,
)
from agent.conversation_compression import (
    _display_compaction_summary,
    extract_compaction_summary_text,
)

_BIG = [{"role": "user", "content": "x" * 400_000}]


def test_turn_start_gate_prefers_real_usage_over_the_estimate(monkeypatch) -> None:
    estimated = []
    monkeypatch.setattr(
        tc, "estimate_request_tokens_rough", lambda *a, **k: estimated.append(1) or 999_999
    )
    cc = SimpleNamespace(last_prompt_tokens=12_345)
    assert tc.resolve_turn_start_compaction_tokens(cc, _BIG) == 12_345
    assert estimated == [], "a real reading must never be second-guessed by an estimate"


def test_turn_start_gate_waits_for_real_usage_after_a_compaction(monkeypatch) -> None:
    estimated = []
    monkeypatch.setattr(
        tc, "estimate_request_tokens_rough", lambda *a, **k: estimated.append(1) or 999_999
    )
    cc = SimpleNamespace(last_prompt_tokens=-1)
    assert tc.resolve_turn_start_compaction_tokens(cc, _BIG) is None
    assert estimated == []


def test_turn_start_gate_falls_back_to_the_estimate_without_any_reading(monkeypatch) -> None:
    seen = {}

    def fake(messages, system_prompt="", tools=None):
        seen["args"] = (len(messages), system_prompt, tools)
        return 4_321

    monkeypatch.setattr(tc, "estimate_request_tokens_rough", fake)
    cc = SimpleNamespace(last_prompt_tokens=0)
    assert (
        tc.resolve_turn_start_compaction_tokens(
            cc, _BIG, system_prompt="sys", tools=[{"name": "t"}]
        )
        == 4_321
    )
    assert seen["args"] == (1, "sys", [{"name": "t"}])


def test_compaction_decision_reports_why_it_is_blocked() -> None:
    blocked = SimpleNamespace(
        should_compress_info=lambda tokens: (False, "cooldown:5"),
        should_compress=lambda tokens: False,
    )
    assert tc._compaction_decision(blocked, 100) == (False, "cooldown:5")
    plain = SimpleNamespace(should_compress=lambda tokens: tokens > 10)
    assert tc._compaction_decision(plain, 100) == (True, None)
    assert tc._compaction_decision(plain, 1) == (False, None)


_BODY = "## Goal\n- ship the compaction change\n\n## Relevant Files\n- cli.py: renderer"


def _summary_row() -> dict:
    content = ContextCompressor._with_summary_prefix(_BODY) + "\n\n" + _SUMMARY_END_MARKER
    return {"role": "user", "content": content, COMPRESSED_SUMMARY_METADATA_KEY: True}


def test_summary_text_is_recovered_without_its_scaffolding() -> None:
    compacted = [
        {"role": "system", "content": "sys"},
        _summary_row(),
        {"role": "user", "content": "next question"},
    ]
    assert extract_compaction_summary_text(compacted) == _BODY
    assert extract_compaction_summary_text([{"role": "user", "content": "hi"}]) is None
    assert extract_compaction_summary_text(None) is None


def test_summary_is_shown_once_and_only_when_it_changed() -> None:
    shown = []
    agent = SimpleNamespace(compaction_summary_callback=lambda s, st: shown.append((s, st)))
    before = [{"role": "user", "content": "hello"}]
    after = [_summary_row(), {"role": "user", "content": "hello"}]

    _display_compaction_summary(agent, before, after, before_messages=1, after_messages=2)
    assert len(shown) == 1
    assert shown[0][0] == _BODY
    assert shown[0][1] == {"before_messages": 1, "after_messages": 2}

    shown.clear()
    _display_compaction_summary(agent, after, after)  # same handoff survived a rollback
    assert shown == []

    _display_compaction_summary(SimpleNamespace(), before, after)  # headless host: no seam bound


def test_compaction_stats_line_reads_naturally() -> None:
    from cli import _format_compaction_stats

    assert _format_compaction_stats(None) == ""
    assert _format_compaction_stats({"before_messages": 40, "after_messages": 6}) == "6 of 40 messages kept"
    line = _format_compaction_stats(
        {"before_messages": 40, "after_messages": 6, "before_tokens": 150_000, "after_tokens": 30_000}
    )
    assert line == "6 of 40 messages kept · ~150,000 → ~30,000 tokens"
    assert _format_compaction_stats({"after_tokens": 30_000}) == "~30,000 tokens now"
