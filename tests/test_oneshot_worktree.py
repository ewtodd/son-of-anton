"""``son-of-anton -z PROMPT -w`` runs in its own git worktree.

``-w`` used to be dropped on the one-shot path: both ``-z`` dispatches (the
fast chat launch and the full parser) called ``run_oneshot`` without it, so a
"parallel" one-shot agent worked in the shared checkout. Driven here through
the real ``main()`` and the real worktree code against a throwaway repo; only
the model call and the final ``os._exit`` are stubbed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import son_of_anton_cli.main as main_mod
import son_of_anton_cli.oneshot as oneshot_mod


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def run_z(monkeypatch):
    """Run ``main()`` with argv; returns (exit code, what the agent saw)."""
    for var in ("TERMINAL_CWD", "SON_OF_ANTON_YOLO_MODE", "SON_OF_ANTON_SESSION_SOURCE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda args: None)
    monkeypatch.setattr(main_mod, "_cleanup_oneshot_runtime", lambda: None)

    def _exit(rc):
        raise SystemExit(rc)

    monkeypatch.setattr(main_mod, "_exit_after_oneshot", _exit)

    def run(argv, *, leave_file=False, full_parser=False):
        seen = {}

        def fake_agent(prompt, **kwargs):
            seen["cwd"] = os.environ.get("TERMINAL_CWD")
            if leave_file:
                Path(seen["cwd"], "result.txt").write_text("output\n", encoding="utf-8")
            print("agent chatter that must not reach stdout")
            return "done", {}

        monkeypatch.setattr(oneshot_mod, "_run_agent", fake_agent)
        if full_parser:
            monkeypatch.setenv("SON_OF_ANTON_DISABLE_FAST_CHAT_LAUNCH", "1")
        else:
            monkeypatch.delenv("SON_OF_ANTON_DISABLE_FAST_CHAT_LAUNCH", raising=False)
        monkeypatch.setattr(sys, "argv", ["son-of-anton", *argv])
        with pytest.raises(SystemExit) as exc:
            main_mod.main()
        return exc.value.code, seen

    yield run
    logging.disable(logging.NOTSET)


@pytest.mark.parametrize("full_parser", [False, True], ids=["fast-launch", "full-parser"])
def test_dash_w_runs_the_prompt_in_a_fresh_worktree(repo, run_z, capsys, full_parser) -> None:
    rc, seen = run_z(["-z", "hi", "-w"], full_parser=full_parser)
    out, err = capsys.readouterr()

    assert rc == 0
    tree = Path(seen["cwd"])
    assert tree.parent == repo / ".worktrees", "the tools must work in the worktree, not the checkout"
    assert out == "done\n", "stdout carries the final response and nothing else"
    assert str(tree) in err, "the worktree is reported on stderr"
    # A clean run leaves nothing behind: tree and branch both gone.
    assert not tree.exists()
    assert _git(repo, "branch", "--list", "son-of-anton/*").strip() == ""


def test_a_run_that_leaves_changes_keeps_its_worktree(repo, run_z, capsys) -> None:
    rc, seen = run_z(["-z", "hi", "-w"], leave_file=True)
    _out, err = capsys.readouterr()

    tree = Path(seen["cwd"])
    assert rc == 0
    assert (tree / "result.txt").read_text(encoding="utf-8") == "output\n"
    assert "keeping" in err and str(tree) in err


def test_without_a_repository_the_prompt_is_not_run(tmp_path, monkeypatch, run_z, capsys) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    monkeypatch.chdir(outside)
    rc, seen = run_z(["-z", "hi", "-w"])
    out, err = capsys.readouterr()

    assert rc == 2
    assert seen == {}, "isolation was asked for: never fall back to the shared directory"
    assert out == ""
    assert "--worktree" in err


def test_without_dash_w_the_run_stays_in_the_checkout(repo, run_z, capsys) -> None:
    rc, seen = run_z(["-z", "hi"])
    capsys.readouterr()
    assert rc == 0
    assert seen["cwd"] is None
    assert not (repo / ".worktrees").exists()
