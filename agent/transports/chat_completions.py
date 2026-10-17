"""OpenAI Chat Completions transport for the custom OpenAI-compatible endpoint.

Messages and tools are already in OpenAI format — convert_messages only strips
internal fields strict endpoints reject; build_kwargs assembles the request
(max_tokens default, reasoning effort in the route's vocabulary).
"""

from typing import Any, Dict

from agent.reasoning_effort import (
    OPENAI_COMPAT_WIRE_EFFORTS,
    clamp_effort,
    custom_reasoning_extras,
)
from agent.transports.types import NormalizedResponse, ToolCall, Usage

#: Output cap sent when the user configured none: without it some servers
#: (Ollama's num_predict=128) truncate after a few tokens.
DEFAULT_MAX_TOKENS = 65536

# Message keys written for persistence/bookkeeping that never go on the wire.
_INTERNAL_MESSAGE_KEYS = frozenset({
    "codex_reasoning_items",
    "codex_message_items",
    "tool_name",
    "effect_disposition",
    "timestamp",
    "api_content",
})
_INTERNAL_TOOL_CALL_KEYS = frozenset({"call_id", "response_item_id", "extra_content"})


def user_agent() -> str:
    """``son-of-anton-cli/<version>``: the endpoint's gateway attributes callers by it."""
    try:
        from son_of_anton_cli import __version__ as _ver

        return f"son-of-anton-cli/{_ver}"
    except Exception:
        return "son-of-anton-cli"


def _merge_extra_body(target: dict[str, Any], layer: dict[str, Any]) -> None:
    """``target.update(layer)``, except ``chat_template_kwargs`` merges per key."""
    for key, value in layer.items():
        current = target.get(key)
        if key == "chat_template_kwargs" and isinstance(current, dict) and isinstance(value, dict):
            target[key] = {**current, **value}
        else:
            target[key] = value


def _sanitize_message(msg: Any) -> Any:
    """Return *msg* without internal keys, or *msg* itself when it has none."""
    if not isinstance(msg, dict):
        return msg
    out = None
    for key in msg:
        if key in _INTERNAL_MESSAGE_KEYS or (isinstance(key, str) and key.startswith("_")):
            out = out if out is not None else dict(msg)
            out.pop(key, None)
    tool_calls = msg.get("tool_calls")
    if msg.get("role") == "assistant" and "tool_calls" in msg and not tool_calls:
        # ``tool_calls: []`` / ``null`` is a 400 on strict endpoints.
        out = out if out is not None else dict(msg)
        out.pop("tool_calls", None)
    elif isinstance(tool_calls, list) and any(
        isinstance(tc, dict) and _INTERNAL_TOOL_CALL_KEYS.intersection(tc) for tc in tool_calls
    ):
        out = out if out is not None else dict(msg)
        out["tool_calls"] = [
            {k: v for k, v in tc.items() if k not in _INTERNAL_TOOL_CALL_KEYS}
            if isinstance(tc, dict) else tc
            for tc in tool_calls
        ]
    return msg if out is None else out


