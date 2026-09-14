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
from agent.context_compactor import (
    COMPACTED_SUMMARY_METADATA_KEY,
    ContextCompactor,
    _SUMMARY_END_MARKER,
)
from agent.conversation_compaction import (
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
        should_compact_info=lambda tokens: (False, "cooldown:5"),
        should_compact=lambda tokens: False,
    )
    assert tc._compaction_decision(blocked, 100) == (False, "cooldown:5")
    plain = SimpleNamespace(should_compact=lambda tokens: tokens > 10)
    assert tc._compaction_decision(plain, 100) == (True, None)
    assert tc._compaction_decision(plain, 1) == (False, None)


_BODY = "## Goal\n- ship the compaction change\n\n## Relevant Files\n- cli.py: renderer"


def _summary_row() -> dict:
    content = ContextCompactor._with_summary_prefix(_BODY) + "\n\n" + _SUMMARY_END_MARKER
    return {"role": "user", "content": content, COMPACTED_SUMMARY_METADATA_KEY: True}


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


# ── deterministic layers before the summarizer ───────────────────────────────

import json
import re

from agent.context_compactor import (
    PRUNE_MIN_RECLAIM_TOKENS,
    PRUNE_TOOL_OUTPUT_MAX_CHARS,
    PRUNE_TRIGGER_TOKENS,
    _PRUNE_MIN_CHARS,
    _SUPERSEDED_PREFIX,
    _summarize_tool_result,
)


def _compactor() -> ContextCompactor:
    return ContextCompactor("test-model", quiet_mode=True)


def test_tool_output_prune_is_on_by_default_and_config_agrees() -> None:
    from son_of_anton_cli.config_defaults import DEFAULT_CONFIG

    cc = _compactor()
    assert cc.proactive_prune_tokens > 0, "masking old tool output must not need opting in"
    assert cc.proactive_prune_min_reclaim_tokens > 0, "…but must stay episodic (prompt cache)"
    assert cc.proactive_prune_min_result_chars >= _PRUNE_MIN_CHARS
    cfg = DEFAULT_CONFIG["compaction"]
    assert cfg["proactive_prune_tokens"] == PRUNE_TRIGGER_TOKENS == cc.proactive_prune_tokens
    assert cfg["proactive_prune_min_reclaim_tokens"] == PRUNE_MIN_RECLAIM_TOKENS
    assert cfg["proactive_prune_min_result_chars"] == PRUNE_TOOL_OUTPUT_MAX_CHARS


def test_summarizer_never_sees_more_than_the_tool_result_cap() -> None:
    cc = _compactor()
    big = "A" * 5_000 + "\nexit_code: 0 FINAL"
    out = cc._serialize_for_summary(
        [
            {"role": "tool", "tool_call_id": "c1", "content": big},
            {"role": "user", "content": "u" * 5_000},
        ]
    )
    tool_part, user_part = out.split("\n\n", 1)
    assert len(tool_part) < PRUNE_TOOL_OUTPUT_MAX_CHARS + 100
    assert "[truncated]" in tool_part and tool_part.endswith("FINAL"), "tail keeps the stop signal"
    assert user_part.count("u") == 5_000, "the user's own words are not capped like tool output"


def _headings(text: str) -> list[str]:
    return re.findall(r"^#{2,3} .+$", text, re.M)


