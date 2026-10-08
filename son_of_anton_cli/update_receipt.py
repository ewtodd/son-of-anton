"""Structured update receipts + post-update fleet version verification.

Phase 1 of the fleet-update reliability plan (#91277): the updater must
*prove* its outcome instead of assuming it.

Two additive capabilities, both designed so a failure inside them can never
break an update (every public entry point is exception-swallowing):

1. **Update receipt** — a machine-readable JSON record of what one
   ``son-of-anton update`` run discovered, did, skipped (and why), written to
   ``<SON_OF_ANTON_HOME>/logs/update_receipts/``. Silent-failure classes this
   makes visible: #88848 (helper died after "success" printed), #74973
   (restart silently skipped), #85753 (restart phase never ran), #81193
   (desktop shows failure for a successful update).

2. **Fleet version verification** — after the restart phase, read every
   profile's ``gateway_state.json``, compare each live gateway's stamped
   ``code_sha`` (written by ``gateway/status.py`` on every runtime-status
   write) against the freshly-updated checkout's HEAD, and print a fleet
   version matrix. Mixed-version fleets (#88654, #69754, #77553, #56717)
   become a loud, actionable report instead of a latent state.

Deployment-kind awareness (docker/image-managed installs) rides on
``son_of_anton_cli.build_info.get_code_identity()``: an image build reports
``source="build-file"`` and the receipt records that the install is not
in-place updatable.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import logging

logger = logging.getLogger(__name__)


# Module-level current receipt. ``son-of-anton update`` is a single-threaded CLI
# command; a module singleton lets the 7k-line updater record steps from
# any depth without threading a handle through every helper.
_current: Optional["UpdateReceipt"] = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UpdateReceipt:
    """Collects the observable facts of one ``son-of-anton update`` run."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {
            "schema": 1,
            "started_at": _utc_now_iso(),
            "finished_at": None,
            "argv": list(sys.argv),
            "pid": os.getpid(),
            "outcome": "running",  # running | success | partial | failed
            "pre_update": {},
            "post_update": {},
            "steps": [],
            "skips": [],
            "gateway_restart": {},
            "fleet": [],
        }
        try:
            from son_of_anton_cli.build_info import get_code_identity

            self.data["pre_update"] = get_code_identity()
        except Exception:
            pass

    # -- recording ---------------------------------------------------------
    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.data["steps"].append(
            {"name": name, "ok": bool(ok), "detail": detail, "at": _utc_now_iso()}
        )

    def skip(self, name: str, reason: str) -> None:
        self.data["skips"].append(
            {"name": name, "reason": reason, "at": _utc_now_iso()}
        )


    def finalize(self, outcome: str) -> None:
        self.data["outcome"] = outcome
        self.data["finished_at"] = _utc_now_iso()
        try:
            from son_of_anton_cli.build_info import get_code_identity

            self.data["post_update"] = get_code_identity(refresh=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Fleet version verification
# ---------------------------------------------------------------------------


