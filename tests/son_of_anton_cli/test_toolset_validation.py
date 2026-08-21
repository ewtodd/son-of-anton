"""Unit tests for son_of_anton_cli.toolset_validation (see #38798).

Pure logic — the validity predicate is injected, so these tests need neither the
tool registry nor a running Son of Anton.
"""

import pytest

from son_of_anton_cli.toolset_validation import validate_platform_toolsets

# A representative set of real toolset names. `son-of-anton` is deliberately absent —
# that is the corruption #38798 reported (`son-of-anton-cli` rewritten to `son-of-anton`).
_KNOWN = {
    "son-of-anton-cli",
    "son-of-anton-telegram",
    "son-of-anton-discord",
    "terminal",
    "web",
}


def _is_valid(name):
    return name in _KNOWN




def test_38798_corruption_warns_and_suggests_correct_name():
    # The exact reported shape: cli holds 'son-of-anton' instead of 'son-of-anton-cli'.
    warnings = validate_platform_toolsets({"cli": ["son-of-anton"]}, _is_valid)
    unknown = [w for w in warnings if "unknown toolset 'son-of-anton'" in w]
    assert len(unknown) == 1
    # Actionable: points at the valid name the entry should have been.
    assert "did you mean 'son-of-anton-cli'?" in unknown[0]
    # And the zero-valid-toolsets safety net fires.
    assert any("zero valid toolsets" in w for w in warnings)


def test_mixed_valid_and_invalid_flags_only_the_invalid():
    cfg = {"cli": ["son-of-anton-cli"], "discord": ["bogus"]}
    warnings = validate_platform_toolsets(cfg, _is_valid)
    # One valid entry exists, so no zero-valid warning.
    assert not any("zero valid toolsets" in w for w in warnings)
    assert len(warnings) == 1
    assert "platform 'discord'" in warnings[0]
    assert "unknown toolset 'bogus'" in warnings[0]




