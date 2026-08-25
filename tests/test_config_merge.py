"""Config-loading contracts: DEFAULT_CONFIG deep-merges with user YAML,
sections survive partial user files, and the env-var metadata used by the
setup wizard stays well-formed.
"""

from __future__ import annotations

import yaml

from son_of_anton_cli.config import load_config
from son_of_anton_cli.config_defaults import DEFAULT_CONFIG, OPTIONAL_ENV_VARS


def test_default_config_has_expected_sections() -> None:
    for section in (
        "model",
        "router",
        "physics",
        "terminal",
        "agent",
        "compression",
        "memory",
        "cron",
        "gateway",
        "security",
        "display",
    ):
        assert section in DEFAULT_CONFIG, f"missing default section: {section}"


def test_user_yaml_deep_merges_over_defaults(son_of_anton_home, monkeypatch) -> None:
    from son_of_anton_cli.config import save_config

    user = {
        "terminal": {"backend": "ssh"},
        "agent": {"max_turns": 42},
    }
    save_config(user)

    cfg = load_config()
    assert cfg["terminal"]["backend"] == "ssh"
    assert cfg["agent"]["max_turns"] == 42
    # Sibling keys from DEFAULT_CONFIG survive the deep merge.
    assert "cwd" in cfg["terminal"]
    assert cfg["terminal"]["cwd"] == DEFAULT_CONFIG["terminal"]["cwd"]


def test_user_yaml_cannot_remove_default_sections(son_of_anton_home) -> None:
    from son_of_anton_cli.config import save_config

    # A user file that only touches one section must not erase the others.
    save_config({"display": {"skin": "mono"}})
    cfg = load_config()
    assert "gateway" in cfg
    assert "cron" in cfg


def test_optional_env_vars_metadata_is_well_formed() -> None:
    assert OPTIONAL_ENV_VARS, "setup wizard needs env var metadata"
    for name, info in OPTIONAL_ENV_VARS.items():
        assert info.get("description"), f"{name}: missing description"
        assert info.get("prompt"), f"{name}: missing prompt"
        assert "category" in info, f"{name}: missing category"


def test_optional_env_vars_do_not_carry_removed_providers() -> None:
    # The provider prune must stay consistent: no removed provider keys in
    # the wizard's env prompt list.
    removed = {
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "KIMI_API_KEY",
        "MINIMAX_API_KEY",
        "NVIDIA_API_KEY",
        "OLLAMA_API_KEY",
        "TOOL_GATEWAY_USER_TOKEN",
        "XAI_API_KEY",
        "FAL_KEY",
        "BROWSERBASE_API_KEY",
    }
    assert not (removed & set(OPTIONAL_ENV_VARS))


def test_config_version_is_an_int() -> None:
    assert isinstance(DEFAULT_CONFIG.get("_config_version"), int)


def test_terminal_cwd_not_bridged_for_local_backend(son_of_anton_home) -> None:
    # The local backend's contract is the process's launch directory: a
    # configured terminal.cwd (a gateway/daemon setting) must not redirect
    # interactive sessions or their children.
    from son_of_anton_cli.config import apply_terminal_config_to_env, save_config

    save_config({"terminal": {"cwd": "/somewhere/else"}})
    env: dict = {}
    apply_terminal_config_to_env(env=env)
    assert "TERMINAL_CWD" not in env


def test_terminal_cwd_bridged_for_remote_backend(son_of_anton_home) -> None:
    from son_of_anton_cli.config import apply_terminal_config_to_env, save_config

    save_config({"terminal": {"backend": "ssh", "cwd": "/remote/workspace"}})
    env: dict = {}
    apply_terminal_config_to_env(env=env)
    assert env["TERMINAL_CWD"] == "/remote/workspace"