class ChatCompletionsTransport:
    """Transport for the chat.completions wire."""

    api_mode = "chat_completions"

    def convert_messages(self, messages: list[dict[str, Any]], **kwargs) -> list[dict[str, Any]]:
        """Strip internal fields (``tool_name``, ``timestamp``, ``_``-prefixed
        markers, legacy Codex fields, empty ``tool_calls``) strict endpoints reject."""
        return [_sanitize_message(m) for m in messages]

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tools

    def build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params,
    ) -> dict[str, Any]:
        """Build chat.completions.create() kwargs.

        params: timeout, temperature, max_tokens, ephemeral_max_output_tokens,
        max_tokens_param_fn, reasoning_config, request_overrides, base_url.
        """
        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": self.convert_messages(messages),
        }
        if params.get("temperature") is not None:
            api_kwargs["temperature"] = params["temperature"]
        if params.get("timeout") is not None:
            api_kwargs["timeout"] = params["timeout"]
        if tools:
            api_kwargs["tools"] = tools

        # max_tokens: ephemeral > user > default cap.
        max_tokens_fn = params.get("max_tokens_param_fn")
        for cap in (
            params.get("ephemeral_max_output_tokens"),
            params.get("max_tokens"),
            DEFAULT_MAX_TOKENS,
        ):
            if cap is not None and max_tokens_fn:
                api_kwargs.update(max_tokens_fn(cap))
                break

        # ``ultra`` is internal ladder vocabulary; clamp to the widest wire set
        # before the route's own vocabulary narrows it further.
        reasoning_config = params.get("reasoning_config")
        if isinstance(reasoning_config, dict):
            effort = str(reasoning_config.get("effort") or "").strip().lower()
            clamped = clamp_effort(effort, OPENAI_COMPAT_WIRE_EFFORTS) if effort else effort
            if clamped != effort:
                reasoning_config = {**reasoning_config, "effort": clamped}
        extra_body, top_level = custom_reasoning_extras(
            reasoning_config, model=model, base_url=params.get("base_url")
        )
        api_kwargs.update(top_level)

        for key, value in (params.get("request_overrides") or {}).items():
            if key == "extra_body" and isinstance(value, dict):
                _merge_extra_body(extra_body, value)
            else:
                api_kwargs[key] = value
        if extra_body:
            api_kwargs["extra_body"] = extra_body
        return api_kwargs

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """Normalize an OpenAI ChatCompletion to NormalizedResponse.

        ``reasoning_content`` (DeepSeek-style) and ``reasoning_details`` are
        kept in provider_data for replay; a refusal that is the whole payload
        becomes the content with a ``content_filter`` finish reason.
        """
        choice = response.choices[0]
        msg = getattr(choice, "message", None)
        finish_reason = getattr(choice, "finish_reason", None)
        if isinstance(finish_reason, int):
            finish_reason = str(finish_reason)
        finish_reason = finish_reason or "stop"

        tool_calls = None
        message_tool_calls = getattr(msg, "tool_calls", None)
        if message_tool_calls:
            tool_calls = []
            for tc in message_tool_calls:
                tc_function = getattr(tc, "function", None)
                function_name = getattr(tc_function, "name", None)
                # Skip absent function/name fields, but keep an explicit blank
                # name for the recovery path.
                if tc_function is None or function_name is None:
                    continue
                function_arguments = getattr(tc_function, "arguments", None)
                tool_calls.append(ToolCall(
                    id=getattr(tc, "id", None),
                    name=function_name,
                    arguments=function_arguments if function_arguments is not None else "{}",
                ))

        usage = None
        if getattr(response, "usage", None):
            u = response.usage
            usage = Usage(
                prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                total_tokens=getattr(u, "total_tokens", 0) or 0,
            )

        reasoning = getattr(msg, "reasoning", None)
        model_extra = getattr(msg, "model_extra", None)
        model_extra = model_extra if isinstance(model_extra, dict) else {}
        reasoning_content = getattr(msg, "reasoning_content", None)
        if reasoning_content is None:
            reasoning_content = model_extra.get("reasoning_content")

        provider_data: Dict[str, Any] = {}
        if reasoning_content is not None:
            provider_data["reasoning_content"] = reasoning_content
        rd = getattr(msg, "reasoning_details", None)
        if rd:
            provider_data["reasoning_details"] = rd

        content = getattr(msg, "content", None)
        refusal = getattr(msg, "refusal", None)
        if refusal is None:
            refusal = model_extra.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            provider_data["refusal"] = refusal
            if not (isinstance(content, str) and content.strip()) and not tool_calls:
                content = refusal
                if finish_reason in (None, "stop"):
                    finish_reason = "content_filter"

        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=reasoning,
            usage=usage,
            provider_data=provider_data or None,
        )

    def validate_response(self, response: Any) -> bool:
        """Check that response has valid choices."""
        return bool(response is not None and getattr(response, "choices", None))

    def extract_cache_stats(self, response: Any) -> dict[str, int] | None:
        """Cache stats from ``prompt_tokens_details`` or DeepSeek's
        top-level ``prompt_cache_hit_tokens``."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
        written = getattr(details, "cache_write_tokens", 0) or 0 if details else 0
        if not cached:
            cached = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        if cached or written:
            return {"cached_tokens": cached, "creation_tokens": written}
        return None