def test_fallback_summary_uses_the_same_sections_as_the_llm_template(monkeypatch) -> None:
    """The deterministic fallback and the LLM prompt must describe one shape."""
    import agent.context_compactor as compactor_module

    captured = {}

    def fake_call_llm(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        body = "## Historical Task Snapshot\nUser asked: 'fix cli.py'\n\n## Objective\n- fix\n"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))])

    # ``call_llm`` is bound into the compactor module at import time.
    monkeypatch.setattr(compactor_module, "call_llm", fake_call_llm)
    cc = _compactor()
    turns = [
        {"role": "user", "content": "fix cli.py"},
        {"role": "assistant", "content": "on it", "tool_calls": [
            {"id": "1", "function": {"name": "read_file", "arguments": json.dumps({"path": "cli.py"})}}]},
        {"role": "tool", "tool_call_id": "1", "content": "x" * 300},
    ]
    cc._generate_summary(turns)
    assert "prompt" in captured, "the summarizer prompt was not built"
    template_sections = [h for h in _headings(captured["prompt"]) if h != "## Pruned Skills"]
    assert template_sections[0] == "## Historical Task Snapshot", "grounding keys on the snapshot heading"
    assert template_sections[1:] == [
        "## Objective", "## Important Details", "## Work State", "### Completed",
        "### Active", "### Blocked", "## Next Move", "## Relevant Files",
    ]
    # A fresh compactor: the fallback of a compactor that already holds a
    # previous summary embeds it under its own heading.
    fallback = ContextCompactor._strip_summary_prefix(_compactor()._build_static_fallback_summary(turns))
    fallback_sections = [h for h in _headings(fallback) if h != "## Last Dropped Turns"]
    assert fallback_sections == template_sections


def test_masked_command_output_keeps_exit_code_and_last_lines() -> None:
    result = json.dumps({"output": "collecting...\n" + "test_x PASSED\n" * 40 + "== 3 failed, 47 passed in 2.1s ==", "exit_code": 1})
    line = _summarize_tool_result("terminal", json.dumps({"command": "pytest tests/"}), result)
    assert line.startswith("[terminal] ran `pytest tests/` -> exit 1")
    assert "3 failed, 47 passed" in line, "the stop signal survives masking"
    assert len(line) < 400


def _call(cid: str, name: str, **args) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_repeating_the_same_call_supersedes_the_older_observation() -> None:
    cc = _compactor()
    msgs = [
        _call("a", "read_file", path="cli.py"), {"role": "tool", "tool_call_id": "a", "content": "old " * 100},
        _call("b", "read_file", path="run_agent.py"), {"role": "tool", "tool_call_id": "b", "content": "other " * 100},
        _call("c", "clarify", question="which?"), {"role": "tool", "tool_call_id": "c", "content": "answer one " * 30},
        _call("d", "read_file", path="cli.py"), {"role": "tool", "tool_call_id": "d", "content": "new " * 100},
        _call("e", "clarify", question="which?"), {"role": "tool", "tool_call_id": "e", "content": "answer two " * 30},
    ]
    pruned, n = cc._prune_old_tool_results(msgs, protect_tail_count=len(msgs))
    by_id = {m["tool_call_id"]: m["content"] for m in pruned if m.get("role") == "tool"}
    assert by_id["a"].startswith(_SUPERSEDED_PREFIX) and "cli.py" in by_id["a"]
    assert by_id["d"] == "new " * 100, "the newest read is the only one that is still true"
    assert by_id["b"] == "other " * 100, "different arguments are not the same call"
    assert by_id["c"] == "answer one " * 30, "user answers are never superseded"
    assert n >= 1
    again, m = cc._prune_old_tool_results(pruned, protect_tail_count=len(pruned))
    assert again == pruned and m == 0, "the pass is idempotent"


def test_only_the_newest_screenshot_survives() -> None:
    cc = _compactor()
    shot = lambda i: [{"type": "text", "text": f"page {i}"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]  # noqa: E731
    msgs = [
        _call("s1", "browser_snapshot"), {"role": "tool", "tool_call_id": "s1", "content": shot(1)},
        _call("s2", "browser_snapshot"), {"role": "tool", "tool_call_id": "s2", "content": shot(2)},
    ]
    pruned, n = cc._prune_old_tool_results(msgs, protect_tail_count=len(msgs))
    old, new = (m["content"] for m in pruned if m.get("role") == "tool")
    assert all(p.get("type") == "text" for p in old), "the stale screenshot is masked"
    assert any(p.get("type") == "image_url" for p in new), "the newest screenshot is kept"
    assert pruned[1]["tool_call_id"] == "s1", "masking replaces content; the pair stays intact"
    assert n == 1
