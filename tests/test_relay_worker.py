"""The worker side of a relay run: a headless terminal turn nobody is watching.

Clarify must answer at once — the CLI's own callback would wait forever for a
terminal user — and its answer must read as "nobody answered" to compaction.
A new worker takes the session id the relay chat assigned, and only when it
is not resuming; the variable never leaks to what the worker starts.
"""

from __future__ import annotations

import json
import threading

import pytest

from son_of_anton_cli import relay_worker


def _in_thread(fn, timeout: float = 5.0):
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("v", fn()), daemon=True)
    worker.start()
    worker.join(timeout)
    assert not worker.is_alive(), "clarify blocked in a relay run"
    return out["v"]


def test_relay_clarify_answers_at_once_for_one_question() -> None:
    from agent.context_compactor import _is_clarify_non_response_sentinel
    from tools.clarify_tool import clarify_tool

    result = json.loads(_in_thread(lambda: clarify_tool(
        "Which framework?", choices=["pytest", "unittest"],
        callback=relay_worker.relay_clarify_callback,
    )))
    assert result["user_response"] == relay_worker.RELAY_CLARIFY_RESPONSE
    assert _is_clarify_non_response_sentinel(result["user_response"])


def test_relay_clarify_answers_at_once_for_a_batch() -> None:
    from agent.context_compactor import _is_clarify_non_response_sentinel
    from tools.clarify_tool import clarify_tool

    result = json.loads(_in_thread(lambda: clarify_tool(
        "", questions=[
            {"question": "Which framework?", "choices": ["a", "b"], "multi_select": True},
            {"question": "Where?"},
        ],
        callback=relay_worker.relay_clarify_callback,
    )))
    for row in result["responses"]:
        assert _is_clarify_non_response_sentinel(row["user_response"])


def test_relay_marker_is_read_once() -> None:
    env = {relay_worker.RELAY_RUN_ENV: "1"}
    assert relay_worker.take_relay_run_marker(env) is True
    assert relay_worker.take_relay_run_marker(env) is False


@pytest.fixture
def make_cli(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("SON_OF_ANTON_DEFER_AGENT_STARTUP", "1")
    made = []

    def _make(**kwargs):
        from cli import SonOfAntonCLI

        instance = SonOfAntonCLI(
            model="test-model", provider="openai", api_key="sk-test",
            base_url="https://example.invalid/v1", **kwargs,
        )
        made.append(instance)
        return instance

    yield _make
    for instance in made:
        if getattr(instance, "_session_db", None) is not None:
            instance._session_db.close()


def test_a_new_worker_takes_the_assigned_session_id(make_cli, monkeypatch) -> None:
    import os

    monkeypatch.setenv(relay_worker.NEW_SESSION_ID_ENV, "20261015_120000_abc123")
    cli = make_cli()
    assert cli.session_id == "20261015_120000_abc123"
    assert relay_worker.NEW_SESSION_ID_ENV not in os.environ


def test_resuming_ignores_an_assigned_id(make_cli, monkeypatch) -> None:
    import os

    monkeypatch.setenv(relay_worker.NEW_SESSION_ID_ENV, "20261015_120000_abc123")
    cli = make_cli(resume="20261001_090000_def456")
    assert cli.session_id == "20261001_090000_def456"
    assert relay_worker.NEW_SESSION_ID_ENV not in os.environ


def test_an_assigned_id_that_exists_or_is_malformed_is_refused(make_cli, monkeypatch) -> None:
    from son_of_anton_state import SessionDB

    db = SessionDB()
    db.create_session("20261015_120000_taken0", source="cli")
    db.close()
    monkeypatch.setenv(relay_worker.NEW_SESSION_ID_ENV, "20261015_120000_taken0")
    with pytest.raises(ValueError):
        make_cli()
    monkeypatch.setenv(relay_worker.NEW_SESSION_ID_ENV, "../escape")
    with pytest.raises(ValueError):
        make_cli()
