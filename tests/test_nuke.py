"""``son-of-anton nuke`` — full local reset behind a typed confirmation."""

from __future__ import annotations

import argparse
import io

from son_of_anton_cli.subcommands import nuke as nuke_mod
from son_of_anton_constants import get_son_of_anton_home


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _seed_home():
    home = get_son_of_anton_home()
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "sessions" / "one.json").write_text("x", encoding="utf-8")
    (home / "config.toml").write_text("model = 1", encoding="utf-8")
    return home


def test_nuke_requires_the_exact_phrase(monkeypatch, capsys) -> None:
    home = _seed_home()
    monkeypatch.setattr(nuke_mod.sys, "stdin", _FakeTTY("nope\n"))
    assert nuke_mod.cmd_nuke(argparse.Namespace()) == 1
    assert home.exists(), "a wrong phrase must not delete anything"
    assert "did not match" in capsys.readouterr().out


def test_nuke_deletes_the_whole_home_on_the_phrase(monkeypatch, capsys) -> None:
    home = _seed_home()
    monkeypatch.setattr(
        nuke_mod.sys, "stdin", _FakeTTY(nuke_mod.CONFIRM_PHRASE + "\n")
    )
    assert nuke_mod.cmd_nuke(argparse.Namespace()) == 0
    assert not home.exists(), "the whole home should be gone"
    assert "Deleted" in capsys.readouterr().out


def test_nuke_refuses_without_an_interactive_terminal(monkeypatch, capsys) -> None:
    home = _seed_home()
    monkeypatch.setattr(
        nuke_mod.sys, "stdin", io.StringIO(nuke_mod.CONFIRM_PHRASE + "\n")
    )
    assert nuke_mod.cmd_nuke(argparse.Namespace()) == 1
    assert home.exists(), "a piped stdin must not be able to nuke"
    assert "interactive terminal" in capsys.readouterr().out
