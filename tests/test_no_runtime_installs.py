"""Son of Anton never installs Python packages at runtime.

Every dependency comes from the Nix build. The lazy pip installer, the
setup wizards that pip-installed provider SDKs, the platform registry's
installer hook and the startup venv repair that ran pip/uv are gone; a
missing optional package says which dependency group to add to the build.
These fail if any of that machinery comes back.
"""

from __future__ import annotations

import importlib
import sys

import pytest

REMOVED_MODULES = (
    "tools.lazy_deps",
    "son_of_anton_cli._install_repair",
    "son_of_anton_cli._early_recovery",
)


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_runtime_installer_modules_are_gone(module: str) -> None:
    try:
        importlib.import_module(module)
    except ImportError:
        return
    raise AssertionError(f"{module} is importable again — a runtime installer is back")


def test_platforms_cannot_register_an_installer() -> None:
    """The registry only probes; nothing may install a platform's SDK."""
    from gateway.platform_registry import PlatformEntry

    with pytest.raises(TypeError):
        PlatformEntry(
            name="example",
            label="Example",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: False,
            ensure_deps_fn=lambda: True,
        )


def test_missing_optional_sdk_names_the_nix_group(monkeypatch) -> None:
    """A provider whose SDK is not in the build says how to add it, not pip."""
    import tools.web_tools as web_tools
    from plugins.web.exa.provider import _get_exa_client

    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setattr(web_tools, "_exa_client", None, raising=False)
    monkeypatch.setitem(sys.modules, "exa_py", None)

    with pytest.raises(ImportError) as excinfo:
        _get_exa_client()
    message = str(excinfo.value)
    assert "extraDependencyGroups" in message and '"exa"' in message
    assert "pip" not in message
