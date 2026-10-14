"""`gateway status` recognises a gateway the NixOS / Home Manager module runs.

It only knew the unit names its own (now removed) installer wrote, so the
module's `son-of-anton-<instance>.service` gateways were reported as "running
manually" with advice to install them as a service. The supervising unit now
comes from the process's cgroup.
"""

from __future__ import annotations

import pytest

from son_of_anton_cli.gateway import service_of_cgroup


@pytest.mark.parametrize(
    "cgroup, unit",
    [
        ("0::/system.slice/son-of-anton-work.service\n", "son-of-anton-work.service"),
        (
            "0::/user.slice/user-1001.slice/user@1001.service/app.slice/son-of-anton-gateway.service\n",
            "son-of-anton-gateway.service",
        ),
        # Started from a terminal: a scope under the user manager is not supervision.
        ("0::/user.slice/user-1001.slice/user@1001.service/app.slice/kitty-2157954-0.scope\n", None),
        ("0::/user.slice/user-1001.slice/session-3.scope\n", None),
        # cgroup v1: one line per hierarchy.
        (
            "12:pids:/system.slice/son-of-anton-play.service\n1:name=systemd:/system.slice/son-of-anton-play.service\n",
            "son-of-anton-play.service",
        ),
        ("", None),
    ],
)
def test_service_of_cgroup(cgroup, unit) -> None:
    assert service_of_cgroup(cgroup) == unit
