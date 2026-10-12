"""OpenAI-compatible LLM layer for the physics modes.

physics-intern shipped its own provider abstraction (Anthropic/OpenAI/
Gemini/HuggingFace/vLLM). The fork collapses that into the son-of-anton
configuration: the physics modes talk to the same endpoints as the main
agent — a local llama-swap/vLLM server or the DeepSeek API — through the
``openai`` SDK, which every supported endpoint speaks.

Endpoint resolution (first match wins):

1. ``physics.base_url`` in config.toml (explicit local/remote endpoint)
2. provider-specific defaults: deepseek -> api.deepseek.com, openai ->
   api.openai.com
3. ``custom_providers.<provider>.base_url`` in config.toml
4. ``http://127.0.0.1:8080/v1`` (the llama-swap convention)
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)

from .core.tool_call import ToolCall
from .core.workspace import log_llm_call


class ContextTooLongError(Exception):
    """Raised when a provider rejects the request because it is too long.

    *input_tokens* is the prompt size and *limit* the model's window, each 0
    when the provider's error did not say.
    """

    def __init__(self, input_tokens: int = 0, limit: int = 0):
        self.input_tokens = input_tokens
        self.limit = limit
        detail = f"{input_tokens} input tokens" if input_tokens else "size unknown"
        if limit:
            detail += f", window {limit}"
        super().__init__(f"context too long ({detail})")


def is_transient(exc: BaseException) -> bool:
    """True when a retry could plausibly succeed (rate limit, 5xx, network)."""
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in (429, 500, 502, 503, 504)
    return False


@dataclass
class LLMResponse:
    """Response from an LLM call.

    ``new_input_tokens`` is the part of ``input_tokens`` that was new to the
    conversation (see :func:`new_prompt_tokens`); for a one-shot call that is
    the whole prompt.
    """

    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    duration: float
    reasoning_tokens: int = 0
    answer_tokens: int = 0
    reasoning_content: str = ""
    log_path: str = ""
    new_input_tokens: int = 0


@dataclass
class AgentResult:
    """Result from a multi-round tool-use agent loop.

    ``total_input_tokens`` sums every round's ``prompt_tokens`` — each round
    re-sends the whole conversation, so it grows quadratically with the round
    count and measures how much prompt the endpoint was handed, not how much
    work the loop added. ``total_new_input_tokens`` counts each prompt token
    once (see :func:`new_prompt_tokens`); with ``total_output_tokens`` it is
    what the token budget charges.
    """

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    rounds: int = 0
    truncated: bool = False
    duration: float = 0.0
    stop_reason: str = "end_turn"
    token_alert_fired: bool = False
    total_reasoning_tokens: int = 0
    total_answer_tokens: int = 0
    total_new_input_tokens: int = 0


def _cached_prompt_tokens(usage) -> Optional[int]:
    """``usage.prompt_tokens_details.cached_tokens``, or None when not reported."""
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return None
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
    else:
        cached = getattr(details, "cached_tokens", None)
    if isinstance(cached, bool) or not isinstance(cached, int):
        return None
    return cached


def new_prompt_tokens(usage, previous_prompt_tokens: int) -> int:
    """How many of this request's prompt tokens are new to the conversation.

    A tool loop re-sends its whole history every round, so ``prompt_tokens``
    counts round 1's prompt again in round 2, 3, ... — summing it charges a
    30-round iteration roughly 15 times for its system prompt. A spend budget
    wants each token once:

    * When the provider reports a prefix-cache hit
      (``prompt_tokens_details.cached_tokens > 0``), the uncached remainder
      ``prompt_tokens - cached_tokens`` is exactly the new part.
    * Otherwise — the field is absent, or a proxy fills it with a placeholder
      0 — it is the growth since the previous request of the same
      conversation, ``prompt_tokens - previous_prompt_tokens``.
    * When that growth is negative the conversation was rewritten (trimmed to
      fit the window, or a fresh conversation); the whole prompt is new again.

    *previous_prompt_tokens* is 0 for a conversation's first request, which
    makes its whole prompt new.
    """
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    cached = _cached_prompt_tokens(usage)
    if cached is not None and 0 < cached <= prompt:
        return prompt - cached
    delta = prompt - int(previous_prompt_tokens or 0)
    return prompt if delta < 0 else delta


def _load_agent_config() -> dict:
    try:
        from son_of_anton_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _resolve_endpoint(config):
    """Return (OpenAI client, model name) from the son-of-anton config."""
    agent_cfg = _load_agent_config()
    physics = agent_cfg.get("physics") or {}
    model_cfg = agent_cfg.get("model") or {}
    provider = (model_cfg.get("provider") or "").strip() or "custom"

    model = config.model or physics.get("model") or model_cfg.get("default") or ""

    base_url = physics.get("base_url") or ""
    api_key_env = physics.get("api_key_env") or ""
    if not base_url:
        if provider == "deepseek":
            base_url = "https://api.deepseek.com/v1"
            api_key_env = api_key_env or "DEEPSEEK_API_KEY"
        elif provider == "openai":
            base_url = "https://api.openai.com/v1"
            api_key_env = api_key_env or "OPENAI_API_KEY"
        else:
            custom = (agent_cfg.get("custom_providers") or {}).get(provider) or {}
            base_url = custom.get("base_url") or "http://127.0.0.1:8080/v1"
            api_key_env = api_key_env or custom.get("api_key_env") or "OPENAI_API_KEY"

    api_key = os.getenv(api_key_env, "") or "none"
    # max_retries=0: retrying is _create_with_retry's job. The SDK's own
    # default (2 retries) nested inside it multiplied every attempt by three,
    # each one allowed the full api_timeout.
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=float(config.api_timeout or 120.0),
        max_retries=0,
    )
    return client, model


#: Substrings that actually mean "the request was too long for the model".
_CONTEXT_ERROR_MARKERS = (
    "maximum context length",
    "context length",
    "context_length_exceeded",
    "too many tokens",
    "reduce the length",
    "input is too long",
    "prompt is too long",
)


def _clamp_physics_reasoning_effort(effort: str, model: str, config) -> str:
    """Clamp a physics reasoning effort to the route's declared vocabulary.

    Physics sends the effort verbatim to every round, so an effort the
    endpoint rejects (a Qwen3-on-vLLM route 400s on minimal/high/max/ultra)
    would abort every iteration. When the route declares
    ``custom_providers.<name>.models.<model>.reasoning_efforts``, clamp
    against it (nearest-weaker). Otherwise — including the deepseek/openai
    default endpoints — return *effort* unchanged to preserve behaviour.
    """
    if not model or not effort:
        return effort
    # Reuse the endpoint resolution chain for the route base_url only. The
    # OpenAI client stores it as client.base_url (an httpx.URL).
    try:
        _client, _ = _resolve_endpoint(config)
        base_url = str(getattr(_client, "base_url", "") or "").rstrip("/")
    except Exception:
        return effort
    if not base_url:
        return effort
    try:
        from son_of_anton_cli.config import get_custom_provider_reasoning_decl
        decl = get_custom_provider_reasoning_decl(str(model), base_url) or {}
    except Exception:
        return effort
    efforts = decl.get("efforts")
    if not efforts:
        return effort
    try:
        from agent.reasoning_effort import clamp_effort
        return clamp_effort(effort, tuple(efforts)) or effort
    except Exception:
        return effort


#: Status codes a context overflow can arrive under. A 429 can also say "too
#: many tokens" — that is a tokens-per-minute RATE limit, which a retry fixes
#: and trimming the request does not.
_CONTEXT_ERROR_STATUSES = (400, 413, 422)

_LIMIT_RE = re.compile(r"maximum context length is (\d+)")
_REQUESTED_RE = re.compile(
    r"\((\d+) in the messages"
    r"|messages resulted in (\d+) tokens"
    r"|request has (\d+) input tokens"
)


def _raise_if_context_error(exc: BaseException) -> None:
    """Reclassify a genuine context overflow, and only that.

    This used to treat EVERY 400 as a context error. A malformed request —
    a duplicated system turn, an unsupported parameter, a bad tool schema —
    surfaced as ``ContextTooLongError: context too long``, and the real fault
    was never seen.

    Only a request-shaped status counts (400/413/422): a 429 whose body says
    "too many tokens" is a rate limit, and stays transient. When the provider
    names its window ("maximum context length is N" — vLLM and OpenAI both
    do) and the prompt size, both ride on the error so ``run_agent_loop`` can
    size its recovery instead of guessing.
    """
    status = getattr(exc, "status_code", None)
    if status is not None and status not in _CONTEXT_ERROR_STATUSES:
        return
    text = str(exc)
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            text = f"{text} {json.dumps(response.json())}"
        except Exception:
            pass
    text = text.lower()
    if not any(marker in text for marker in _CONTEXT_ERROR_MARKERS):
        return
    limit = 0
    requested = 0
    match = _LIMIT_RE.search(text)
    if match:
        limit = int(match.group(1))
    match = _REQUESTED_RE.search(text)
    if match:
        requested = int(next(g for g in match.groups() if g))
    raise ContextTooLongError(requested, limit=limit) from exc


def _create_with_retry(
    client, model, messages, max_tokens, config, tools=None, tool_choice=None
):
    """One chat.completions call with retry on transient errors.

    Bounded three ways: ``api_retry_max`` retries; a backoff that never sleeps
    longer than ``api_retry_max_delay``; and ``api_retry_total_timeout``, the
    wall clock one call may take across all of its attempts. Each attempt's
    own timeout is clipped to what is left of that total, so a long
    ``api_timeout`` (sized for one slow reasoning call) times the retry count
    cannot turn a dead endpoint into an overnight hang.
    """
    attempts = int(config.api_retry_max or 3)
    delay = float(config.api_retry_initial_delay or 2.0)
    max_delay = float(getattr(config, "api_retry_max_delay", 0) or 60.0)
    total = float(getattr(config, "api_retry_total_timeout", 0) or 0)
    deadline = time.monotonic() + total if total > 0 else None
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts + 1):
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
            }
            # A thinking model's default effort is its highest. Qwen3.8's is
            # "xhigh", which spent 80,000 characters reasoning and produced no
            # answer at all inside a 24k budget. Endpoints that do not take the
            # parameter ignore it.
            #
            # When the route declares its accepted ``reasoning_efforts``
            # (custom_providers.<name>.models.<model>), clamp against it —
            # a Qwen3-on-vLLM route accepts exactly none/low/medium/xhigh and
            # 400s on minimal/high/max/ultra, and this layer sends the effort
            # verbatim to every physics round.
            effort = str(getattr(config, "reasoning_effort", "") or "").strip()
            if effort:
                effort = _clamp_physics_reasoning_effort(effort, model, config)
                if effort:
                    kwargs["reasoning_effort"] = effort
            if tools:
                kwargs["tools"] = tools
                if tool_choice:
                    kwargs["tool_choice"] = tool_choice
            if deadline is not None:
                remaining = deadline - time.monotonic()
                per_attempt = float(config.api_timeout or 120.0)
                kwargs["timeout"] = max(1.0, min(per_attempt, remaining))
            return client.chat.completions.create(**kwargs)
        except ContextTooLongError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface anything to caller
            _raise_if_context_error(exc)
            last_exc = exc
            if not is_transient(exc) or attempt == attempts:
                raise
            sleep_for = min(delay * (2**attempt), max_delay)
            if deadline is not None and time.monotonic() + sleep_for >= deadline:
                raise
            time.sleep(sleep_for)
    raise RuntimeError("unreachable") from last_exc


def _log_round(config, agent_name, iteration, round_num, resp, duration, finish):
    """Record one tool-loop round in EVENT_LOG.jsonl.

    ``run_agent_loop`` logged nothing, so the Manager — which runs entirely
    through it — was invisible: a run's event log showed only its sub-agents,
    and questions like "was this iteration faster because of the model or
    because the prefix was cached" had no data behind them. Rounds are where
    the Manager's time actually goes.
    """
    if not config.workspace_dir:
        return
    log_llm_call(
        config.workspace_dir,
        agent_name,
        iteration,
        getattr(config, "model", "") or "default",
        getattr(resp.usage, "prompt_tokens", 0) or 0,
        getattr(resp.usage, "completion_tokens", 0) or 0,
        finish,
        round(duration, 2),
        0,
        0,
        0,
        round=round_num,
    )


def _log_usage(config, agent_name, iteration, response, duration):
    if config.workspace_dir:
        log_llm_call(
            config.workspace_dir,
            agent_name,
            iteration,
            config.model or "default",
            response.input_tokens,
            response.output_tokens,
            response.stop_reason,
            round(duration, 2),
            0,
            0,
            0,
        )


CONTINUATION_PROMPT = (
    "Your previous message stopped because it reached the output token limit, "
    "mid-sentence. Continue from exactly where it stopped. Do not repeat any "
    "text you have already written, and do not restate the question — emit "
    "only the remainder."
)

#: The same situation with nothing visible to continue: a reasoning model
#: spent the whole output budget thinking and never started its answer.
EMPTY_ANSWER_PROMPT = (
    "Your previous response reached the output token limit before you wrote "
    "any answer. Write your answer now — concisely, without re-deriving what "
    "you already worked out."
)

#: Sent once when a tool loop with ``final_answer_on_max_rounds`` uses its
#: last round without answering.
ROUND_CAP_PROMPT = (
    "You have used all {rounds} of your tool rounds. Do not call any more "
    "tools. Give your final answer now, from what you have gathered so far."
)


def _continue_truncated(
    client,
    model,
    system: str,
    messages: list[dict],
    config,
    agent_name: str,
    iteration: int,
    result: LLMResponse,
    max_tokens: int,
    tools: Optional[list[dict]] = None,
) -> LLMResponse:
    """Extend *result* while it is truncated, up to ``max_tokens_retries``.

    *tools*, when the conversation is a tool loop's, are re-sent with
    ``tool_choice="none"``: the history holds tool calls, and the
    continuation must be text.

    ``config.default.toml`` documents this ("The helper resends the visible-text
    portion of the truncated response as an assistant turn and asks the model to
    continue from where it stopped") but nothing implemented it, so a response
    that hit the output cap was returned as a half-finished string — and for the
    one-shot agents, that is a JSON object with no closing brace.
    """
    retries = int(getattr(config, "max_tokens_retries", 0) or 0)
    attempt = 0
    # The request before each continuation, for new_prompt_tokens: the
    # continuation re-sends it plus the text so far.
    last_prompt = result.input_tokens
    while result.stop_reason == "max_tokens" and attempt < retries:
        attempt += 1
        conversation = (
            [{"role": "system", "content": system}]
            + list(messages)
            + [
                {"role": "assistant", "content": result.text},
                {
                    "role": "user",
                    "content": (
                        CONTINUATION_PROMPT
                        if result.text.strip()
                        else EMPTY_ANSWER_PROMPT
                    ),
                },
            ]
        )
        start = time.time()
        tool_kwargs = {"tools": tools, "tool_choice": "none"} if tools else {}
        resp = _create_with_retry(
            client, model, conversation, max_tokens, config, **tool_kwargs
        )
        duration = time.time() - start
        choice = resp.choices[0]
        finish = choice.finish_reason or "end_turn"
        continuation = LLMResponse(
            text=choice.message.content or "",
            input_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
            stop_reason="max_tokens" if finish == "length" else finish,
            duration=duration,
            new_input_tokens=new_prompt_tokens(resp.usage, last_prompt),
        )
        last_prompt = continuation.input_tokens
        _log_usage(
            config, f"{agent_name}_continue{attempt}", iteration, continuation, duration
        )
        if not continuation.text:
            break
        result.text += continuation.text
        result.input_tokens += continuation.input_tokens
        result.new_input_tokens += continuation.new_input_tokens
        result.output_tokens += continuation.output_tokens
        result.duration += continuation.duration
        result.stop_reason = continuation.stop_reason
    return result


def call_llm(
    system: str,
    user_content: str,
    config,
    agent_name: str = "",
    iteration: int = 0,
) -> LLMResponse:
    """Call the LLM once (system + user), with retry on transient errors."""
    if not user_content or not user_content.strip():
        raise ValueError(
            f"Empty user content in call_llm (agent={agent_name}, iteration={iteration})"
        )
    client, model = _resolve_endpoint(config)
    max_tokens = config.max_tokens_for_agent(agent_name)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    start = time.time()
    resp = _create_with_retry(client, model, messages, max_tokens, config)
    duration = time.time() - start

    choice = resp.choices[0]
    finish = choice.finish_reason or "end_turn"
    result = LLMResponse(
        text=choice.message.content or "",
        input_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
        stop_reason="max_tokens" if finish == "length" else finish,
        duration=duration,
        new_input_tokens=new_prompt_tokens(resp.usage, 0),
    )
    _log_usage(config, agent_name, iteration, result, duration)
    # messages[1:] — the system turn is prepended by _continue_truncated, and
    # passing the whole list produced [system, system, user, ...]. Providers
    # reject that ("System message must be at the beginning"), so every
    # truncated one-shot response failed instead of continuing.
    return _continue_truncated(
        client,
        model,
        system,
        messages[1:],
        config,
        agent_name,
        iteration,
        result,
        max_tokens,
    )


def _message_reasoning(message) -> str:
    """The reasoning a response carried, under either field name.

    vLLM's reasoning parser has emitted it as ``reasoning_content`` and, more
    recently, ``reasoning``; proxies pass through one or the other. The SDK
    keeps unknown fields as extra attributes.
    """
    extra = getattr(message, "model_extra", None)
    for name in ("reasoning_content", "reasoning"):
        value = getattr(message, name, None)
        if value is None and isinstance(extra, dict):
            value = extra.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def _parse_tool_arguments(raw, *, truncated: bool) -> tuple[Optional[dict], str]:
    """Parse one tool call's arguments into ``(arguments, problem)``.

    *arguments* is None when the call must not run, and *problem* says why.
    Empty arguments are an empty object — a no-argument call like ``end_turn``
    — unless the response was cut off at the output limit, when empty means
    the arguments never arrived.
    """
    if isinstance(raw, dict):
        return raw, ""
    if raw is None or not str(raw).strip():
        if truncated:
            return None, "truncated"
        return {}, ""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None, "truncated" if truncated else "invalid"
    if not isinstance(parsed, dict):
        return None, "invalid"
    return parsed, ""


def _rejected_call_message(name: str, problem: str) -> str:
    if problem == "truncated":
        return (
            f"ERROR: this {name} call was NOT executed. Your response reached "
            "the output token limit before the call's arguments were complete. "
            "Issue it again with complete arguments — if an argument is long, "
            "shorten it or split the work across several calls."
        )
    return (
        f"ERROR: this {name} call was NOT executed: its arguments were not a "
        "valid JSON object. Issue it again with valid JSON arguments."
    )


def run_agent_loop(
    system: str,
    user_content: str,
    config,
    tool_executor: Any,
    tools: list[dict],
    max_rounds: int = 10,
    agent_name: str = "",
    iteration: int = 0,
    on_round: Optional[Callable] = None,
    final_answer_on_max_rounds: bool = False,
) -> AgentResult:
    """Run a tool-use agent loop until the executor stops it, or max_rounds.

    *tools* are in OpenAI canonical format (``type: "function"``), the same
    format the agents already use — no adapter pass needed.

    A final answer cut off at the output cap is continued, as ``call_llm``
    does, up to ``max_tokens_retries`` times; ``stop_reason`` stays
    ``"max_tokens"`` only if it is still cut off after that. When the loop
    reaches *max_rounds*, ``stop_reason`` is ``"max_rounds"`` and the text is
    the model's last non-empty message — or, with
    *final_answer_on_max_rounds*, its answer to one more tools-off request
    asking for it. That is for agents whose answer IS the result (a
    sub-agent); the Manager's result is what it wrote to memory.

    The loop honours the executor duck-type protocol that the executors in
    this package document and depend on:

    ``active_tools``
        Optional property returning the tool set for the coming round, so an
        executor can gate tools by lifecycle stage (the computer agent must
        call ``document_approach`` before it may call ``execute_python``; the
        Manager loses ``dispatch_subagent`` during wind-down). ``None`` keeps
        *tools*.
    ``end_round()``
        Optional hook called after each round's tool results are appended. A
        returned string is injected as a user turn — this is how the Manager's
        budget and tool-call-cap warnings reach the model.
    ``stop_after_round``
        Checked after ``end_round()``. This is what makes the exit tools exit:
        ``end_turn`` / ``submit_final_answer`` / ``submit_result`` set it, and
        without this check the loop ran on to ``max_rounds`` every time and
        ``submit_final_answer`` did not terminate anything.
    """
    client, model = _resolve_endpoint(config)
    max_tokens = config.max_tokens_for_agent(agent_name)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

    all_tool_calls: list[ToolCall] = []
    total_input = 0
    total_new_input = 0
    total_output = 0
    previous_prompt = 0
    last_content = ""
    round_tools = tools
    start = time.time()

    for round_num in range(1, max_rounds + 1):
        round_tools = getattr(tool_executor, "active_tools", None) or tools
        round_start = time.time()
        resp = _create_with_retry(
            client, model, messages, max_tokens, config, tools=round_tools
        )
        _log_round(
            config,
            agent_name,
            iteration,
            round_num,
            resp,
            time.time() - round_start,
            resp.choices[0].finish_reason or "end_turn",
        )
        round_input = getattr(resp.usage, "prompt_tokens", 0) or 0
        round_output = getattr(resp.usage, "completion_tokens", 0) or 0
        round_new_input = new_prompt_tokens(resp.usage, previous_prompt)
        previous_prompt = round_input
        total_input += round_input
        total_new_input += round_new_input
        total_output += round_output
        # What the token budget charges: each prompt token once, plus output.
        spent = {
            "round_new_input": round_new_input,
            "total_new_input": total_new_input,
        }

        choice = resp.choices[0]
        message = choice.message
        truncated = choice.finish_reason == "length"

        if message.content:
            last_content = message.content

        if not getattr(message, "tool_calls", None):
            text = message.content or ""
            if truncated:
                # The final answer hit the output cap. call_llm continued such
                # a response and this loop returned it half-finished — for a
                # sub-agent, a script with no end, or nothing at all when a
                # reasoning model spent the whole budget thinking.
                extended = _continue_truncated(
                    client,
                    model,
                    system,
                    messages[1:],
                    config,
                    agent_name,
                    iteration,
                    LLMResponse(
                        text=text,
                        input_tokens=round_input,
                        output_tokens=round_output,
                        stop_reason="max_tokens",
                        duration=0.0,
                        new_input_tokens=round_new_input,
                    ),
                    max_tokens,
                    tools=round_tools,
                )
                text = extended.text
                total_input += extended.input_tokens - round_input
                total_new_input += extended.new_input_tokens - round_new_input
                total_output += extended.output_tokens - round_output
                spent["total_new_input"] = total_new_input
                truncated = extended.stop_reason == "max_tokens"
            stop_reason = "max_tokens" if truncated else "end_turn"
            result = AgentResult(
                text=text,
                tool_calls=all_tool_calls,
                total_input_tokens=total_input,
                total_new_input_tokens=total_new_input,
                total_output_tokens=total_output,
                rounds=round_num,
                duration=time.time() - start,
                stop_reason=stop_reason,
                truncated=truncated,
            )
            if on_round:
                on_round(round_num, stop_reason, [], total_input, total_output, round_input, round_output, **spent)
            return result

        round_calls: list[ToolCall] = []
        tool_messages = []
        replayed_calls = []
        for index, tool_call in enumerate(message.tool_calls):
            function = tool_call.function
            # Some endpoints omit the id. The tool result must still be paired
            # with its call, and a None id breaks that on the next request.
            call_id = getattr(tool_call, "id", None) or f"call_r{round_num}_{index}"
            raw_arguments = getattr(function, "arguments", None)
            tool_input, problem = _parse_tool_arguments(
                raw_arguments, truncated=truncated
            )
            call_start = time.time()
            if tool_input is None:
                # Never execute a call whose arguments did not survive: a
                # truncated write_to_permanent_memory run with {} is an error
                # the model cannot see, and a cut-off one run with half its
                # content is worse. Tell it instead.
                output = _rejected_call_message(function.name, problem)
                is_error = True
                tool_input = {}
            else:
                is_error = False
                try:
                    executed = tool_executor.execute(function.name, tool_input)
                    output = executed.output
                    is_error = executed.is_error
                except Exception as exc:  # noqa: BLE001 — tool errors are data
                    output = f"tool error: {exc}"
                    is_error = True
            round_calls.append(
                ToolCall(
                    tool_name=function.name,
                    tool_input=tool_input,
                    output=output,
                    is_error=is_error,
                    duration=time.time() - call_start,
                )
            )
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": output,
                }
            )
            replayed_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": function.name,
                        # Replayed arguments must be valid JSON whatever the
                        # model emitted: chat templates parse them, and a
                        # truncated string 400s every later round.
                        "arguments": (
                            raw_arguments
                            if not problem
                            and isinstance(raw_arguments, str)
                            and raw_arguments.strip()
                            else json.dumps(tool_input)
                        ),
                    },
                }
            )

        assistant_turn: dict[str, Any] = {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": replayed_calls,
        }
        # Replay the reasoning that produced these calls. Qwen3's chat
        # template renders reasoning_content for the assistant turns after the
        # last user message; without it the model loses its own interleaved
        # reasoning between rounds, and the prompt differs from what the
        # server generated, so its prefix cache misses at every round.
        reasoning = _message_reasoning(message)
        if reasoning:
            assistant_turn["reasoning_content"] = reasoning
        messages.append(assistant_turn)
        messages.extend(tool_messages)
        all_tool_calls.extend(round_calls)

        stop_reason = "max_tokens" if truncated else "tool_use"
        if on_round:
            on_round(round_num, stop_reason, round_calls, total_input, total_output, round_input, round_output, **spent)

        end_round = getattr(tool_executor, "end_round", None)
        if callable(end_round):
            injected = end_round()
            if injected:
                messages.append({"role": "user", "content": injected})

        if getattr(tool_executor, "stop_after_round", False):
            return AgentResult(
                text=message.content or "",
                tool_calls=all_tool_calls,
                total_input_tokens=total_input,
                total_new_input_tokens=total_new_input,
                total_output_tokens=total_output,
                rounds=round_num,
                duration=time.time() - start,
                stop_reason="exit_tool",
                truncated=truncated,
            )

    # The round cap. A sub-agent that spent every round on lookups used to come
    # back with text="" here, which its caller reported as "wrote no code".
    # With final_answer_on_max_rounds it is asked once, tools off, for the
    # answer it has; failing that, the last thing it did say is returned.
    text = last_content
    truncated = False
    if final_answer_on_max_rounds:
        cap_prompt = ROUND_CAP_PROMPT.format(rounds=max_rounds)
        if messages[-1]["role"] == "user":
            # end_round already injected a user turn; never two in a row.
            messages[-1] = {
                "role": "user",
                "content": f"{messages[-1]['content']}\n\n{cap_prompt}",
            }
        else:
            messages.append({"role": "user", "content": cap_prompt})
        round_start = time.time()
        resp = _create_with_retry(
            client,
            model,
            messages,
            max_tokens,
            config,
            tools=round_tools,
            tool_choice="none" if round_tools else None,
        )
        finish = resp.choices[0].finish_reason or "end_turn"
        _log_round(
            config,
            agent_name,
            iteration,
            max_rounds + 1,
            resp,
            time.time() - round_start,
            finish,
        )
        final_input = getattr(resp.usage, "prompt_tokens", 0) or 0
        final_output = getattr(resp.usage, "completion_tokens", 0) or 0
        final_new_input = new_prompt_tokens(resp.usage, previous_prompt)
        answer = LLMResponse(
            text=resp.choices[0].message.content or "",
            input_tokens=final_input,
            output_tokens=final_output,
            stop_reason="max_tokens" if finish == "length" else "end_turn",
            duration=time.time() - round_start,
            new_input_tokens=final_new_input,
        )
        answer = _continue_truncated(
            client,
            model,
            system,
            messages[1:],
            config,
            agent_name,
            iteration,
            answer,
            max_tokens,
            tools=round_tools,
        )
        total_input += answer.input_tokens
        total_new_input += answer.new_input_tokens
        total_output += answer.output_tokens
        if answer.text.strip():
            text = answer.text
            truncated = answer.stop_reason == "max_tokens"

    return AgentResult(
        text=text,
        tool_calls=all_tool_calls,
        total_input_tokens=total_input,
        total_new_input_tokens=total_new_input,
        total_output_tokens=total_output,
        rounds=max_rounds,
        duration=time.time() - start,
        stop_reason="max_rounds",
        truncated=truncated,
    )
