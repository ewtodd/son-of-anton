"""Shared helpers for tool backend selection."""

from __future__ import annotations

import logging

from utils import is_truthy_value

logger = logging.getLogger(__name__)


def prefers_gateway(config_section: str) -> bool:
    """Return True when the user opted into the Tool Gateway for this tool.

    Reads ``<section>.use_gateway`` from config.toml.  Never raises.
    """
    try:
        from son_of_anton_cli.config import load_config
        section = (load_config() or {}).get(config_section)
        if isinstance(section, dict):
            return is_truthy_value(section.get("use_gateway"), default=False)
    except Exception:
        pass
    return False


# Per-capability keys that also count as "this category has been configured".
_EXTRA_SELECTION_KEYS = {
    "web": ("search_backend", "extract_backend"),
}

# Which key(s) carry the category's provider selection. ``browser.backend``
# is deliberately excluded for the browser section — it is the DRIVER choice
# ("browser-use" CLI vs built-in tools), not the cloud provider selection.
_SELECTION_NAME_KEYS = {
    "browser": ("cloud_provider",),
    "web": ("backend",),
}
_DEFAULT_NAME_KEYS = ("provider", "backend", "cloud_provider")


def read_selection(section: str) -> str | None:
    """Return the stored `son-of-anton tools` provider string for a config section.

    THE single runtime read of the persisted selection. Returns:
    - ``"nous"`` — the managed Nous Tool Gateway row was selected,
    - a vendor name (``"fal"``, ``"openai"``, ``"firecrawl"``, ...) — that
      vendor, direct, with the user's own credentials,
    - ``None`` — the category has NEVER been configured; the legacy
      credential autodetect ladder is permitted (and must not be persisted).

    Reads the RAW config.toml (not the DEFAULT_CONFIG-merged view) so key
    presence means "a selection was actually written", not "the schema has a
    default". Never raises; an unreadable config reports ``None``.

    Legacy interpretation (read-time only — nothing is migrated on disk):
    older picker versions wrote ``<section>.use_gateway`` beside the name
    key. ``use_gateway: false`` beside a name key maps to that name.
    """
    try:
        from son_of_anton_cli.config import read_raw_config_readonly

        cfg = read_raw_config_readonly() or {}
        raw = cfg.get(section) if isinstance(cfg, dict) else None
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return None

    def _str_or_none(key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        text = str(value).strip().lower()
        return text or None

    name = None
    for key in _SELECTION_NAME_KEYS.get(section, _DEFAULT_NAME_KEYS):
        name = _str_or_none(key)
        if name:
            break

    # NOTE on the legacy DEFAULT_CONFIG ``stt.provider: local`` seed: it never
    # reached the raw config.toml (``save_config`` strips schema defaults),
    # and the old picker's Local Whisper row always wrote ``use_gateway:
    # False`` beside it. A raw ``local`` here therefore IS a user selection —
    # hand-written or picker-written — and is honored like any other vendor
    # name. The seeded-value ambiguity only exists in DEFAULT_CONFIG-merged
    # views, which this function never reads.

    if name:
        return name

    # use_gateway: false with no name key is not a usable selection shape;
    # per-capability web keys still count as configured elsewhere via
    # selection_exists(). Fall to autodetect.
    return None


def selection_exists(section: str) -> bool:
    """True when ANY selection signal has ever been written for the section.

    Wider than ``read_selection() is not None``: per-capability web keys
    (``search_backend``/``extract_backend``) mark the category as configured
    even when the shared backend name is empty.
    """
    if read_selection(section) is not None:
        return True
    extra = _EXTRA_SELECTION_KEYS.get(section, ())
    if not extra:
        return False
    try:
        from son_of_anton_cli.config import read_raw_config_readonly

        cfg = read_raw_config_readonly() or {}
        raw = cfg.get(section) if isinstance(cfg, dict) else None
    except Exception:
        return False
    if not isinstance(raw, dict):
        return False
    return any(str(raw.get(key) or "").strip() for key in extra)


def selection_error(section: str, selection_name: str, failure: str) -> str:
    """The uniform honest-error contract for a selected-but-broken provider."""
    return (
        f"{section} is configured to use {selection_name} (set via son-of-anton "
        f"tools), but {failure}. Run 'son-of-anton tools' to change it."
    )


