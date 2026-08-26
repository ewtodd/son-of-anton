"""Signal needs no home channel — it has exactly one place to talk.

A home channel is the default destination for output not tied to a chat (cron
results, cross-platform sends). On Discord or Slack, where one bot sits in many
channels, choosing one is a real decision. A Signal gateway answers exactly one
group, so asking produced a first-turn notice on every new session telling the
operator to run ``/sethome`` in the only conversation the bot had.
"""

from __future__ import annotations

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform

GROUP = "kujiYVYYHM9LhgvhO4ngKHPybAAEVwH5kWqunuDZqNg="


@pytest.fixture
def config() -> GatewayConfig:
    return GatewayConfig(platforms={})


def test_single_allowed_group_becomes_the_home_channel(monkeypatch, config) -> None:
    monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", GROUP)
    home = config.get_home_channel(Platform.SIGNAL)
    assert home is not None
    assert home.platform == Platform.SIGNAL
    assert home.chat_id == f"group:{GROUP}"


def test_chat_id_carries_the_group_prefix(monkeypatch, config) -> None:
    """``send()`` parses ``group:<id>`` into a groupId.

    A bare id would be treated as a phone number and silently fail to deliver,
    which is the failure mode this whole rework exists to stop repeating.
    """
    monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", GROUP)
    home = config.get_home_channel(Platform.SIGNAL)
    assert home.chat_id.startswith("group:")
    assert home.chat_id[len("group:") :] == GROUP


def test_surrounding_whitespace_is_tolerated(monkeypatch, config) -> None:
    monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", f"  {GROUP}  ")
    assert config.get_home_channel(Platform.SIGNAL).chat_id == f"group:{GROUP}"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="unset-no-group-is-served"),
        pytest.param("   ", id="blank"),
        pytest.param("*", id="wildcard-is-ambiguous"),
        pytest.param("a-group,b-group", id="several-groups-are-ambiguous"),
        pytest.param("*,a-group", id="wildcard-among-others"),
    ],
)
def test_no_derivation_when_the_target_is_not_singular(monkeypatch, config, raw) -> None:
    """Only an unambiguous single group is derived.

    Everything else keeps returning None so an explicit ``/sethome`` remains
    what decides — guessing a destination for cron output would be worse than
    having none.
    """
    monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", raw)
    assert config.get_home_channel(Platform.SIGNAL) is None


def test_explicit_home_channel_still_wins(monkeypatch) -> None:
    """A configured home channel is never overridden by the derivation."""
    from gateway.config import PlatformConfig

    explicit = HomeChannel(
        platform=Platform.SIGNAL, chat_id="group:chosen", name="chosen"
    )
    config = GatewayConfig(
        platforms={
            Platform.SIGNAL: PlatformConfig(enabled=True, home_channel=explicit)
        }
    )
    monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", GROUP)
    assert config.get_home_channel(Platform.SIGNAL) is explicit


def test_other_platforms_are_untouched(monkeypatch, config) -> None:
    """Discord and Slack keep asking, because for them it is a real choice."""
    monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", GROUP)
    for platform in (Platform.DISCORD, Platform.SLACK):
        assert config.get_home_channel(platform) is None
