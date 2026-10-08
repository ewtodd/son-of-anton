"""Runtime inventory + update plan for the fleet-update pipeline (#91277 Phase 2).

One read-only pass that answers, BEFORE any mutation: what Son of Anton runtimes
are running on this machine, how is each one deployed, which of them will
this update touch, and how will each be restarted?

This is the "plan" phase of the transactional deployment model (#88683):

    plan → snapshot → apply → restart-per-kind → verify → report

The module is deliberately side-effect free — every collector is a probe
over primitives that already exist (`find_profile_gateway_processes`,
`_get_service_pids`, `gateway_state.json` code stamps from #91283,
`detect_install_method`) — so `son-of-anton update --plan` can run on a live
fleet with zero risk, and the update receipt can embed the inventory
without changing update behavior.

Deployment kinds (the concept most fleet-update bugs were missing):

    git      — source checkout; updatable in place via `son-of-anton update`
    docker   — published image; NOT updatable in place (pull + recreate)
    nix/apt  — package-manager owned; updatable via the manager only
    unknown  — no marker; treated as in-place updatable (legacy default)

Supervisors (how a runtime is restarted after code changes):

    systemd / launchd — restart via the service manager (fleet-wide)
    desktop           — Desktop app supervises `son-of-anton serve`; it respawns
    manual            — plain process; SIGTERM + watcher/manual relaunch
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


