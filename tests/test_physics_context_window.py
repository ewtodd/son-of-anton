"""The Autophysicist keeps its prompts inside the model's context window.

Before this, nothing handled an overflow. ``ContextTooLongError`` was raised
and caught only by the runner's generic handler, which logged it, appended a
note and moved on; ``PERMANENT_MEMORY.md`` is append-only and was injected in
full every iteration — so once memory plus prompt outgrew the window, every
remaining iteration failed the same way and the run burned them all.

The contracts here: the injected memory view is bounded and stable, the full
file is never rewritten, an overflow inside an iteration trims and retries, and
a prompt that cannot fit at all stops the run instead of failing it N times.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from autophysicist.core.tool_call import ToolCall
from autophysicist.llm import (
    ContextTooLongError,
    PromptTooLargeError,
    run_agent_loop,
)
from autophysicist.memory import PermanentMemory, estimate_tokens


def _fill(memory: PermanentMemory, entries: int, size: int) -> None:
    for i in range(1, entries + 1):
        memory.append(f"RESULT-{i}: " + ("x" * size), i)


# --- the bounded memory view -------------------------------------------------


def test_memory_that_fits_is_injected_verbatim(tmp_path) -> None:
    memory = PermanentMemory(tmp_path)
    _fill(memory, 3, 100)
    memory.configure_view(100_000)
    assert memory.read_for_prompt() == memory.read_full()


def test_an_unconfigured_view_is_the_whole_file(tmp_path) -> None:
    memory = PermanentMemory(tmp_path)
    _fill(memory, 40, 3_000)
    assert memory.read_for_prompt() == memory.read_full()


def test_memory_over_its_share_is_bounded_and_keeps_the_newest_entry(tmp_path) -> None:
    memory = PermanentMemory(tmp_path)
    _fill(memory, 40, 3_000)
    full_before = memory.read_full()
    budget = 8_000
    memory.configure_view(budget, condense=lambda text, target: "digest of old results")

    view = memory.read_for_prompt()

    assert estimate_tokens(view) <= budget * 1.05
    assert "RESULT-40: " + "x" * 3_000 in view, "the newest entry stays in full"
    assert "RESULT-1: " + "x" * 3_000 not in view
    assert "digest of old results" in view
    assert PermanentMemory.FILENAME in view, "the Manager is told where the full text is"
    assert memory.read_full() == full_before, "the authoritative file is untouched"
    assert (tmp_path / PermanentMemory.DIGEST_FILENAME).exists()


def test_the_view_is_stable_and_the_digest_is_not_rebuilt_every_iteration(
    tmp_path,
) -> None:
    calls: list[int] = []

    def condense(text, target):
        calls.append(len(text))
        return f"digest #{len(calls)}"

    memory = PermanentMemory(tmp_path)
    _fill(memory, 40, 3_000)
    memory.configure_view(8_000, condense=condense)

    first = memory.read_for_prompt()
    assert memory.read_for_prompt() == first, "same files, same bytes"
    assert len(calls) == 1

    memory.append("a small new result", 41)
    memory.read_for_prompt()
    assert len(calls) == 1, "one small entry does not re-fold the digest"


def test_a_failing_condenser_falls_back_to_a_bounded_excerpt(tmp_path) -> None:
    def condense(text, target):
        raise RuntimeError("endpoint down")

    memory = PermanentMemory(tmp_path)
    _fill(memory, 40, 3_000)
    budget = 8_000
    memory.configure_view(budget, condense=condense)
    view = memory.read_for_prompt()
    assert estimate_tokens(view) <= budget * 1.05
    assert "RESULT-1:" in view, "the excerpt digest names every folded entry"


def test_an_oversized_digest_is_not_trusted(tmp_path) -> None:
    memory = PermanentMemory(tmp_path)
    _fill(memory, 40, 3_000)
    budget = 8_000
    memory.configure_view(budget, condense=lambda text, target: "y" * 10**6)
    assert estimate_tokens(memory.read_for_prompt()) <= budget * 1.05


def test_an_edited_memory_file_rebuilds_the_digest(tmp_path) -> None:
    calls: list[str] = []
    memory = PermanentMemory(tmp_path)
    _fill(memory, 40, 3_000)
    memory.configure_view(8_000, condense=lambda text, target: calls.append(text) or "d")
    memory.read_for_prompt()

    path = tmp_path / PermanentMemory.FILENAME
    path.write_text(
        path.read_text(encoding="utf-8").replace("RESULT-2:", "CORRECTED-2:"),
        encoding="utf-8",
    )
    memory.read_for_prompt()
    assert len(calls) == 2
    assert "CORRECTED-2:" in calls[-1]


# --- overflow inside an iteration --------------------------------------------


class _Config:
    model = "m"
    api_timeout = 5.0
    api_retry_max = 0
    api_retry_initial_delay = 0.0
    workspace_dir = ""
    max_tokens_retries = 0
    context_length = 0

    def max_tokens_for_agent(self, _name):
        return 32_768


class _BigOutputs:
    stop_after_round = False

    def execute(self, name, args):
        return ToolCall(tool_name=name, tool_input=args, output="z" * 40_000, is_error=False, duration=0.0)


def _response(content="", tool_calls=None, finish="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def _read(n):
    call = SimpleNamespace(id=f"c{n}", function=SimpleNamespace(name="read", arguments="{}"))
    return _response(tool_calls=[call], finish="tool_calls")


def _size(messages) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


def test_an_overflowing_round_trims_old_tool_output_and_retries(monkeypatch) -> None:
    """A fake endpoint with a 100k-character window: three 40k tool outputs
    overflow it, and the loop must trim instead of failing the iteration."""
    window_chars = 100_000
    replies = iter([_read(1), _read(2), _read(3), _response("done")])
    sent: list[list[dict]] = []

    def _create(client, model, messages, max_tokens, config, tools=None, **_):
        if _size(messages) > window_chars:
            raise ContextTooLongError()
        sent.append([dict(m) for m in messages])
        return next(replies)

    monkeypatch.setattr("autophysicist.llm._resolve_endpoint", lambda c: (None, "m"))
    monkeypatch.setattr("autophysicist.llm._create_with_retry", _create)

    result = run_agent_loop(
        system="SYSTEM",
        user_content="OPENING",
        config=_Config(),
        tool_executor=_BigOutputs(),
        tools=[{"type": "function", "function": {"name": "read"}}],
        max_rounds=5,
    )
    assert result.text == "done"
    last = sent[-1]
    assert last[0]["content"] == "SYSTEM" and last[1]["content"] == "OPENING"
    assert any("context window" in str(m.get("content")) for m in last if m["role"] == "tool")


def test_a_reply_that_does_not_fit_is_capped_before_history_is_cut(monkeypatch) -> None:
    """vLLM rejects when prompt + max_tokens exceeds the window even though
    the prompt fits; capping the reply keeps the prefix cache intact."""
    seen_max: list[int] = []

    def _create(client, model, messages, max_tokens, config, tools=None, **_):
        seen_max.append(max_tokens)
        if max_tokens > 100_000 - 90_000:
            raise ContextTooLongError(90_000, limit=100_000)
        return _response("ok")

    monkeypatch.setattr("autophysicist.llm._resolve_endpoint", lambda c: (None, "m"))
    monkeypatch.setattr("autophysicist.llm._create_with_retry", _create)
    config = _Config()
    run_agent_loop(
        system="s", user_content="u", config=config,
        tool_executor=_BigOutputs(), tools=[], max_rounds=2,
    )
    assert seen_max[0] == 32_768
    assert seen_max[-1] <= 10_000
    assert config.context_length == 100_000, "the window the error named is adopted"


def test_an_opening_prompt_that_cannot_fit_raises_prompt_too_large(monkeypatch) -> None:
    def _create(*a, **k):
        raise ContextTooLongError(500_000, limit=262_144)

    monkeypatch.setattr("autophysicist.llm._resolve_endpoint", lambda c: (None, "m"))
    monkeypatch.setattr("autophysicist.llm._create_with_retry", _create)
    with pytest.raises(PromptTooLargeError):
        run_agent_loop(
            system="s", user_content="u", config=_Config(),
            tool_executor=_BigOutputs(), tools=[], max_rounds=3,
        )


def test_the_preflight_refuses_a_prompt_far_over_the_window(monkeypatch, tmp_path) -> None:
    from autophysicist.core.config import Config
    from autophysicist.core.metrics import MetricsTracker
    from autophysicist.memory import Scratchpad
    from autophysicist.runner import _run_iteration
    from autophysicist.utils.sandbox import SandboxPolicy

    def _never(*a, **k):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr("autophysicist.llm._create_with_retry", _never)
    monkeypatch.setattr("autophysicist.llm._resolve_endpoint", lambda c: (None, "m"))
    config = Config()
    config.context_length = 8_000
    with pytest.raises(PromptTooLargeError):
        _run_iteration(
            system_prompt="s",
            problem_text="p" * 200_000,
            answer_template="",
            config=config,
            permanent_memory=PermanentMemory(tmp_path),
            scratchpad=Scratchpad(tmp_path),
            workspace_root=tmp_path,
            iteration=1,
            max_iterations=1,
            token_budget=64_000,
            tool_call_cap=15,
            max_rounds=3,
            sandbox_timeout=1,
            metrics=MetricsTracker(),
            policy=SandboxPolicy(interpreter="python3"),
        )


# --- the run stops instead of burning its iterations -------------------------


def _run_with(monkeypatch, tmp_path: Path, failure) -> list[int]:
    from autophysicist import runner

    attempted: list[int] = []

    def fake_iteration(**kwargs):
        attempted.append(kwargs["iteration"])
        raise failure

    monkeypatch.setattr(runner, "_run_iteration", fake_iteration)
    monkeypatch.setattr(runner, "resolve_context_window", lambda config: 100_000)
    monkeypatch.setattr(runner, "build_lookups", lambda: None)
    runner.run_autophysicist(
        problem_text="p",
        max_iterations=6,
        workspace_root=tmp_path / "ws",
    )
    return attempted


def test_a_prompt_that_cannot_fit_stops_the_run(monkeypatch, tmp_path) -> None:
    attempted = _run_with(monkeypatch, tmp_path, PromptTooLargeError(300_000, limit=100_000))
    assert attempted == [1]
    scratch = (tmp_path / "ws" / "SCRATCHPAD.md").read_text(encoding="utf-8")
    assert "Run stopped" in scratch


def test_repeated_overflows_stop_the_run(monkeypatch, tmp_path) -> None:
    attempted = _run_with(monkeypatch, tmp_path, ContextTooLongError())
    assert attempted == [1, 2]


def test_other_failures_still_move_on(monkeypatch, tmp_path) -> None:
    attempted = _run_with(monkeypatch, tmp_path, RuntimeError("flaky"))
    assert attempted == [1, 2, 3, 4, 5, 6]


# --- an unknown window is unknown, and budgets assume a small one -------------


def _endpoint(base_url: str, model: str):
    return lambda config: (SimpleNamespace(base_url=base_url, api_key="none"), model)


def test_an_undetectable_window_resolves_to_unknown_not_256k(monkeypatch) -> None:
    from autophysicist import llm
    from autophysicist.core.config import Config

    llm._CONTEXT_WINDOWS.clear()
    # A local endpoint that answers nothing, and a model no table knows.
    monkeypatch.setattr(
        llm, "_resolve_endpoint", _endpoint("http://127.0.0.1:1/v1", "zz-unlisted-model-q")
    )
    config = Config()
    config.context_length = 0
    assert llm.resolve_context_window(config) == 0


def test_an_unknown_window_still_bounds_memory_but_never_stops_the_run(
    monkeypatch, tmp_path
) -> None:
    from autophysicist import llm, runner
    from autophysicist.core.config import Config
    from autophysicist.core.metrics import MetricsTracker
    from autophysicist.memory import Scratchpad
    from autophysicist.utils.sandbox import SandboxPolicy

    memory = PermanentMemory(tmp_path)
    _fill(memory, 60, 3_000)  # far more than a small window's memory share
    config = Config()
    config.context_length = 0
    monkeypatch.setattr(runner, "resolve_context_window", lambda c: 0)
    monkeypatch.setattr(runner, "call_llm", lambda **k: (_ for _ in ()).throw(RuntimeError()))
    runner._configure_memory_view(memory, config, 0)
    view = memory.read_for_prompt()
    assert view != memory.read_full()
    assert estimate_tokens(view) <= llm.ASSUMED_CONTEXT_WINDOW * runner.MEMORY_CONTEXT_SHARE * 1.05

    # The assumed window is a budget, not a limit: a prompt bigger than it
    # still goes to the model (only a stated / reported window refuses).
    sent = []

    def _loop(**kwargs):
        sent.append(kwargs["user_content"])
        raise RuntimeError("stop here")

    monkeypatch.setattr(runner, "run_agent_loop", _loop)
    monkeypatch.setattr("autophysicist.llm._resolve_endpoint", lambda c: (None, "m"))
    with pytest.raises(RuntimeError, match="stop here"):
        runner._run_iteration(
            system_prompt="s",
            problem_text="p" * 200_000,
            answer_template="",
            config=config,
            permanent_memory=memory,
            scratchpad=Scratchpad(tmp_path),
            workspace_root=tmp_path,
            iteration=1,
            max_iterations=1,
            token_budget=64_000,
            tool_call_cap=15,
            max_rounds=3,
            sandbox_timeout=1,
            metrics=MetricsTracker(),
            policy=SandboxPolicy(interpreter="python3"),
        )
    assert sent
