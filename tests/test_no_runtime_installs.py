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
    "son_of_anton_cli.managed_uv",
    "son_of_anton_cli.npm_engine",
    "agent.lsp.install",
    # Installation is declarative only: no setup wizard, no MCP catalog.
    "son_of_anton_cli.setup",
    "son_of_anton_cli.mcp_catalog",
    "son_of_anton_cli.mcp_picker",
)


@pytest.mark.parametrize("action", ["add", "install", "catalog", "picker", "remove", "configure"])
def test_mcp_servers_cannot_be_added_imperatively(action, monkeypatch) -> None:
    """MCP servers are declared in config.toml; the CLI only inspects them."""
    from son_of_anton_cli.main import main

    monkeypatch.setattr(sys, "argv", ["son-of-anton", "mcp", action, "example"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2, f"`mcp {action}` was accepted"


def test_no_private_node_toolchain_is_provisioned() -> None:
    """Node comes from the system like every other tool."""
    import son_of_anton_constants

    for name in ("bootstrap_son_of_anton_managed_node", "heal_son_of_anton_managed_node",
                 "find_node_executable"):
        assert not hasattr(son_of_anton_constants, name), f"{name} is back"


def test_bitwarden_takes_bws_from_path_only(tmp_path, monkeypatch) -> None:
    """With bws absent, the source reports it and downloads nothing."""
    from agent.secret_sources import bitwarden

    monkeypatch.setenv("PATH", str(tmp_path))
    assert bitwarden.find_bws() is None
    assert not hasattr(bitwarden, "install_bws")
    assert not any(tmp_path.iterdir()), "something was written while looking for bws"


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
