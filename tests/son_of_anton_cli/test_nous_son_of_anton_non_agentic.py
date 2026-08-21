"""Tests for the Nous-Son of Anton-3/4 non-agentic warning detector.

Prior to this check, the warning fired on any model whose name contained
``"son-of-anton"`` anywhere (case-insensitive). That false-positived on unrelated
local Modelfiles such as ``son-of-anton-brain:qwen3-14b-ctx16k`` — a tool-capable
Qwen3 wrapper that happens to live under the "son-of-anton" tag namespace.

``is_nous_son_of_anton_non_agentic`` should only match the actual Nous Research
Son of Anton-3 / Son of Anton-4 chat family.
"""

from __future__ import annotations

import pytest

from son_of_anton_cli.model_switch import (
    _SON_OF_ANTON_MODEL_WARNING,
    _check_son_of_anton_model_warning,
    is_nous_son_of_anton_non_agentic,
)


@pytest.mark.parametrize(
    "model_name",
    [
        "NousResearch/Son of Anton-3-Llama-3.1-70B",
        "NousResearch/Son of Anton-3-Llama-3.1-405B",
        "son-of-anton-3",
        "Son of Anton-3",
        "son-of-anton-4",
        "son-of-anton-4-405b",
        "son_of_anton_4_70b",
        "openrouter/son-of-anton3:70b",
        "openrouter/nousresearch/son-of-anton-4-405b",
        "NousResearch/Son of Anton3",
        "son-of-anton-3.1",
    ],
)
def test_matches_real_nous_son_of_anton_chat_models(model_name: str) -> None:
    assert is_nous_son_of_anton_non_agentic(model_name), (
        f"expected {model_name!r} to be flagged as Nous Son of Anton 3/4"
    )
    assert _check_son_of_anton_model_warning(model_name) == _SON_OF_ANTON_MODEL_WARNING


