"""Skin-engine contracts — skins are pure data; built-ins always exist and
any unknown or partially-specified skin falls back to the default skin's
values instead of raising.
"""

from __future__ import annotations

from son_of_anton_cli.skin_engine import (
    _BUILTIN_SKINS,
    get_prompt_toolkit_style_overrides,
    list_skins,
    load_skin,
    snap_pt_style_to_theme,
)


def test_builtin_skins_exist() -> None:
    # The four documented built-ins must always exist; extra built-ins may
    # come and go without breaking the contract.
    assert {"default", "ares", "mono", "slate"} <= set(_BUILTIN_SKINS)


def test_unknown_skin_falls_back_to_default() -> None:
    skin = load_skin("definitely-not-a-skin")
    default = load_skin("default")
    assert skin is not None
    assert skin.colors == default.colors


def test_list_skins_includes_builtins() -> None:
    names = {entry["name"] for entry in list_skins()}
    assert {"default", "ares", "mono", "slate"} <= names


def test_builtin_skins_are_data_only() -> None:
    # Pure-data rule: skins must not smuggle executable callables.
    import inspect

    for name, data in _BUILTIN_SKINS.items():
        for key, value in data.items():
            assert not callable(value), f"{name}.{key} is callable"
            if isinstance(value, dict):
                assert not any(callable(v) for v in value.values()), f"{name}.{key} nested callable"
        assert not inspect.isfunction(data)


def test_prompt_toolkit_overrides_cover_every_status_badge() -> None:
    # The session-title badge and YOLO flag used to be hardcoded in the CLI's
    # base style, invisible to skins. Every status-bar class must be
    # skin-drivable.
    overrides = get_prompt_toolkit_style_overrides()
    for key in ("status-bar", "status-bar-strong", "status-bar-session-title", "status-bar-yolo"):
        assert key in overrides, f"missing prompt_toolkit override: {key}"


def test_snap_pt_style_to_theme_uses_palette_names() -> None:
    # prompt_toolkit's fixed RGB names and hex tokens bypass the terminal
    # theme; snapping must rewrite both onto the ansi* palette names.
    out = snap_pt_style_to_theme("bg:#1a1a2e bold yellow")
    assert "#" not in out
    assert "yellow" not in out.split()
    assert "ansiyellow" in out.split()
    assert "bg:ansiblack" in out  # #1a1a2e snaps to palette black


def test_snap_pt_style_to_theme_passes_attributes_through() -> None:
    assert snap_pt_style_to_theme("") == ""
    assert snap_pt_style_to_theme("default bold") == "default bold"
    assert "italic" in snap_pt_style_to_theme("ansibrightblack italic")
