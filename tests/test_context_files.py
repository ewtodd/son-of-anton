"""Context-file scan contract — the git-root discovery must degrade
gracefully when the walk crosses an unreadable directory (e.g. another
user's 0700 home) instead of crashing the whole turn.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent.prompt_builder import _find_git_root


def test_find_git_root_ignores_unreadable_parent(tmp_path: Path) -> None:
    parent = tmp_path / "locked"
    child = parent / "work"
    child.mkdir(parents=True)
    os.chmod(parent, 0o000)
    try:
        # Walking up from `child` must not raise on the 000-mode parent.
        assert _find_git_root(child) is None
    finally:
        os.chmod(parent, 0o700)


def test_find_git_root_finds_real_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert _find_git_root(repo) == repo.resolve()
    assert _find_git_root(repo / "sub") == repo.resolve()
