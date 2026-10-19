"""Bare ``-c`` is cwd-scoped: no cross-workspace resume, fresh on a miss."""

from __future__ import annotations

import argparse
import os
import subprocess

from son_of_anton_state import SessionDB, detect_git_metadata
from son_of_anton_cli.main import (
    _resolve_continue_arg,
    _resolve_last_session,
    _resolve_workspace_session,
)


def _make_session(session_id: str, cwd: str) -> None:
    db = SessionDB()
    db.create_session(session_id=session_id, source="cli", cwd=cwd)
    db.close()


def test_workspace_lookup_returns_only_the_current_directory(tmp_path, monkeypatch) -> None:
    other = tmp_path / "other-project"
    other.mkdir()
    here = tmp_path / "here"
    here.mkdir()
    _make_session("20261001_000001_aaaaaa", str(other))
    _make_session("20261001_000002_bbbbbb", str(here))

    monkeypatch.chdir(other)
    assert _resolve_workspace_session(source="cli") == "20261001_000001_aaaaaa"

    monkeypatch.chdir(here)
    assert _resolve_workspace_session(source="cli") == "20261001_000002_bbbbbb"


def test_workspace_lookup_misses_in_a_fresh_directory(tmp_path, monkeypatch) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    assert _resolve_workspace_session(source="cli") is None
    # The old global fallback still exists for explicit ``--resume latest``.
    assert _resolve_last_session(source="cli") is None


def test_bare_continue_leaves_a_fresh_workspace_unresolved(tmp_path, monkeypatch, capsys) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    args = argparse.Namespace(continue_last=True, resume=None, create_if_missing=False)

    _resolve_continue_arg(args)

    assert args.resume is None
    assert "no previous session in this workspace" in capsys.readouterr().err


def test_bare_continue_resolves_the_workspace_session(tmp_path, monkeypatch) -> None:
    here = tmp_path / "here"
    here.mkdir()
    _make_session("20261001_000003_cccccc", str(here))
    monkeypatch.chdir(here)
    args = argparse.Namespace(continue_last=True, resume=None, create_if_missing=False)

    _resolve_continue_arg(args)

    assert args.resume == "20261001_000003_cccccc"


def test_detect_git_metadata_none_outside_a_repo(tmp_path) -> None:
    assert detect_git_metadata(str(tmp_path)) == (None, None)


def test_detect_git_metadata_finds_a_new_repo(tmp_path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    branch, repo_root = detect_git_metadata(str(tmp_path))
    assert repo_root == os.path.abspath(str(tmp_path))
    # Unborn HEAD has no branch name yet.
    assert branch is None


def test_workspace_lookup_also_sees_oneshot_sessions(tmp_path, monkeypatch) -> None:
    here = tmp_path / "here"
    here.mkdir()
    db = SessionDB()
    db.create_session(session_id="20261001_000004_dddddd", source="oneshot", cwd=str(here))
    db.close()
    monkeypatch.chdir(here)

    # A CLI "latest"/-c lookup covers one-shot runs, which are local sessions.
    assert _resolve_workspace_session(source="cli") == "20261001_000004_dddddd"
    assert _resolve_last_session(source="cli") == "20261001_000004_dddddd"
    # ...but a non-cli lookup does not absorb them.
    assert _resolve_last_session(source="tui") is None


def test_oneshot_sessions_record_their_launch_cwd(monkeypatch) -> None:
    from run_agent import _launch_cwd_for_session

    monkeypatch.chdir("/tmp")
    assert _launch_cwd_for_session("oneshot") == "/tmp"
    assert _launch_cwd_for_session("cli") == "/tmp"
    assert _launch_cwd_for_session("signal") is None
    assert _launch_cwd_for_session("cron") is None


def test_oneshot_source_comes_from_the_session_env(monkeypatch) -> None:
    from run_agent import _session_source_for_agent

    monkeypatch.setenv("SON_OF_ANTON_SESSION_SOURCE", "oneshot")
    assert _session_source_for_agent("cli") == "oneshot"
    monkeypatch.delenv("SON_OF_ANTON_SESSION_SOURCE")
    assert _session_source_for_agent("cli") == "cli"
