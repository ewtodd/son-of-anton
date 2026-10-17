"""User-authorization methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` as part of the god-file decomposition campaign
(``~/.son-of-anton/plans/god-file-decomposition.md``, Phase 3 mechanical mixin lifts).
This mixin holds the inbound-message authorization cluster: whether a user/chat
is allowed to talk to the agent, the per-adapter DM policy, and the
unauthorized-DM behavior.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``self.*`` calls resolve unchanged via the MRO. Neutral dependencies import at
module top; the module-level ``logger`` is imported lazily inside the one method
that uses it (``from gateway.run import logger`` resolves at call time, when
``gateway.run`` is fully loaded) so this module never imports ``gateway.run`` at
import time -> no import cycle. The lazy import preserves the exact logger name
(``"gateway.run"``) so log records are unchanged.
"""

from __future__ import annotations

import os
from typing import Optional

from gateway.config import Platform
from gateway.session import SessionSource


def _auth_env(name: str, default: str = "") -> str:
    """Read an allowlist/auth env var, preferring the .env secret store."""
    if not name:
        return default
    try:
        from agent.secret_scope import get_secret

        val = get_secret(name)
        if val is not None and str(val).strip():
            return str(val).strip()
    except Exception:
        pass
    return (os.getenv(name) or default).strip()


def _platform_gate_env(name: str, default: str = "") -> str:
    """Read a platform allow/deny gate env var from the process environment."""
    if not name:
        return default
    return (os.getenv(name) or default).strip()


class GatewayAuthorizationMixin:
    """User/chat authorization methods for ``GatewayRunner``."""

    def _authorization_adapter(self, platform: Optional[Platform]):
        """Resolve the live adapter whose intake policy gates authorization."""
        if not platform:
            return None
        adapters = getattr(self, "adapters", None) or {}
        return adapters.get(platform)

    def _adapter_for_source(self, source: Optional[SessionSource]):
        """Resolve the live adapter for an inbound ``SessionSource``."""
        if source is None:
            return None
        return self._authorization_adapter(getattr(source, "platform", None))

    def _pairing_store_for(self, source: "SessionSource"):
        """Return the gateway's PairingStore. One store per gateway process."""
        return getattr(self, "pairing_store", None)

    def _is_user_authorized(self, source: SessionSource) -> bool:
        """
        Check if a user is authorized to use the bot.

        Checks in order:
        1. Per-platform allow-all flag (SIGNAL_ALLOW_ALL_USERS=true)
        2. Environment variable allowlists (SIGNAL_ALLOWED_USERS, etc.)
        3. DM pairing approved list
        4. Global allow-all (GATEWAY_ALLOW_ALL_USERS=true)
        5. Default: deny
        """

        user_id = source.user_id

        if not user_id:
            return False

        platform_env_map = {Platform.SIGNAL: "SIGNAL_ALLOWED_USERS"}
        platform_allow_all_map = {Platform.SIGNAL: "SIGNAL_ALLOW_ALL_USERS"}

        # Per-platform allow-all flag (e.g., SIGNAL_ALLOW_ALL_USERS=true)
        platform_allow_all_var = platform_allow_all_map.get(source.platform, "")
        if platform_allow_all_var and _auth_env(platform_allow_all_var).lower() in {"true", "1", "yes"}:
            return True

        # Check pairing store. A pairing entry is a first-class authorization
        # grant, created only by a trusted operator approving a pairing code
        # (son-of-anton gateway pairing approve / the authenticated dashboard) — an
        # inbound sender can never reach approve_code, so this is not an
        # attacker-controlled path. Honored as a UNION with the allowlist: a
        # paired user is authorized regardless of the allowlist, and when an
        # allowlist IS configured, operator approval also writes the user into
        # that allowlist (see PairingStore._approve_user), keeping a single
        # operator-visible source of truth. (#23778: the original bypass was the
        # inbound message/approval-button gate, not this gate; that gate is
        # fixed separately.)
        platform_name = source.platform.value if source.platform else ""
        pairing_store = self._pairing_store_for(source)
        if pairing_store is not None and pairing_store.is_approved(platform_name, user_id):
            return True

        # Check platform-specific and global allowlists
        platform_allowlist = _auth_env(platform_env_map.get(source.platform, ""))
        global_allowlist = _auth_env("GATEWAY_ALLOWED_USERS")

        if not platform_allowlist and not global_allowlist:
            # No allowlists configured -- check global allow-all flag
            return _auth_env("GATEWAY_ALLOW_ALL_USERS").lower() in {"true", "1", "yes"}

        # Check if user is in any allowlist.
        allowed_ids = set()
        if platform_allowlist:
            allowed_ids.update(uid.strip() for uid in platform_allowlist.split(",") if uid.strip())
        if global_allowlist:
            allowed_ids.update(uid.strip() for uid in global_allowlist.split(",") if uid.strip())

        # "*" in any allowlist means allow everyone (consistent with
        # SIGNAL_GROUP_ALLOWED_USERS precedent)
        if "*" in allowed_ids:
            return True

        check_ids = {user_id}
        if "@" in user_id:
            check_ids.add(user_id.split("@")[0])

        return bool(check_ids & allowed_ids)

    def _get_unauthorized_dm_behavior(
        self,
        platform: Optional[Platform],
    ) -> str:
        """Return how unauthorized DMs should be handled for a platform.

        Resolution order:
        1. Explicit per-platform ``unauthorized_dm_behavior`` in config — always wins.
        2. Explicit global ``unauthorized_dm_behavior`` in config — wins for
           chat-shaped platforms when no per-platform override is set.
        3. When an allowlist (``SIGNAL_ALLOWED_USERS`` or
           ``GATEWAY_ALLOWED_USERS``) is configured, default to ``"ignore"`` —
           the allowlist signals that the owner has deliberately restricted
           access; spamming unknown contacts with pairing codes is both noisy
           and a potential info-leak. (#9337)
        4. No allowlist and no explicit config → ``"pair"`` (open-gateway default).
        """
        config = getattr(self, "config", None)

        # Check for an explicit per-platform override first.
        if config and hasattr(config, "get_unauthorized_dm_behavior") and platform:
            platform_cfg = config.platforms.get(platform) if hasattr(config, "platforms") else None
            if platform_cfg and "unauthorized_dm_behavior" in getattr(platform_cfg, "extra", {}):
                # Operator explicitly configured behavior for this platform — respect it.
                return config.get_unauthorized_dm_behavior(platform)

        # Check for an explicit global config override.
        if config and hasattr(config, "unauthorized_dm_behavior"):
            if config.unauthorized_dm_behavior != "pair":  # non-default → explicit override
                return config.unauthorized_dm_behavior

        # No explicit override.  Fall back to allowlist-aware default:
        # if any allowlist is configured for this platform, silently drop
        # unauthorized messages instead of sending pairing codes.
        if platform == Platform.SIGNAL and _platform_gate_env("SIGNAL_ALLOWED_USERS").strip():
            return "ignore"

        if _platform_gate_env("GATEWAY_ALLOWED_USERS").strip():
            return "ignore"

        return "pair"
