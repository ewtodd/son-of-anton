"""``python -m autophysicist problem``: the physics mode's own entry.

The Go ``son-of-anton problem`` hands the physics mode to this entry until its
Go port (daemon/phases/10a-son-of-anton-command.md).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _run(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "autophysicist", *args],
                          capture_output=True, text=True, env=env, timeout=120, cwd=REPO)


def test_problem_lists_its_actions() -> None:
    r = _run("problem", "--help")
    assert r.returncode == 0, r.stderr
    assert "create" in r.stdout and "run" in r.stdout


def test_problem_run_takes_the_run_options() -> None:
    r = _run("problem", "run", "--help")
    assert r.returncode == 0, r.stderr
    for opt in ("--max-iterations", "--script-timeout", "--token-budget", "--workspace"):
        assert opt in r.stdout


def test_problem_without_an_action_is_a_usage_error() -> None:
    r = _run("problem")
    assert r.returncode == 2
    assert "son-of-anton problem run SPEC" in r.stdout + r.stderr


def test_the_homes_env_is_loaded_for_the_model_key(tmp_path) -> None:
    # The physics LLM layer reads its key from the environment; the entry must
    # load <home>/.env as the Python command did.
    (tmp_path / ".env").write_text("PHYSICS_ENTRY_PROBE_KEY=from-the-home\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env["SON_OF_ANTON_HOME"] = str(tmp_path)
    env.pop("PHYSICS_ENTRY_PROBE_KEY", None)
    code = (
        "import os, sys, autophysicist.__main__ as m\n"
        "sys.argv = ['autophysicist', 'problem']\n"
        "try:\n    m.main(['problem'])\nexcept SystemExit:\n    pass\n"
        "print('KEY=' + os.environ.get('PHYSICS_ENTRY_PROBE_KEY', ''))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120, cwd=REPO)
    assert "KEY=from-the-home" in r.stdout, (r.stdout, r.stderr)
