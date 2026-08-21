"""The decomposed command modules stay lazy after `import renco_cli.main`.

The main.py decomposition re-exports the sessions/update/dashboard command
surface from renco_cli.main so argparse wiring and monkeypatches keep
resolving. Those re-exports must not import the modules eagerly: every
`renco` invocation (including `renco --version`) would pay for update_cmd's
dependency chain (jwt, click, ...) even when no subcommand runs.
"""

import subprocess
import sys
import textwrap

import renco_cli.main


def test_importing_main_does_not_import_command_modules():
    code = textwrap.dedent(
        """
        import sys
        import renco_cli.main  # noqa: F401
        loaded = [
            m
            for m in (
                "renco_cli.update_cmd",
                "renco_cli.sessions_cmd",
                "renco_cli.dashboard_procs",
            )
            if m in sys.modules
        ]
        assert not loaded, f"eagerly imported: {loaded}"
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def test_lazy_reexports_resolve_to_real_objects():
    import renco_cli.dashboard_procs
    import renco_cli.sessions_cmd
    import renco_cli.update_cmd

    assert renco_cli.main.cmd_sessions is renco_cli.sessions_cmd.cmd_sessions
    assert (
        renco_cli.main._cmd_update_impl is renco_cli.update_cmd._cmd_update_impl
    )
    assert (
        renco_cli.main._scan_dashboard_processes
        is renco_cli.dashboard_procs._scan_dashboard_processes
    )
    # Back-compat alias resolves to the kill helper.
    assert (
        renco_cli.main._warn_stale_dashboard_processes
        is renco_cli.dashboard_procs._kill_stale_dashboard_processes
    )


def test_lazy_reexports_accept_monkeypatch(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("renco_cli.main._cmd_update_impl", sentinel)
    assert renco_cli.main._cmd_update_impl is sentinel
