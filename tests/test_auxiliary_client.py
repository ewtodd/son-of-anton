"""Auxiliary-client contracts — the removed OpenRouter/Nous providers must
never be probed by the auto fallback chain, and the title thread must carry
the turn's profile secret scope.
"""

from __future__ import annotations

from agent.auxiliary_client import _get_provider_chain


def test_auto_chain_excludes_removed_aggregators() -> None:
    labels = [label for label, _ in _get_provider_chain()]
    assert labels
    assert "openrouter" not in labels
    assert "nous" not in labels
    assert "local/custom" in labels
    assert "api-key" in labels


def test_auto_title_session_reinstalls_secret_scope(monkeypatch) -> None:
    import agent.title_generator as tg
    from agent.secret_scope import current_secret_scope

    seen: dict = {}

    def _fake_worker(*args, **kwargs):
        seen["scope"] = current_secret_scope()

    monkeypatch.setattr(tg, "_auto_title_session", _fake_worker)
    tg.auto_title_session(
        None,
        "sid",
        "hello",
        secret_scope={"OPENAI_API_KEY": "k"},
    )
    assert seen["scope"] == {"OPENAI_API_KEY": "k"}
    # The thread body must restore the outer scope on the way out.
    assert current_secret_scope() is None
