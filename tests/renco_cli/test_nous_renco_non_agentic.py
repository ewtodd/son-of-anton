"""Tests for the Nous-Renco-3/4 non-agentic warning detector.

Prior to this check, the warning fired on any model whose name contained
``"renco"`` anywhere (case-insensitive). That false-positived on unrelated
local Modelfiles such as ``renco-brain:qwen3-14b-ctx16k`` — a tool-capable
Qwen3 wrapper that happens to live under the "renco" tag namespace.

``is_nous_renco_non_agentic`` should only match the actual Nous Research
Renco-3 / Renco-4 chat family.
"""

from __future__ import annotations

import pytest

from renco_cli.model_switch import (
    _RENCO_MODEL_WARNING,
    _check_renco_model_warning,
    is_nous_renco_non_agentic,
)


@pytest.mark.parametrize(
    "model_name",
    [
        "NousResearch/Renco-3-Llama-3.1-70B",
        "NousResearch/Renco-3-Llama-3.1-405B",
        "renco-3",
        "Renco-3",
        "renco-4",
        "renco-4-405b",
        "renco_4_70b",
        "openrouter/renco3:70b",
        "openrouter/nousresearch/renco-4-405b",
        "NousResearch/Renco3",
        "renco-3.1",
    ],
)
def test_matches_real_nous_renco_chat_models(model_name: str) -> None:
    assert is_nous_renco_non_agentic(model_name), (
        f"expected {model_name!r} to be flagged as Nous Renco 3/4"
    )
    assert _check_renco_model_warning(model_name) == _RENCO_MODEL_WARNING


