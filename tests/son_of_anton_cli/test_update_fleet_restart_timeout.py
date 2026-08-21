"""Regression for #68523 — one systemctl timeout must not abort fleet restarts.

On hosts with many profile-backed ``son-of-anton-gateway*.service`` units,
``son-of-anton update`` used to wrap the entire per-scope unit loop in a single
``except subprocess.TimeoutExpired``. A timeout on unit N skipped units
N+1…, leaving later gateways on pre-update in-memory modules while the
checkout on disk was already new (mixed-generation crashes).
"""

from __future__ import annotations

import subprocess

import pytest

from son_of_anton_cli.main import (
    _for_each_systemd_gateway_unit,
    _service_unit_supports_graceful_sigusr1_restart,
    _warn_incomplete_gateway_fleet_restart,
)


def _list_units_stdout(names: list[str]) -> str:
    return "\n".join(f"{name}.service loaded active running" for name in names)


class TestFleetRestartTimeoutIsolation:
    def test_timeout_on_middle_unit_continues_remaining_units(self):
        units = [
            "son-of-anton-gateway-xiaomo1",
            "son-of-anton-gateway-xiaomo2",
            "son-of-anton-gateway-xiaomo3",
            "son-of-anton-gateway-xiaomo4",
            "son-of-anton-gateway-xiaomo5",
            "son-of-anton-gateway-xiaomo6",
            "son-of-anton-gateway-xiaomo7",
            "son-of-anton-gateway",
        ]
        restarted: list[str] = []
        failed: list[str] = []
        timeout_cmds: list = []

        def process_unit(svc_name: str) -> None:
            if svc_name == "son-of-anton-gateway-xiaomo5":
                raise subprocess.TimeoutExpired(
                    cmd=["systemctl", "--user", "--no-ask-password", "restart", svc_name],
                    timeout=15,
                )
            restarted.append(svc_name)

        def on_unit_timeout(svc_name: str, exc: subprocess.TimeoutExpired) -> None:
            failed.append(svc_name)
            timeout_cmds.append(exc.cmd)

        _for_each_systemd_gateway_unit(
            _list_units_stdout(units),
            process_unit=process_unit,
            on_unit_timeout=on_unit_timeout,
        )

        assert failed == ["son-of-anton-gateway-xiaomo5"]
        assert restarted == [
            "son-of-anton-gateway-xiaomo1",
            "son-of-anton-gateway-xiaomo2",
            "son-of-anton-gateway-xiaomo3",
            "son-of-anton-gateway-xiaomo4",
            "son-of-anton-gateway-xiaomo6",
            "son-of-anton-gateway-xiaomo7",
            "son-of-anton-gateway",
        ]
        assert set(restarted) | set(failed) == set(units)
        assert timeout_cmds == [
            ["systemctl", "--user", "--no-ask-password", "restart", "son-of-anton-gateway-xiaomo5"]
        ]

    def test_non_gateway_units_in_list_output_are_ignored(self):
        seen: list[str] = []

        _for_each_systemd_gateway_unit(
            "\n".join(
                [
                    "ssh.service loaded active running",
                    "son-of-anton-gateway-coder.service loaded active running",
                    "not-a-service loaded active running",
                    "",
                ]
            ),
            process_unit=seen.append,
            on_unit_timeout=lambda *_: pytest.fail("unexpected timeout"),
        )

        assert seen == ["son-of-anton-gateway-coder"]

    def test_son_of_anton_serve_units_are_included(self):
        # #83438 — son-of-anton update restarted son-of-anton-gateway* units but left
        # son-of-anton-serve* (the Desktop app's backend) on stale pre-update code.
        seen: list[str] = []

        _for_each_systemd_gateway_unit(
            "\n".join(
                [
                    "ssh.service loaded active running",
                    "son-of-anton-serve.service loaded active running",
                    "son-of-anton-serve-work.service loaded active running",
                    "son-of-anton-gateway.service loaded active running",
                    "",
                ]
            ),
            process_unit=seen.append,
            on_unit_timeout=lambda *_: pytest.fail("unexpected timeout"),
        )

        assert seen == ["son-of-anton-serve", "son-of-anton-serve-work", "son-of-anton-gateway"]

    def test_son_of_anton_server_near_prefix_is_rejected(self):
        # Review on #83595: a bare ``startswith("son-of-anton-serve")`` gate also
        # accepts the unrelated ``son-of-anton-server.service``. Only the exact
        # base unit or the hyphenated profile family should pass.
        seen: list[str] = []

        _for_each_systemd_gateway_unit(
            _list_units_stdout(["son-of-anton-server"]),
            process_unit=seen.append,
            on_unit_timeout=lambda *_: pytest.fail("unexpected timeout"),
        )

        assert seen == []

    def test_son_of_anton_gateway_near_prefix_is_rejected(self):
        # Same strict shape on the gateway side: profile units are
        # ``son-of-anton-gateway-<profile>``, so a hypothetical
        # ``son-of-anton-gatewayd.service`` must not enter the restart path.
        seen: list[str] = []

        _for_each_systemd_gateway_unit(
            _list_units_stdout(["son-of-anton-gatewayd", "son-of-anton-gateway-coder"]),
            process_unit=seen.append,
            on_unit_timeout=lambda *_: pytest.fail("unexpected timeout"),
        )

        assert seen == ["son-of-anton-gateway-coder"]


class TestGracefulSigusr1Eligibility:
    def test_gateway_units_are_eligible(self):
        assert _service_unit_supports_graceful_sigusr1_restart("son-of-anton-gateway")
        assert _service_unit_supports_graceful_sigusr1_restart(
            "son-of-anton-gateway-work"
        )

    def test_serve_units_are_not_eligible(self):
        # son-of-anton-serve doesn't run gateway/run.py, so it never installs the
        # SIGUSR1 handler — sending it the signal would just terminate the
        # process (the default action) instead of draining gracefully.
        assert not _service_unit_supports_graceful_sigusr1_restart("son-of-anton-serve")
        assert not _service_unit_supports_graceful_sigusr1_restart(
            "son-of-anton-serve-work"
        )

    def test_process_errors_other_than_timeout_still_propagate(self):
        def process_unit(_svc_name: str) -> None:
            raise RuntimeError("not a timeout")

        with pytest.raises(RuntimeError, match="not a timeout"):
            _for_each_systemd_gateway_unit(
                _list_units_stdout(["son-of-anton-gateway"]),
                process_unit=process_unit,
                on_unit_timeout=lambda *_: pytest.fail("timeout handler must not run"),
            )


class TestIncompleteFleetRestartWarning:
    def test_warns_with_exact_unrestarted_units(self, capsys):
        _warn_incomplete_gateway_fleet_restart(
            ["son-of-anton-gateway-xiaomo5", "son-of-anton-gateway-xiaomo6", "son-of-anton-gateway-xiaomo5"]
        )
        out = capsys.readouterr().out
        assert "Update incomplete" in out
        assert out.count("son-of-anton-gateway-xiaomo5") == 1
        assert "son-of-anton-gateway-xiaomo6" in out
        assert "pre-update code" in out

