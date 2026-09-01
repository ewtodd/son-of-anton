"""The physics modes' execution layer: it runs, and it is actually confined.

The bug these cover: ``execute_python`` shelled out to ``["python", script]``.
The sealed Nix install has no ``python`` on ``PATH`` — the gateway unit's PATH
is the son-of-anton wrapper plus coreutils and git — so every computation a
physics run attempted returned ``EXECUTION ERROR: [Errno 2] No such file or
directory: 'python'``. Nothing caught it, because no test had ever executed a
script; the physics tests all stopped at "the modules import".
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from physics_intern.utils.sandbox import (
    SandboxPolicy,
    execute_python,
    resolve_interpreter,
)

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap not installed"
)


def _policy(tmp_path: Path, **kwargs) -> SandboxPolicy:
    return SandboxPolicy(
        interpreter=sys.executable, workspace=tmp_path, **kwargs
    )


def test_resolve_interpreter_is_an_existing_executable(monkeypatch) -> None:
    monkeypatch.delenv("SON_OF_ANTON_PHYSICS_PYTHON", raising=False)
    monkeypatch.delenv("SON_OF_ANTON_PYTHON", raising=False)
    monkeypatch.setattr("physics_intern.utils.sandbox._physics_config", dict)
    interpreter = resolve_interpreter()
    assert interpreter != "python"
    assert os.path.exists(interpreter), (
        f"resolved interpreter {interpreter!r} does not exist — this is the "
        "shape of the bug where computations died on a bare 'python'"
    )


def test_config_python_wins_over_environment(monkeypatch, tmp_path) -> None:
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("SON_OF_ANTON_PYTHON", sys.executable)
    monkeypatch.setattr(
        "physics_intern.utils.sandbox._physics_config",
        lambda: {"python": str(fake)},
    )
    assert resolve_interpreter() == str(fake)


def test_nonexistent_configured_interpreter_falls_through(monkeypatch) -> None:
    monkeypatch.delenv("SON_OF_ANTON_PHYSICS_PYTHON", raising=False)
    monkeypatch.setenv("SON_OF_ANTON_PYTHON", sys.executable)
    monkeypatch.setattr(
        "physics_intern.utils.sandbox._physics_config",
        lambda: {"python": "/nonexistent/python3"},
    )
    assert resolve_interpreter() == sys.executable


def test_script_actually_runs(tmp_path: Path) -> None:
    script = tmp_path / "compute.py"
    script.write_text("print(6 * 7)\n")
    result = execute_python(
        script, timeout=60, cwd=tmp_path, policy=_policy(tmp_path)
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "42"


def test_workspace_writes_persist(tmp_path: Path) -> None:
    script = tmp_path / "write.py"
    script.write_text("open('RESULTS.txt', 'w').write('answer = 1.5\\n')\n")
    result = execute_python(
        script, timeout=60, cwd=tmp_path, policy=_policy(tmp_path)
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "RESULTS.txt").read_text() == "answer = 1.5\n"


def test_timeout_is_reported(tmp_path: Path) -> None:
    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(30)\n")
    result = execute_python(
        script, timeout=2, cwd=tmp_path, policy=_policy(tmp_path)
    )
    assert result.timed_out
    assert "TIMEOUT" in result.stderr


@needs_bwrap
def test_secrets_are_not_inherited(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-should-not-be-visible")
    script = tmp_path / "env.py"
    script.write_text("import os\nprint(os.environ.get('LITELLM_MASTER_KEY', 'ABSENT'))\n")
    result = execute_python(
        script, timeout=60, cwd=tmp_path, policy=_policy(tmp_path)
    )
    assert result.sandboxed
    assert result.stdout.strip() == "ABSENT"


@needs_bwrap
def test_home_directory_is_not_visible(tmp_path: Path) -> None:
    secret = Path.home() / ".ssh"
    script = tmp_path / "peek.py"
    script.write_text(f"import os\nprint(os.path.exists({str(secret)!r}))\n")
    result = execute_python(
        script, timeout=60, cwd=tmp_path, policy=_policy(tmp_path)
    )
    assert result.sandboxed
    assert result.stdout.strip() == "False"


@needs_bwrap
def test_network_is_unreachable_by_default(tmp_path: Path) -> None:
    script = tmp_path / "net.py"
    script.write_text(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('REACHABLE')\n"
        "except OSError:\n"
        "    print('BLOCKED')\n"
    )
    result = execute_python(
        script, timeout=60, cwd=tmp_path, policy=_policy(tmp_path)
    )
    assert result.stdout.strip() == "BLOCKED"


@needs_bwrap
def test_declared_data_is_readable_but_not_writable(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "input.csv").write_text("a,b\n1,2\n")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    script = workspace / "read.py"
    script.write_text(
        f"print(open({str(data / 'input.csv')!r}).read().strip())\n"
        "try:\n"
        f"    open({str(data / 'clobber')!r}, 'w').write('x')\n"
        "    print('WRITABLE')\n"
        "except OSError:\n"
        "    print('READ-ONLY')\n"
    )
    policy = SandboxPolicy(
        interpreter=sys.executable, workspace=workspace, data_dirs=(data,)
    )
    result = execute_python(script, timeout=60, cwd=workspace, policy=policy)
    assert result.returncode == 0, result.stderr
    assert "a,b\n1,2" in result.stdout
    assert "READ-ONLY" in result.stdout
    assert not (data / "clobber").exists()


@needs_bwrap
def test_undeclared_paths_are_invisible(tmp_path: Path) -> None:
    hidden = tmp_path / "not-declared"
    hidden.mkdir()
    (hidden / "secret.txt").write_text("nope")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    script = workspace / "peek.py"
    script.write_text(f"import os\nprint(os.path.exists({str(hidden / 'secret.txt')!r}))\n")
    result = execute_python(
        script, timeout=60, cwd=workspace, policy=_policy(workspace)
    )
    assert result.stdout.strip() == "False"


def test_sandbox_off_is_opt_in_and_reported(tmp_path: Path) -> None:
    script = tmp_path / "s.py"
    script.write_text("print('unconfined')\n")
    policy = _policy(tmp_path, mode="off")
    result = execute_python(script, timeout=60, cwd=tmp_path, policy=policy)
    assert result.returncode == 0, result.stderr
    assert result.sandboxed is False


def test_missing_bwrap_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """With no bwrap and mode=auto the run must refuse, not silently unconfine."""
    monkeypatch.setattr("physics_intern.utils.sandbox.bwrap_path", lambda: None)
    script = tmp_path / "s.py"
    script.write_text("print('should not run')\n")
    result = execute_python(
        script, timeout=60, cwd=tmp_path, policy=_policy(tmp_path)
    )
    assert result.returncode != 0
    assert "bubblewrap" in result.stderr
    assert "should not run" not in result.stdout


# --- house-library guidance -------------------------------------------------
#
# Telling a model that `analysis_utilities` is importable does not make it use
# the library: it has seen a million numpy/matplotlib scripts and none using
# this one, so it reimplements the waveform features, the TTree loop and the
# plot style by hand — slower, uncached, and not the published figures. The
# guidance block is what closes that gap, so it has to actually reach the two
# places an agent reads: the execute_python schema and the sub-agent's
# code-execution instructions.


def test_notes_only_appear_for_libraries_that_are_present() -> None:
    from physics_intern.utils.runtime_notes import notes_for

    assert notes_for({"numpy": "2.0"}) == ""
    assert "analysis_utilities" in notes_for({"analysis_utilities": "26.8.27"})


def test_extra_notes_are_appended() -> None:
    from physics_intern.utils.runtime_notes import notes_for

    assert "house rule" in notes_for({}, extra="house rule")
    combined = notes_for({"analysis_utilities": "1"}, extra="house rule")
    assert "analysis_utilities" in combined and "house rule" in combined


def test_guidance_reaches_the_execute_python_schema(monkeypatch) -> None:
    from physics_intern.agents.computer.tools import ToolExecutor

    monkeypatch.setattr(
        "physics_intern.utils.sandbox.runtime_summary",
        lambda _i=None: {
            "interpreter": "x",
            "python_version": "3.12",
            "packages": {"analysis_utilities": "26.8.27"},
            "bwrap": "",
        },
    )
    schema = ToolExecutor._execute_python_def(None)
    description = schema["function"]["description"]
    assert "analysis_utilities" in description
    assert "PlottingUtils" in description


def test_guidance_reaches_the_subagent_instructions(monkeypatch) -> None:
    from physics_intern.autophysicist.subagent import code_execution_suffix

    monkeypatch.setattr(
        "physics_intern.utils.sandbox.runtime_summary",
        lambda _i=None: {
            "interpreter": "x",
            "python_version": "3.12",
            "packages": {"analysis_utilities": "26.8.27"},
            "bwrap": "",
        },
    )
    assert "load_tree_data" in code_execution_suffix(None, 60)


def test_no_guidance_when_the_library_is_absent(monkeypatch) -> None:
    from physics_intern.agents.computer.tools import ToolExecutor

    monkeypatch.setattr(
        "physics_intern.utils.sandbox.runtime_summary",
        lambda _i=None: {
            "interpreter": "x",
            "python_version": "3.12",
            "packages": {"numpy": "2.0"},
            "bwrap": "",
        },
    )
    description = ToolExecutor._execute_python_def(None)["function"]["description"]
    assert "analysis_utilities" not in description
    assert "numpy 2.0" in description


@needs_bwrap
def test_a_shell_is_available_in_the_sandbox(tmp_path: Path) -> None:
    """ROOT shells out while starting its interpreter and segfaults without one."""
    script = tmp_path / "shell.py"
    script.write_text(
        "import subprocess\n"
        "print(subprocess.run(['/bin/sh', '-c', 'echo shell-ok'],\n"
        "                     capture_output=True, text=True).stdout.strip())\n"
    )
    result = execute_python(
        script, timeout=60, cwd=tmp_path, policy=_policy(tmp_path)
    )
    assert result.stdout.strip() == "shell-ok", result.stderr


def test_pyroot_notes_appear_when_root_is_present() -> None:
    """Every failed script in the first fast run guessed at the C++ API."""
    from physics_intern.utils.runtime_notes import notes_for

    note = notes_for({"ROOT": "6.40.00"})
    assert "GetListOfBranches" in note
    assert "DO NOT INTROSPECT" in note
    assert notes_for({"numpy": "2.0"}) == ""


def test_both_notes_compose() -> None:
    from physics_intern.utils.runtime_notes import notes_for

    note = notes_for({"ROOT": "6.40.00", "analysis_utilities": "26.8.27"})
    assert "GetListOfBranches" in note and "load_tree_data" in note


def test_the_guidance_states_the_real_timeout(monkeypatch) -> None:
    """"The timeout is short" was ignored; a number with the consequence is not.

    The first run to get this far timed out twice on byte-identical scripts,
    reading a multi-GB file in full under a 60 s limit.
    """
    from physics_intern.utils.runtime_notes import notes_for

    note = notes_for({"analysis_utilities": "26.8.27"}, timeout=900)
    # The note is wrapped and comment-prefixed, so compare on normalised text.
    flat = " ".join(note.replace("#", " ").split())
    assert "killed after 900 seconds" in flat
    assert "{timeout}" not in note
    assert "does NOT mean your approach was wrong" in flat


def test_the_execute_python_schema_states_the_real_timeout() -> None:
    from physics_intern.agents.computer.tools import ToolExecutor

    description = ToolExecutor._execute_python_def(None, 900)["function"][
        "description"
    ]
    assert "killed after 900s" in description


def test_the_notes_warn_about_raw_waveforms() -> None:
    """Baseline-subtract and invert is tacit domain knowledge.

    Integrating raw CAEN samples gives a charge dominated by the DC offset and
    a meaningless tail-to-total ratio — and every number downstream looks
    plausible. The agent is told, and pointed at the lab's implementation
    rather than left to hand-roll it.
    """
    from physics_intern.utils.runtime_notes import notes_for

    note = " ".join(notes_for({"analysis_utilities": "26.8.27"}).split())
    assert "goes NEGATIVE" in note
    assert "WaveformProcessingUtils" in note
    assert "cfg.polarity" in note
    assert "ProcessingStats" in note
