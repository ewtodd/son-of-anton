"""Guards for physics/research workspace isolation.

``WorkspaceManager.init()`` runs ``git init`` + ``git add -A`` + ``git commit``
inside ``Config.workspace_dir``. That field defaults to ``""`` -> ``Path(".")``
-> **the process working directory**, and the research-mode entry points used
to construct ``PhysicsIntern(message)`` with no config at all.

Under the gateway the cwd is the profile's HOME, so a routed message ("derive
the ...") committed the user's whole home directory — SSH keys included — into
a brand-new git repository. Under the CLI it committed the user's project.

These tests pin the isolation and the defense-in-depth guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _physics_intern_call_args(body: str) -> list[str]:
    """Return the argument text of each ``PhysicsIntern(...)`` call in *body*.

    Scans with balanced parentheses — a naive ``[^)]*`` regex stops at the
    inner ``)`` of arguments like ``message.strip()``.
    """
    calls = []
    for match in re.finditer(r"PhysicsIntern\(", body):
        depth, i = 0, match.end() - 1
        while i < len(body):
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
                if depth == 0:
                    calls.append(body[match.end() : i])
                    break
            i += 1
    return calls


def _config(workspace_dir):
    from physics_intern.core.config import build_config

    cfg = build_config(None)
    cfg.workspace_dir = str(workspace_dir)
    return cfg


def test_relative_workspace_is_refused(tmp_path, monkeypatch) -> None:
    """A relative workspace root (the old default) must be refused."""
    from physics_intern.core.workspace import WorkspaceManager

    monkeypatch.chdir(tmp_path)
    ws = WorkspaceManager(_config(""))  # Path("") -> Path(".")
    with pytest.raises(ValueError, match="relative path"):
        ws.init("derive the decay constant")

    assert not (tmp_path / ".git").exists()
    assert not (tmp_path / "RESEARCH_STATE.md").exists()


def test_existing_git_repo_is_refused(tmp_path) -> None:
    """A workspace must never be initialized inside an existing repository."""
    from physics_intern.core.workspace import WorkspaceManager

    repo = tmp_path / "someproject"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="existing git repository"):
        WorkspaceManager(_config(repo)).init("derive the thing")


def test_home_directory_is_refused(tmp_path, monkeypatch) -> None:
    """A workspace must never be initialized directly in $HOME."""
    from physics_intern.core.workspace import WorkspaceManager

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    with pytest.raises(ValueError, match="directly in"):
        WorkspaceManager(_config(fake_home)).init("derive the thing")


def test_resolver_returns_fresh_absolute_root(tmp_path, monkeypatch) -> None:
    """``resolve_workspace_root`` yields a new absolute dir under the home."""
    from physics_intern.core.workspace import resolve_workspace_root

    monkeypatch.setenv("SON_OF_ANTON_HOME", str(tmp_path / "soa"))
    monkeypatch.chdir(tmp_path)

    first = resolve_workspace_root("session", "some-model", "research")
    second = resolve_workspace_root("session", "some-model", "research")

    assert first.is_absolute() and second.is_absolute()
    assert first != second, "two runs must not share a workspace root"
    for root in (first, second):
        assert root.is_dir()
        assert str(root).startswith(str((tmp_path / "soa").resolve()))

    # A workspace from the resolver is accepted by the guard.
    from physics_intern.core.workspace import WorkspaceManager

    WorkspaceManager(_config(first))._assert_safe_workspace_root()


def test_resolver_honours_configured_workspace_root(tmp_path, monkeypatch) -> None:
    """``physics.workspace_root`` from config.yaml overrides the default base."""
    import physics_intern.core.workspace as ws_mod

    custom = tmp_path / "scratch" / "physics"
    monkeypatch.setattr(
        "son_of_anton_cli.config.load_config",
        lambda *a, **k: {"physics": {"workspace_root": str(custom)}},
    )
    root = ws_mod.resolve_workspace_root("session", "m", "autophysicist")
    assert str(root).startswith(str(custom.resolve()))


def test_the_research_runner_pins_a_workspace() -> None:
    """PhysicsIntern may never be constructed without a config.

    ``PhysicsIntern(text)`` with no ``config=`` inherits workspace_dir="" and
    lands in the process cwd — where ``init()`` runs ``git init && git add -A``.
    Under the gateway that cwd is the profile's home directory.

    This used to be asserted at each of the entry points; they now all go
    through ``physics_intern.run.run_problem``, so this is the one construction
    left to guard.
    """
    source = (REPO_ROOT / "physics_intern" / "run.py").read_text(encoding="utf-8")

    assert "resolve_workspace_root(" in source, (
        "physics_intern/run.py does not pin an explicit workspace root"
    )
    constructions = _physics_intern_call_args(source)
    assert constructions, "no PhysicsIntern construction found in physics_intern/run.py"
    for args in constructions:
        assert "config=" in args, (
            f"run.py constructs PhysicsIntern({args}) without config= "
            f"— workspace_dir would default to the process cwd"
        )


@pytest.mark.parametrize(
    "path, symbol",
    [
        ("cli.py", "_run_problem_mode"),
        ("gateway/run.py", "_run_physics_mode_sync"),
    ],
)
def test_entry_points_do_not_construct_their_own_runs(path, symbol) -> None:
    """An entry point that builds its own run is a copy that can drift.

    Research mode already lost the problem spec that way, so its runs were
    never scored.
    """
    source = (REPO_ROOT / path).read_text(encoding="utf-8")
    start = source.index(f"def {symbol}")
    body = source[start : start + 2500]

    assert not _physics_intern_call_args(body), (
        f"{path}:{symbol} constructs PhysicsIntern directly instead of calling "
        f"physics_intern.run.run_problem"
    )
    assert "run_autophysicist(" not in body, (
        f"{path}:{symbol} calls run_autophysicist directly instead of calling "
        f"physics_intern.run.run_problem"
    )


def test_autophysicist_default_workspace_is_not_relative() -> None:
    """The autophysicist default workspace must not be a relative path.

    It used to be ``Path("workspaces/<stamp>_...")``, created under whatever
    cwd the gateway or CLI happened to have.
    """
    source = (REPO_ROOT / "physics_intern" / "autophysicist" / "runner.py").read_text(
        encoding="utf-8"
    )
    assert 'Path(\n            f"workspaces/' not in source
    assert 'f"workspaces/' not in source, (
        "autophysicist still builds a relative workspaces/ path"
    )
    assert "resolve_workspace_root(" in source
