"""Skin-engine contracts — skins are pure data; built-ins always exist and
any unknown or partially-specified skin falls back to the default skin's
values instead of raising.
"""

from __future__ import annotations

from son_of_anton_cli.skin_engine import (
    _BUILTIN_SKINS,
    list_skins,
    load_skin,
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
