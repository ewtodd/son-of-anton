"""Contract tests for the dotenv loader.

Covers the two failure modes found by the live deployment:
- An unreadable ``$SON_OF_ANTON_HOME/.env`` (stale/cross-user HOME) must
  fail open instead of crashing startup with a raw PermissionError.
- A dotenv reload must never clobber the ``TERMINAL_*`` runtime contract
  that the launcher bridged from config.yaml.
"""

import os
import stat

import pytest


def test_unreadable_dotenv_fails_open(tmp_path) -> None:
    """A .env under an untraversable home is treated as absent, not a crash.

    Repro: ``su e-play`` from e-work's shell carries
    ``SON_OF_ANTON_HOME=/home/e-work/.son-of-anton``; the other account's
    0700 home makes ``Path.exists()`` raise PermissionError, which used to
    kill the CLI before it could print anything.
    """
    from son_of_anton_cli.env_loader import load_son_of_anton_dotenv

    env_dir = tmp_path / "other-user-home"
    env_dir.mkdir()
    (env_dir / ".env").write_text("OPENAI_API_KEY=stale\n", encoding="utf-8")

    # Drop all access to the home directory itself (0700 on the file is not
    # enough: the parent must be untraversable to reproduce the su cross-user
    # case). This is skipped for root, which bypasses permission checks.
    os.chmod(env_dir, 0o000)
    try:
        with pytest.raises(PermissionError):
            (env_dir / ".env").exists()
        loaded = load_son_of_anton_dotenv(son_of_anton_home=env_dir)
        assert loaded == []
    finally:
        os.chmod(env_dir, stat.S_IRWXU)


def test_dotenv_reload_does_not_clobber_terminal_contract(tmp_path, monkeypatch) -> None:
    """A later dotenv reload must not overwrite TERMINAL_* already bridged.

    The CLI bridges TERMINAL_ENV/TERMINAL_CWD from config.yaml (local
    backend contract = the launch directory). A stale .env left by an older
    setup (TERMINAL_ENV=docker/ssh, a configured terminal.cwd) must lose to
    that bridge on every reload (run_agent import, gateway per-turn reload,
    MCP reload), otherwise the agent suddenly works in the wrong directory.
    """
    from son_of_anton_cli.env_loader import _load_dotenv_with_fallback

    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "TERMINAL_ENV=ssh\n"
        "TERMINAL_CWD=/somewhere/else\n"
        "OPENAI_API_KEY=from-dotenv\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", "/launch-dir")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    _load_dotenv_with_fallback(dotenv_path, override=True)

    # The launcher's contract survives; the credential key still loads.
    assert os.environ["TERMINAL_ENV"] == "local"
    assert os.environ["TERMINAL_CWD"] == "/launch-dir"
    assert os.environ["OPENAI_API_KEY"] == "from-dotenv"


def test_dotenv_first_load_still_seeds_terminal_values(tmp_path, monkeypatch) -> None:
    """First load (no launcher claim yet) keeps the legacy .env behaviour.

    A config-less install that keeps TERMINAL_ENV=docker in .env must still
    get that value on the first load, not silently fall back to local.
    """
    from son_of_anton_cli.env_loader import _load_dotenv_with_fallback

    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("TERMINAL_ENV=docker\n", encoding="utf-8")
    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    _load_dotenv_with_fallback(dotenv_path, override=True)

    assert os.environ["TERMINAL_ENV"] == "docker"
