"""User-authorization methods for ``GatewayRunner``.

Kept out of ``gateway/run.py`` so the inbound-message authorization rule —
whether a sender may talk to the agent — reads in one place. ``self.*`` calls
resolve via the MRO.
"""

from __future__ import annotations

import os
from typing import Optional

from gateway.config import Platform
from gateway.session import SessionSource


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

    def _is_user_authorized(self, source: SessionSource) -> bool:
        """Whether the sender is named in ``SIGNAL_ALLOWED_USERS``.

        An unset allowlist denies everyone. Group routing is the adapter's
        job (``SIGNAL_GROUP_ALLOWED_USERS``); this gates the person.
        """
        user_id = source.user_id
        if not user_id or source.platform != Platform.SIGNAL:
            return False
        allowlist = (os.getenv("SIGNAL_ALLOWED_USERS") or "").strip()
        allowed_ids = {uid.strip() for uid in allowlist.split(",") if uid.strip()}
        return user_id in allowed_ids
