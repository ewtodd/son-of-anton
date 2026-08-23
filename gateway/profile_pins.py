"""Per-chat profile pins for multiplexed gateways.

A multiplexed gateway serves several profiles, normally routed by
``gateway.profile_routes``. A *pin* is the command-driven alternative: a
sender in a chat runs ``/profile <name>`` and every subsequent message in
that chat resolves to the pinned profile — no room/group routes required.
Pins are keyed by ``platform:chat_id`` and persisted in the gateway's own
home so they survive restarts.

The pin is only ever consulted while ``gateway.multiplex_profiles`` is on,
and only when no explicit route or ``/p/<profile>/`` stamp matched.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PINS_FILENAME = "profile_pins.json"


def pin_key(platform: str, chat_id: str) -> str:
    """The storage key for one chat's pin: ``"platform:chat_id"``."""
    platform = str(platform or "").strip().lower()
    chat_id = str(chat_id or "").strip()
    return f"{platform}:{chat_id}"


def load_pins(home: Path) -> dict[str, str]:
    """Read the pin map from ``<home>/profile_pins.json`` (missing -> {})."""
    path = Path(home) / PINS_FILENAME
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {
            str(key): str(value).strip()
            for key, value in data.items()
            if str(value).strip()
        }
    except Exception:
        logger.debug("profile pins unreadable at %s", path, exc_info=True)
        return {}


def save_pins(home: Path, pins: dict[str, str]) -> None:
    """Atomically persist the pin map to ``<home>/profile_pins.json``."""
    path = Path(home) / PINS_FILENAME
    try:
        from utils import atomic_json_write

        atomic_json_write(path, pins)
    except Exception:
        logger.debug("profile pins not saved at %s", path, exc_info=True)


def resolve_pin(
    pins: dict[str, str],
    platform: str,
    chat_id: str,
    served_profiles: set[str],
) -> Optional[str]:
    """Return the pinned profile for a chat, or None.

    *served_profiles* is the multiplexer's authoritative profile set
    (including ``"default"``). A pin pointing at a profile that is no longer
    served is treated as absent so the chat falls back to the default.
    """
    name = pins.get(pin_key(platform, chat_id))
    if not name:
        return None
    if name == "default" or name in served_profiles:
        return name
    return None
