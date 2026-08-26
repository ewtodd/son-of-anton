"""Auxiliary-client contracts — the removed OpenRouter/Nous providers must
never be probed by the auto fallback chain.
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
