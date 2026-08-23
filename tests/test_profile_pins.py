"""Per-chat profile-pin contract — the command-driven multiplex switch.
"""

from __future__ import annotations

from pathlib import Path

from gateway.profile_pins import load_pins, pin_key, resolve_pin, save_pins


def test_pin_key_format() -> None:
    assert pin_key("signal", "group-abc") == "signal:group-abc"
    assert pin_key("Signal", "  group-abc  ") == "signal:group-abc"


def test_pins_round_trip_through_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    assert load_pins(home) == {}

    save_pins(home, {"signal:dm:+123": "play", "signal:group:x": "work"})
    loaded = load_pins(home)
    assert loaded == {"signal:dm:+123": "play", "signal:group:x": "work"}


def test_resolve_pin_validates_against_served_profiles() -> None:
    pins = {"signal:dm:+123": "play", "signal:group:x": "gone"}
    served = {"play", "work", "default"}
    assert resolve_pin(pins, "signal", "dm:+123", served) == "play"
    assert resolve_pin(pins, "signal", "group:x", served) is None  # unserved
    assert resolve_pin(pins, "signal", "dm:+999", served) is None  # unpinned
    assert resolve_pin({"signal:a": "default"}, "signal", "a", served) == "default"


def test_load_pins_handles_missing_and_corrupt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    assert load_pins(home) == {}
    (home / "profile_pins.json").write_text("{not json", encoding="utf-8")
    assert load_pins(home) == {}
    (home / "profile_pins.json").write_text('["not", "a", "dict"]', encoding="utf-8")
    assert load_pins(home) == {}
