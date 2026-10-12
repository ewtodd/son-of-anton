"""Assistant reasoning must reach custom/local endpoints on replay.

Qwen3-style chat templates render ``reasoning_content`` for every assistant
turn after the last user query — the whole tool loop. Stripping it blinds
the model to its own earlier reasoning mid-chain and misses the local
server's prefix cache at every tool call. The contracts here:

* the echo decision resolves per route (per-model declaration > explicit
  flag > "local / private-network endpoints echo, everything else
  strips" — hosted APIs declared as custom providers included);
* the local default replays the model's real reasoning and never
  fabricates a pad — only the require-side families (DeepSeek / Kimi /
  MiMo) and an explicit ``reasoning_echo = true`` are padded;
* reasoning captured from a stream reaches the stored assistant message;
* a thinking-only turn is treated the same whether or not the route echoes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.message_sanitization import (
    apply_reasoning_content_policy,
    reapply_reasoning_echo,
    resolve_reasoning_echo,
)

CUSTOM_URL = "http://192.168.1.20:4002/v1"
TOOL_CALLS = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
HOSTED_URL = "https://api.groq.invalid/openai/v1"
PROVIDERS = [
    {
        "name": "custom",
        "base_url": CUSTOM_URL,
        "models": {
            "local/qwen": {},
            "local/no-echo": {"reasoning_echo": False},
            "local/kimi-proxy": {"reasoning_echo": True},
        },
    },
    # A hosted strict API declared the way every endpoint is in this fork.
    {"name": "groq", "base_url": HOSTED_URL, "models": {"llama": {}}},
]


# ── route resolution ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/v1",
        "http://localhost:11434/v1",
        "http://10.0.0.6:4002/v1",
        "http://172.20.1.2:8000/v1",
        "http://192.168.1.20:4002/v1",
        "http://169.254.10.10/v1",
        "http://[fd00::5]:8000/v1",
        "http://llama-box.local:8080/v1",
        "http://gpu.lan/v1",
        "http://strix.home.arpa/v1",
        "http://llamaswap:8080/v1",
    ],
)
def test_local_and_private_network_endpoints_echo_by_default(url: str) -> None:
    for provider in ("custom", "custom:lab", "ollama"):
        assert resolve_reasoning_echo(provider, "m", url, None, PROVIDERS), (provider, url)


@pytest.mark.parametrize(
    "url",
    [
        HOSTED_URL,
        "https://api.mistral.ai/v1",
        "https://api.cerebras.ai/v1",
        "https://gateway.example.com/v1",
    ],
)
def test_hosted_endpoints_strip_by_default_even_as_custom_providers(url: str) -> None:
    for provider in ("custom", "custom:groq", "mistral"):
        assert not resolve_reasoning_echo(provider, "llama", url, None, PROVIDERS), (provider, url)


@pytest.mark.parametrize("explicit", [True, False])
def test_an_explicit_flag_beats_the_route_default(explicit: bool) -> None:
    assert resolve_reasoning_echo("custom", "local/qwen", CUSTOM_URL, explicit, PROVIDERS) is explicit
    assert resolve_reasoning_echo("custom", "llama", HOSTED_URL, explicit, PROVIDERS) is explicit


def test_a_per_model_declaration_beats_the_explicit_flag() -> None:
    assert not resolve_reasoning_echo("custom", "local/no-echo", CUSTOM_URL, True, PROVIDERS)


# ── padding: explicit opt-in pads, the local default does not ─────────────


def _route_agent(*, model: str, base_url: str, flag):
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.provider = "custom"
    agent.model = model
    agent.base_url = base_url
    agent._custom_providers = PROVIDERS
    agent._reasoning_echo_flag = flag
    return agent


def test_local_default_echoes_without_padding() -> None:
    agent = _route_agent(model="local/qwen", base_url=CUSTOM_URL, flag=None)
    assert agent._reasoning_echo_enabled()
    assert not agent._needs_thinking_reasoning_pad()


@pytest.mark.parametrize(
    "model,base_url,flag",
    [
        # model.reasoning_echo = true on a gateway fronting a thinking model
        ("glm-5.2", "https://proxy.example.com/v1", True),
        # per-model declaration
        ("local/kimi-proxy", CUSTOM_URL, None),
    ],
)
def test_an_explicit_opt_in_echoes_and_pads(model: str, base_url: str, flag) -> None:
    agent = _route_agent(model=model, base_url=base_url, flag=flag)
    assert agent._reasoning_echo_enabled()
    assert agent._needs_thinking_reasoning_pad()
    api = {"role": "assistant", "content": "", "tool_calls": TOOL_CALLS}
    from agent.agent_runtime_helpers import copy_reasoning_content_for_api

    copy_reasoning_content_for_api(agent, dict(api), api)
    assert isinstance(api.get("reasoning_content"), str) and api["reasoning_content"]


def test_the_pad_decision_follows_a_flag_change() -> None:
    agent = _route_agent(model="glm-5.2", base_url="https://proxy.example.com/v1", flag=True)
    assert agent._needs_thinking_reasoning_pad()
    agent._reasoning_echo_flag = None  # e.g. fallback to an entry without the flag
    assert not agent._needs_thinking_reasoning_pad()
    assert not agent._reasoning_echo_enabled()


# ── replay policy ─────────────────────────────────────────────────────────


def _replay(source: dict, *, pad: bool, echo: bool) -> dict:
    api = dict(source)
    apply_reasoning_content_policy(source, api, pad, echo)
    return api




@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_echo_replays_the_models_own_reasoning_verbatim(field: str) -> None:
    text = "  I will call f, then remember PURPLE-42.\n"
    source = {"role": "assistant", "content": "", "tool_calls": TOOL_CALLS, field: text}
    assert _replay(source, pad=False, echo=True)["reasoning_content"] == text


@pytest.mark.parametrize("tool_calls", [None, TOOL_CALLS])
def test_echo_never_fabricates_a_pad(tool_calls) -> None:
    source = {"role": "assistant", "content": "plain answer"}
    if tool_calls:
        source["tool_calls"] = tool_calls
    assert "reasoning_content" not in _replay(source, pad=False, echo=True)


@pytest.mark.parametrize("blank", ["", " ", "\n"])
def test_echo_drops_a_blank_pad_left_by_another_provider(blank: str) -> None:
    source = {"role": "assistant", "content": "x", "reasoning_content": blank}
    assert "reasoning_content" not in _replay(source, pad=False, echo=True)


def test_strict_routes_never_see_the_field() -> None:
    source = {"role": "assistant", "content": "x", "reasoning_content": "real", "reasoning": "real"}
    assert "reasoning_content" not in _replay(source, pad=False, echo=False)


def test_require_side_families_are_still_padded() -> None:
    for source in (
        {"role": "assistant", "content": "plain"},
        {"role": "assistant", "content": "", "tool_calls": TOOL_CALLS},
        {"role": "assistant", "content": "", "reasoning_content": ""},
    ):
        rc = _replay(source, pad=True, echo=True)["reasoning_content"]
        assert isinstance(rc, str) and rc, source


def test_reapply_reconciles_built_messages_for_an_echo_route() -> None:
    built = [
        {"role": "assistant", "content": "a", "reasoning_content": " "},
        {"role": "assistant", "content": "b", "reasoning_content": "kept"},
        {"role": "assistant", "content": "c"},
    ]
    reapply_reasoning_echo(built, False, True)
    assert "reasoning_content" not in built[0]
    assert built[1]["reasoning_content"] == "kept"
    assert "reasoning_content" not in built[2]
    # Idempotent: a second pass changes nothing.
    assert reapply_reasoning_echo(built, False, True) == 0


# ── capture: streamed reasoning lands on the stored message ───────────────


def _bare_agent():
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.provider = "custom"
    agent.model = "local/qwen"
    agent.base_url = CUSTOM_URL
    agent.verbose_logging = False
    agent.reasoning_callback = None
    agent.stream_delta_callback = None
    agent._stream_callback = None
    agent._custom_providers = PROVIDERS
    agent._reasoning_echo_flag = None
    return agent


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_streamed_reasoning_is_stored_and_replayed_as_reasoning_content(field: str) -> None:
    from agent.agent_runtime_helpers import copy_reasoning_content_for_api
    from agent.chat_completion_helpers import build_assistant_message

    agent = _bare_agent()
    text = "Need the weather first. Code word PURPLE-42."
    message = SimpleNamespace(role="assistant", content="", tool_calls=None, **{field: text})
    stored = build_assistant_message(agent, message, "stop")
    assert stored.get("reasoning_content") == text

    api = dict(stored)
    copy_reasoning_content_for_api(agent, stored, api)
    assert api["reasoning_content"] == text


# ── thinking-only turns: same wire shape with or without echo ─────────────


@pytest.mark.parametrize("echo", [True, False])
def test_thinking_only_turn_is_dropped_regardless_of_echo(echo: bool) -> None:
    from agent.agent_runtime_helpers import (
        copy_reasoning_content_for_api,
        drop_thinking_only_and_merge_users,
        repair_empty_non_final_messages,
    )

    agent = SimpleNamespace(
        _needs_thinking_reasoning_pad=lambda: False,
        _reasoning_echo_enabled=lambda: echo,
    )
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "", "reasoning": "thinking...", "reasoning_content": "thinking..."},
        {"role": "user", "content": "q2"},
    ]
    wire = []
    for msg in history:
        api = dict(msg)
        copy_reasoning_content_for_api(agent, msg, api)
        api.pop("reasoning", None)
        wire.append(api)
    wire = drop_thinking_only_and_merge_users(repair_empty_non_final_messages(wire))
    assert [m["role"] for m in wire] == ["user"]
    assert "[response interrupted]" not in wire[0]["content"]
