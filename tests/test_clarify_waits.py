"""A clarify question waits for the user; it never times out into a decision.

Agents used to wait 120s (the CLI's own defaults shadowed the canonical
``agent.clarify_timeout``) and were then told to "use your best judgement and
proceed", so by the time the user came back to answer, the agent had already
acted. The default is now no timeout, an opted-in deadline tells the agent to
stop and wait, and every surface returns the same sentinel so a timed-out
batch stops asking.
"""

from __future__ import annotations

import concurrent.futures
import json
import types


def test_cli_config_never_shadows_the_clarify_timeout(tmp_path, monkeypatch) -> None:
    import cli
    from son_of_anton_constants import get_son_of_anton_home
    from tools.clarify_gateway import resolve_clarify_timeout

    config = get_son_of_anton_home() / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    assert resolve_clarify_timeout(cli.load_cli_config()) <= 0, "default must be no timeout"

    config.write_text("[agent]\nclarify_timeout = 777\n", encoding="utf-8")
    assert resolve_clarify_timeout(cli.load_cli_config()) == 777


def test_a_gateway_timeout_stops_a_batch_and_says_to_wait() -> None:
    from gateway import run as gateway_run
    from tools import clarify_tool

    sent = concurrent.futures.Future()
    sent.set_result(types.SimpleNamespace(success=True))
    clarify_mod = types.SimpleNamespace(
        get_clarify_timeout=lambda: 1,
        wait_for_response=lambda clarify_id, timeout: None,  # nobody answered
        clear_session=lambda session_key: None,
    )
    reply = gateway_run._clarify_send_then_wait(
        sent, clarify_id="c1", session_key="s1", clarify_mod=clarify_mod
    )

    asked = []

    def one_at_a_time(question, choices):
        asked.append(question)
        return reply

    result = json.loads(
        clarify_tool.clarify_tool(
            "",
            questions=[{"question": "First?"}, {"question": "Second?"}],
            callback=one_at_a_time,
        )
    )
    assert asked == ["First?"], "a timed-out batch kept asking"
    assert result["timed_out"] is True
    assert result["note"] == clarify_tool.TIMEOUT_RESPONSE
