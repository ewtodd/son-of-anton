"""``gateway.model`` — a per-surface default out of one config.yaml.

One SON_OF_ANTON_HOME is shared by an account's Signal service and that
account's own CLI; that shared home is what makes a Signal conversation
resumable from the terminal. So the two surfaces need separate model defaults
without a second config file: ``model.default`` is what the CLI opens with,
``gateway.model`` is what the service answers with.
"""

from __future__ import annotations

from gateway.run import _resolve_gateway_model


def test_gateway_model_wins_over_model_default() -> None:
    assert (
        _resolve_gateway_model(
            {
                "model": {"default": "cli-model"},
                "gateway": {"model": "signal-model"},
            }
        )
        == "signal-model"
    )


def test_model_default_is_used_when_gateway_model_is_unset() -> None:
    """Back-compat: an existing config.yaml behaves exactly as before."""
    assert _resolve_gateway_model({"model": {"default": "cli-model"}}) == "cli-model"
    assert (
        _resolve_gateway_model({"model": {"default": "cli-model"}, "gateway": {}})
        == "cli-model"
    )


def test_blank_gateway_model_falls_through() -> None:
    """A provisioned-but-empty value must not blank the model.

    The Nix module renders ``gateway.model`` from an option whose default is
    the empty string, so "" reaches config.yaml whenever an instance does not
    pin one. Treating that as a real value would leave the gateway with no
    model at all.
    """
    for blank in ("", "   "):
        assert (
            _resolve_gateway_model(
                {"model": {"default": "cli-model"}, "gateway": {"model": blank}}
            )
            == "cli-model"
        )


def test_gateway_model_is_stripped() -> None:
    assert (
        _resolve_gateway_model(
            {"model": {"default": "cli"}, "gateway": {"model": "  spaced  "}}
        )
        == "spaced"
    )


def test_non_string_gateway_model_is_ignored() -> None:
    """config.yaml is user-edited; a mistyped value must not become the model."""
    for bogus in (True, 42, ["a"], {"x": 1}, None):
        assert (
            _resolve_gateway_model(
                {"model": {"default": "cli-model"}, "gateway": {"model": bogus}}
            )
            == "cli-model"
        )


def test_non_dict_gateway_section_is_ignored() -> None:
    assert (
        _resolve_gateway_model({"model": {"default": "cli-model"}, "gateway": "oops"})
        == "cli-model"
    )


def test_scalar_model_section_still_supported() -> None:
    """The legacy ``model: <str>`` shorthand keeps working."""
    assert _resolve_gateway_model({"model": "shorthand"}) == "shorthand"
    assert (
        _resolve_gateway_model({"model": "shorthand", "gateway": {"model": "pinned"}})
        == "pinned"
    )


def test_empty_config_returns_empty_string() -> None:
    assert _resolve_gateway_model({}) == ""
