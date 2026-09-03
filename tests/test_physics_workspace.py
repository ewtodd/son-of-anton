"""Guards for physics workspace isolation.

``run_autophysicist`` runs ``git init`` + ``git add -A`` + ``git commit`` in
its workspace root. Pointed at a directory it does not own, that commits the
user's whole home (SSH keys included) into a brand-new repository. The runner
used to build a relative ``workspaces/...`` path resolved against the process
cwd — the profile home under the gateway, the user's project under the CLI —
so the guard below refuses relative roots, existing repositories, and home
directories for fresh runs, and the resolver below is the only default path.

These tests pin the isolation and the defense-in-depth guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_autophysicist_calls(body: str) -> list[str]:
    """Return the argument text of each ``run_autophysicist(...)`` call."""
    calls = []
    for match in re.finditer(r"run_autophysicist\(", body):
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


# --- the guard itself --------------------------------------------------------


def test_relative_workspace_is_refused(tmp_path, monkeypatch) -> None:
    """A relative workspace root (the old default) must be refused."""
    from physics_intern.core.workspace import assert_safe_workspace_root

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="relative path"):
        assert_safe_workspace_root(Path("workspaces/abc"))

    # And a git repo must not have been initialised in the process cwd.
    assert not (tmp_path / ".git").exists()


def test_existing_git_repo_is_refused(tmp_path) -> None:
    """A fresh workspace must never be initialised inside an existing repo."""
    from physics_intern.core.workspace import assert_safe_workspace_root

    repo = tmp_path / "someproject"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="existing git repository"):
        assert_safe_workspace_root(repo)
    # A resume into the same directory is legitimate — the .git IS the run.
    assert_safe_workspace_root(repo, must_be_fresh=False)


def test_home_directory_is_refused(tmp_path, monkeypatch) -> None:
    """A workspace must never be initialised directly in $HOME."""
    from physics_intern.core.workspace import assert_safe_workspace_root

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    with pytest.raises(ValueError, match="directly in"):
        assert_safe_workspace_root(fake_home)


# --- the resolver -------------------------------------------------------------


def test_resolver_returns_fresh_absolute_root(tmp_path, monkeypatch) -> None:
    """``resolve_workspace_root`` yields a new absolute dir under the home."""
    from physics_intern.core.workspace import assert_safe_workspace_root, resolve_workspace_root

    monkeypatch.setenv("SON_OF_ANTON_HOME", str(tmp_path / "soa"))
    monkeypatch.chdir(tmp_path)

    first = resolve_workspace_root("session", "some-model", "autophysicist")
    second = resolve_workspace_root("session", "some-model", "autophysicist")

    assert first.is_absolute() and second.is_absolute()
    assert first != second, "two runs must not share a workspace root"
    for root in (first, second):
        assert root.is_dir()
        assert str(root).startswith(str((tmp_path / "soa").resolve()))

    # A workspace from the resolver is accepted by the guard.
    assert_safe_workspace_root(first)


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


# --- a fresh run actually writes its spec, and the guard applies -------------


def test_a_fresh_run_writes_the_spec_into_the_workspace(monkeypatch, tmp_path) -> None:
    """The evaluator reads problem.yaml out of the workspace.

    A run that never puts one there is silently never scored — it finishes,
    prints an answer, and reports "Formal verification skipped".
    """
    import physics_intern.run as run_module

    seen: dict = {}

    def fake_autophysicist(**kwargs):
        seen.update(kwargs)
        return tmp_path

    monkeypatch.setattr(
        "physics_intern.autophysicist.runner.run_autophysicist", fake_autophysicist
    )
    monkeypatch.setattr("physics_intern.run._physics_config", dict)

    spec = {
        "name": "decay_curve",
        "problem": "Measure the half-life.",
        "checks": [
            {"id": "halflife", "key": "halflife_s", "expected": 119.2, "tolerance": 4.0}
        ],
    }
    spec_path = tmp_path / "problem.yaml"
    spec_path.write_text(yaml.dump(spec), encoding="utf-8")

    run_module.run_problem(str(spec_path), mode="physics", workspace_root=tmp_path / "ws")

    assert seen["problem_def"] == spec
    assert seen["problem_name"] == "decay_curve"
    assert seen["workspace_root"] == (tmp_path / "ws").resolve()


def test_the_runner_applies_the_workspace_guard() -> None:
    """An explicit workspace_root goes through assert_safe_workspace_root.

    The guard is the thing that keeps ``--workspace ~`` from committing the
    user's home; the runner used to ``mkdir`` and ``git init`` unconditionally.
    """
    source = (REPO_ROOT / "physics_intern" / "autophysicist" / "runner.py").read_text(
        encoding="utf-8"
    )
    assert "assert_safe_workspace_root(" in source, (
        "the runner must check an explicit workspace root against the guard"
    )
    assert "resolve_workspace_root(" in source


# --- entry points must not build their own runs ------------------------------


@pytest.mark.parametrize(
    "path, symbol",
    [
        ("son_of_anton_cli/main.py", "cmd_problem"),
    ],
)
def test_entry_points_do_not_construct_their_own_runs(path, symbol) -> None:
    """An entry point that builds its own run is a copy that can drift.

    The modes drifted apart that way before — once badly enough that a mode's
    runs were never scored at all.
    """
    source = (REPO_ROOT / path).read_text(encoding="utf-8")
    start = source.index(f"def {symbol}")
    body = source[start : start + 2500]

    assert "run_problem(" in body, (
        f"{path}:{symbol} must go through physics_intern.run.run_problem"
    )
    assert not _run_autophysicist_calls(body), (
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
