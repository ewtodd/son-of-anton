"""OpenAI-compatible LLM layer for the physics modes.

physics-intern shipped its own provider abstraction (Anthropic/OpenAI/
Gemini/HuggingFace/vLLM). The fork collapses that into the son-of-anton
configuration: the physics modes talk to the same endpoints as the main
agent — a local llama-swap/vLLM server or the DeepSeek API — through the
``openai`` SDK, which every supported endpoint speaks.

Endpoint resolution (first match wins):

1. ``physics.base_url`` in config.yaml (explicit local/remote endpoint)
2. provider-specific defaults: deepseek -> api.deepseek.com, openai ->
   api.openai.com
3. ``custom_providers.<provider>.base_url`` in config.yaml
4. ``http://127.0.0.1:8080/v1`` (the llama-swap convention)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)

from .agents.computer.tools import ToolExecutor
from .core.workspace import log_llm_call
from .state.tool_call import ToolCall


class ParseFailureError(Exception):
    """Raised when an agent's structured output cannot be parsed."""


class ContextTooLongError(Exception):
    """Raised when a provider rejects the request because it is too long."""

    def __init__(self, input_tokens: int = 0):
        self.input_tokens = input_tokens
        super().__init__(
            f"context too long (estimated {input_tokens} input tokens)"
        )


def is_transient(exc: BaseException) -> bool:
    """True when a retry could plausibly succeed (rate limit, 5xx, network)."""
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in (429, 500, 502, 503, 504)
    return False


@dataclass
class LLMResponse:
    """Response from an LLM call."""

    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    duration: float
    reasoning_tokens: int = 0
    answer_tokens: int = 0
    reasoning_content: str = ""
    log_path: str = ""


@dataclass
class AgentResult:
    """Result from a multi-round tool-use agent loop."""

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
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=float(config.api_timeout or 120.0),
    )
    return client, model


def _raise_if_context_error(exc: BaseException) -> None:
    text = str(exc).lower()
    if isinstance(exc, APIStatusError):
        body = ""
        try:
            body = json.dumps(exc.response.json() if hasattr(exc, "response") else {})
        except Exception:
            pass
        text = f"{text} {body}".lower()
    if "maximum context length" in text or "context length" in text or (
        isinstance(exc, APIStatusError) and exc.status_code == 400
    ):
        raise ContextTooLongError() from exc


def _create_with_retry(client, model, messages, max_tokens, config, tools=None):
    """One chat.completions call with retry on transient errors."""
    attempts = int(config.api_retry_max or 3)
    delay = float(config.api_retry_initial_delay or 2.0)
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts + 1):
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
            }
            if tools:
                kwargs["tools"] = tools
            return client.chat.completions.create(**kwargs)
        except ContextTooLongError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface anything to caller
            _raise_if_context_error(exc)
            last_exc = exc
            if not is_transient(exc) or attempt == attempts:
                raise
            time.sleep(min(delay * (2**attempt), 60.0))
    raise RuntimeError("unreachable") from last_exc


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
) -> LLMResponse:
    """Extend *result* while it is truncated, up to ``max_tokens_retries``.

    ``config.default.yaml`` documents this ("The helper resends the visible-text
    portion of the truncated response as an assistant turn and asks the model to
    continue from where it stopped") but nothing implemented it, so a response
    that hit the output cap was returned as a half-finished string — and for the
    one-shot agents, that is a JSON object with no closing brace.
    """
    retries = int(getattr(config, "max_tokens_retries", 0) or 0)
    attempt = 0
    while result.stop_reason == "max_tokens" and attempt < retries:
        attempt += 1
        conversation = (
            [{"role": "system", "content": system}]
            + list(messages)
            + [
                {"role": "assistant", "content": result.text},
                {"role": "user", "content": CONTINUATION_PROMPT},
            ]
        )
        start = time.time()
        resp = _create_with_retry(client, model, conversation, max_tokens, config)
        duration = time.time() - start
        choice = resp.choices[0]
        finish = choice.finish_reason or "end_turn"
        continuation = LLMResponse(
            text=choice.message.content or "",
            input_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
            stop_reason="max_tokens" if finish == "length" else finish,
            duration=duration,
        )
        _log_usage(
            config, f"{agent_name}_continue{attempt}", iteration, continuation, duration
        )
        if not continuation.text:
            break
        result.text += continuation.text
        result.input_tokens += continuation.input_tokens
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
    )
    _log_usage(config, agent_name, iteration, result, duration)
    return _continue_truncated(
        client,
        model,
        system,
        messages,
        config,
        agent_name,
        iteration,
        result,
        max_tokens,
    )


