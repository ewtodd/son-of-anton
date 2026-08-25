"""Contract tests for the TERMINAL_CWD ownership rule.

Two processes disagree about what ``TERMINAL_CWD`` means and share one env var:

- the **CLI** owns the *launch directory* (where the user typed the command);
- the **gateway** owns the configured ``terminal.cwd`` from config.yaml.

``gateway/run.py`` bridges config.yaml -> env at import time. The CLI agent
lazy-imports that module on its first turn (``agent/relay_runtime
._segments_config``), so the bridge used to fire inside the CLI and silently
replace the launch dir with the gateway's configured cwd ~2s after startup —
the banner showed the spawn dir while ``pwd`` and the system prompt showed
``terminal.cwd``.

Gating the bridge on ``_SON_OF_ANTON_GATEWAY`` did NOT fix it: that marker is
set by ``gateway/run.py`` itself at import (line ~1918), so it is always "1"
by the time the bridge reads it. The real gate is
``_SON_OF_ANTON_GATEWAY_PROC``, set by the gateway *launchers* before they
import the module.

These tests run in subprocesses because the bug is import-time and one-shot.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIGURED_CWD = "/gateway-configured-cwd"


def _write_config(home: Path, cwd_value: str = CONFIGURED_CWD) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f"terminal:\n  backend: local\n  cwd: {cwd_value}\n",
        encoding="utf-8",
    )


def _run(home: Path, body: str, extra_env: dict | None = None) -> str:
    """Import gateway.run in a clean subprocess and report TERMINAL_CWD."""
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SON_OF_ANTON_HOME": str(home),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"subprocess failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout.strip().splitlines()[-1]


def test_cli_lazy_import_preserves_launch_dir(tmp_path) -> None:
    """The CLI's launch-dir contract survives a lazy ``gateway.run`` import.

    This is the exact user-visible bug: an agent turn late-imports gateway.run
    and the working directory changes underneath the session.
    """
    home = tmp_path / "home"
    _write_config(home)
    launch_dir = str(tmp_path / "launch")

    result = _run(
        home,
        f"""
        import os
        os.environ["TERMINAL_CWD"] = {launch_dir!r}
        os.environ["TERMINAL_ENV"] = "local"
        from gateway.run import _load_gateway_config  # the relay_runtime path
        print(os.environ.get("TERMINAL_CWD"))
        """,
    )
    assert result == launch_dir, (
        "importing gateway.run from the CLI overwrote the launch-dir contract"
    )


def test_gateway_process_applies_configured_cwd(tmp_path) -> None:
    """A real gateway still gets ``terminal.cwd`` bridged into TERMINAL_CWD."""
    home = tmp_path / "home"
    _write_config(home)

    result = _run(
        home,
        """
        import os
        os.environ["TERMINAL_CWD"] = "/some/stale/value"
        import gateway.run  # noqa: F401
        print(os.environ.get("TERMINAL_CWD"))
        """,
        extra_env={"_SON_OF_ANTON_GATEWAY_PROC": "1"},
    )
    assert result == CONFIGURED_CWD


def test_gateway_resolves_placeholder_cwd(tmp_path) -> None:
    """Placeholder ``terminal.cwd`` still resolves for the local backend.

    Regression guard: an earlier refactor deleted this resolution block
    outright, leaving TERMINAL_CWD unset in the gateway.
    """
    home = tmp_path / "home"
    _write_config(home, cwd_value="auto")

    result = _run(
        home,
        """
        import os
        import gateway.run  # noqa: F401
        print(os.environ.get("TERMINAL_CWD"))
        """,
        extra_env={"_SON_OF_ANTON_GATEWAY_PROC": "1"},
    )
    assert result == str(home), "placeholder cwd was not resolved to the home fallback"


def test_gateway_gate_does_not_key_off_the_inherited_marker() -> None:
    """``_IS_GATEWAY_PROCESS`` must not be derived from ``_SON_OF_ANTON_GATEWAY``.

    That marker is exported into the environment and inherited by children, so
    deriving the gate from it is circular. Pins the invariant that made the
    first TERMINAL_CWD fix a no-op.
    """
    source = (REPO_ROOT / "gateway" / "run.py").read_text(encoding="utf-8")
    gate = source.index("_IS_GATEWAY_PROCESS = (")
    gate_expr = source[gate : source.index(")", gate)]
    assert "_SON_OF_ANTON_GATEWAY_PROC" in gate_expr
    assert '"_SON_OF_ANTON_GATEWAY"' not in gate_expr, (
        "the gate must not key off the exported/inherited marker"
    )


def test_marker_is_set_only_by_a_real_gateway() -> None:
    """``_SON_OF_ANTON_GATEWAY`` must be assigned under the gate, not at import.

    Consumers read it to mean "I am inside the gateway process tree":
    cli.py's TERMINAL_CWD bridge, terminal_tool's lifecycle hard-block, and
    ``son-of-anton gateway stop|restart``'s self-target refusal. Setting it
    unconditionally made all of them fire in ordinary CLI sessions.
    """
    source = (REPO_ROOT / "gateway" / "run.py").read_text(encoding="utf-8")
    assignment = 'os.environ["_SON_OF_ANTON_GATEWAY"] = "1"'
    assert source.count(assignment) == 1, "expected exactly one marker assignment"

    gate = source.index("_IS_GATEWAY_PROCESS = (")
    idx = source.index(assignment)
    assert gate < idx, "the gate must be defined before the marker is set"

    # The assignment must be indented, i.e. nested under `if _IS_GATEWAY_PROCESS:`
    line_start = source.rindex("\n", 0, idx) + 1
    assert source[line_start:idx].strip() == "", "sanity: assignment starts its line"
    assert source[line_start] in " \t", (
        "the marker assignment is at column 0 — it runs on every import"
    )
    guard = source.rindex("if _IS_GATEWAY_PROCESS:", gate, idx)
    assert guard < idx, "marker assignment is not under an _IS_GATEWAY_PROCESS guard"


def test_incidental_import_does_not_mark_the_process(tmp_path) -> None:
    """Importing gateway.run from a CLI must not set the gateway marker."""
    home = tmp_path / "home"
    _write_config(home)
    result = _run(
        home,
        """
        import os
        from gateway.run import _load_gateway_config  # noqa: F401
        print(os.environ.get("_SON_OF_ANTON_GATEWAY"))
        """,
    )
    assert result == "None", (
        "an incidental import marked the process as a gateway — CLI sessions "
        "would skip the TERMINAL_CWD export and refuse gateway lifecycle commands"
    )


def test_real_gateway_process_sets_the_marker(tmp_path) -> None:
    """A genuine gateway still exports the marker for its children."""
    home = tmp_path / "home"
    _write_config(home)
    result = _run(
        home,
        """
        import os
        import gateway.run  # noqa: F401
        print(os.environ.get("_SON_OF_ANTON_GATEWAY"))
        """,
        extra_env={"_SON_OF_ANTON_GATEWAY_PROC": "1"},
    )
    assert result == "1"


def test_restart_watcher_sheds_both_markers() -> None:
    """The detached restart watcher must not look like a gateway.

    It runs ``son-of-anton gateway restart``, which refuses to self-target when
    the marker is present — silently, leaving the gateway down.
    """
    source = (REPO_ROOT / "gateway" / "run.py").read_text(encoding="utf-8")
    idx = source.index('watcher_env.pop("_SON_OF_ANTON_GATEWAY", None)')
    window = source[idx : idx + 400]
    assert 'watcher_env.pop("_SON_OF_ANTON_GATEWAY_PROC", None)' in window, (
        "the watcher sheds the inherited marker but keeps the launcher marker, "
        "so it would re-mark itself on importing gateway.run"
    )


@pytest.mark.parametrize(
    "launcher, anchor",
    [
        ("cli.py", "from gateway.run import start_gateway"),
        ("son_of_anton_cli/gateway.py", "from gateway.run import start_gateway"),
    ],
)
def test_launchers_mark_the_process_before_importing(launcher, anchor) -> None:
    """Every gateway launcher sets the marker BEFORE importing gateway.run."""
    source = (REPO_ROOT / launcher).read_text(encoding="utf-8")
    idx_import = source.index(anchor)
    idx_marker = source.index('os.environ["_SON_OF_ANTON_GATEWAY_PROC"] = "1"')
    assert idx_marker < idx_import, (
        f"{launcher} imports gateway.run before marking the process as a gateway"
    )