def call_llm_continuation(
    system: str,
    messages: list[dict],
    config,
    agent_name: str = "",
    iteration: int = 0,
    append_to_log: str = "",
) -> LLMResponse:
    """Continue a multi-turn conversation with a full ``messages`` list."""
    if not messages:
        raise ValueError(
            f"Empty messages in call_llm_continuation (agent={agent_name}, iteration={iteration})"
        )
    client, model = _resolve_endpoint(config)
    max_tokens = config.max_tokens_for_agent(agent_name)
    full_messages = [{"role": "system", "content": system}] + list(messages)
    start = time.time()
    resp = _create_with_retry(client, model, full_messages, max_tokens, config)
    duration = time.time() - start

    choice = resp.choices[0]
    finish = choice.finish_reason or "end_turn"
    result = LLMResponse(
        text=choice.message.content or "",
        input_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
        stop_reason="max_tokens" if finish == "length" else finish,
        duration=duration,
    )
    _log_usage(config, agent_name, iteration, result, duration)
    return _continue_truncated(
        client,
        model,
        system,
        list(messages),
        config,
        agent_name,
        iteration,
        result,
        max_tokens,
    )


def run_agent_loop(
    system: str,
    user_content: str,
    config,
    tool_executor: ToolExecutor,
    tools: list[dict],
    max_rounds: int = 10,
    agent_name: str = "",
    iteration: int = 0,
    on_round: Optional[Callable] = None,
) -> AgentResult:
    """Run a tool-use agent loop until the executor stops it, or max_rounds.

    *tools* are in OpenAI canonical format (``type: "function"``), the same
    format the agents already use — no adapter pass needed.

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
    total_output = 0
    start = time.time()

    for round_num in range(1, max_rounds + 1):
        round_tools = getattr(tool_executor, "active_tools", None) or tools
        resp = _create_with_retry(
            client, model, messages, max_tokens, config, tools=round_tools
        )
        round_input = getattr(resp.usage, "prompt_tokens", 0) or 0
        round_output = getattr(resp.usage, "completion_tokens", 0) or 0
        total_input += round_input
        total_output += round_output

        choice = resp.choices[0]
        message = choice.message
        truncated = choice.finish_reason == "length"

        if not getattr(message, "tool_calls", None):
            stop_reason = "max_tokens" if truncated else "end_turn"
            result = AgentResult(
                text=message.content or "",
                tool_calls=all_tool_calls,
                total_input_tokens=total_input,
                total_output_tokens=total_output,
                rounds=round_num,
                duration=time.time() - start,
                stop_reason=stop_reason,
                truncated=truncated,
            )
            if on_round:
                on_round(round_num, stop_reason, [], total_input, total_output, round_input, round_output)
            return result

        round_calls: list[ToolCall] = []
        tool_messages = []
        for tool_call in message.tool_calls:
            function = tool_call.function
            try:
                tool_input = json.loads(function.arguments or "{}")
            except json.JSONDecodeError:
                tool_input = {}
            call_start = time.time()
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
                    "tool_call_id": tool_call.id,
                    "content": output,
                }
            )

        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ],
            }
        )
        messages.extend(tool_messages)
        all_tool_calls.extend(round_calls)

        stop_reason = "max_tokens" if truncated else "tool_use"
        if on_round:
            on_round(round_num, stop_reason, round_calls, total_input, total_output, round_input, round_output)

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
                total_output_tokens=total_output,
                rounds=round_num,
                duration=time.time() - start,
                stop_reason="exit_tool",
                truncated=truncated,
            )

    return AgentResult(
        text="",
        tool_calls=all_tool_calls,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        rounds=max_rounds,
        duration=time.time() - start,
        stop_reason="max_rounds",
    )
