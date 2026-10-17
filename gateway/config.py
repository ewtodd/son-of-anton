"""
Gateway configuration: the Signal platform, its home channel, and the
``[gateway]`` settings the deployments use.
"""

import logging
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from enum import Enum

from son_of_anton_cli.config import get_son_of_anton_home
from agent.hour_window import parse_hour_window
from utils import is_truthy_value

logger = logging.getLogger(__name__)


def _coerce_bool(value: Any, default: bool = True) -> bool:
    """Coerce bool-ish config values, preserving a caller-provided default."""
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        return default
    return is_truthy_value(value, default=default)


def _normalize_transport_token(value: Any) -> str:
    """Normalize a streaming transport/mode value to a canonical token.

    Handles the TOML 1.1 boolean quirk where bare ``on`` / ``off`` parse to
    Python ``True`` / ``False``.
    Without this, ``mode: off`` arrives as boolean ``False`` and stringifying it
    yields ``"false"`` instead of the advertised ``"off"``, so streaming would be
    enabled instead of disabled. Booleans map to ``"auto"`` (True) / ``"off"``
    (False); anything else is lower-cased, defaulting to ``"auto"``.
    """
    if value is None:
        return "auto"
    if isinstance(value, bool):
        return "auto" if value else "off"
    return str(value).strip().lower() or "auto"


def _coerce_float(value: Any, default: float) -> float:
    """Coerce numeric config values, falling back on malformed input."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    """Coerce integer config values, falling back on malformed input."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default



_SYSTEMD_WATCHDOG_MAX_SECONDS = 2_147_483_647


def coerce_systemd_watchdog_seconds(
    value: Any, key: str = "gateway.systemd_watchdog_seconds"
) -> int:
    """Return a bounded positive watchdog interval or zero when disabled.

    Runtime and service generation share this normalization so a value can
    never enable ``Type=notify`` while disabling application heartbeats.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        logger.warning("Ignoring invalid %s (expected a positive integer)", key)
        return 0
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw or not raw.isascii() or not raw.isdecimal():
            logger.warning("Ignoring invalid %s (expected a positive integer)", key)
            return 0
        try:
            parsed = int(raw, 10)
        except (TypeError, ValueError, OverflowError):
            logger.warning("Ignoring invalid %s (expected a positive integer)", key)
            return 0
    else:
        logger.warning("Ignoring invalid %s (expected a positive integer)", key)
        return 0
    if parsed == 0:
        return 0
    if not 0 < parsed <= _SYSTEMD_WATCHDOG_MAX_SECONDS:
        logger.warning(
            "Ignoring invalid %s (expected an integer from 1 to %d)",
            key,
            _SYSTEMD_WATCHDOG_MAX_SECONDS,
        )
        return 0
    return parsed


def _coerce_dict(value: Any) -> Dict[str, Any]:
    """Return *value* when it is a mapping, otherwise an empty dict."""
    return value if isinstance(value, dict) else {}


class Platform(Enum):
    """Supported messaging platforms."""
    LOCAL = "local"
    SIGNAL = "signal"


@dataclass
class HomeChannel:
    """
    Default destination for a platform.

    When a cron job specifies deliver="signal" without a specific chat ID,
    messages are sent to this home channel.
    """
    platform: Platform
    chat_id: str
    name: str  # Human-readable name for display
    thread_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "name": self.name,
        }
        if self.thread_id:
            result["thread_id"] = self.thread_id
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HomeChannel":
        return cls(
            platform=Platform(data["platform"]),
            chat_id=str(data["chat_id"]),
            name=data.get("name", "Home"),
            thread_id=str(data["thread_id"]) if data.get("thread_id") else None,
        )


def persist_home_channel(home: HomeChannel) -> None:
    """Persist a home channel to config.toml."""
    from son_of_anton_cli.config import load_config, save_config

    config = load_config()
    platforms = config.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        platforms = {}
        config["platforms"] = platforms
    platform_config = platforms.setdefault(home.platform.value, {})
    if not isinstance(platform_config, dict):
        platform_config = {}
        platforms[home.platform.value] = platform_config
    platform_config.setdefault("enabled", True)
    platform_config["home_channel"] = home.to_dict()
    save_config(config)


@dataclass
class PlatformConfig:
    """Configuration for a single messaging platform."""
    enabled: bool = False
    home_channel: Optional[HomeChannel] = None

    # Whether the gateway is allowed to send "♻️ Gateway online" /
    # "♻ Gateway restarted" lifecycle notifications on this platform.
    gateway_restart_notification: bool = True

    # Whether the gateway shows a "typing…" indicator while the agent
    # processes a message. Drives the per-message _keep_typing refresh loop in
    # gateway/platforms/base.py.
    typing_indicator: bool = True

    # Platform-specific settings
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlatformConfig":
        """Build from a ``[platforms.<name>]`` block.

        ``require_mention`` is the one adapter setting read from the block;
        it lands in ``extra``, where the Signal adapter looks for it.
        """
        data = _coerce_dict(data)
        home_channel = None
        if isinstance(data.get("home_channel"), dict):
            home_channel = HomeChannel.from_dict(data["home_channel"])
        extra = dict(_coerce_dict(data.get("extra", {})))
        if "require_mention" in data:
            extra["require_mention"] = data["require_mention"]
        return cls(
            enabled=_coerce_bool(data.get("enabled"), False),
            home_channel=home_channel,
            gateway_restart_notification=_coerce_bool(
                data.get("gateway_restart_notification"), True
            ),
            typing_indicator=_coerce_bool(data.get("typing_indicator"), True),
            extra=extra,
        )


# Streaming defaults — single source of truth so both StreamingConfig and
# StreamConsumerConfig agree on the out-of-the-box edit rhythm.  A touch under
# 1s lets the cadence breathe without bumping into rate limits, and a smaller
# buffer threshold makes short replies feel near-instant in DMs.
DEFAULT_STREAMING_EDIT_INTERVAL: float = 0.8
DEFAULT_STREAMING_BUFFER_THRESHOLD: int = 24
DEFAULT_STREAMING_CURSOR: str = " ▉"


@dataclass
class StreamingConfig:
    """Configuration for real-time token streaming to messaging platforms."""
    enabled: bool = False
    # Transport selection:
    #   "auto"  — prefer native streaming-draft updates when the platform
    #             supports them; fall back to edit-based when not.
    #   "draft" — explicitly request native drafts; falls back to edit when
    #             the platform/chat doesn't support them.
    #   "edit"  — progressive editMessageText only (legacy behaviour).
    #   "off"   — disable streaming entirely.
    #
    # Default is "auto": prefer native draft streaming on platforms that
    # support it and fall back to edit-based streaming everywhere else.  This
    # is safe as a global default because adapters without draft support
    # (Discord, Slack, …) report supports_draft_streaming() == False and
    # transparently use the edit path.
    transport: str = "auto"
    edit_interval: float = DEFAULT_STREAMING_EDIT_INTERVAL
    buffer_threshold: int = DEFAULT_STREAMING_BUFFER_THRESHOLD
    cursor: str = DEFAULT_STREAMING_CURSOR
    # Ported from openclaw/openclaw#72038.  When >0, the final edit for
    # a long-running streamed response is delivered as a fresh message
    # if the original preview has been visible for at least this many
    # seconds, so the platform's visible timestamp reflects completion
    # time instead of the preview creation time.  Adapters opt in per
    # platform; other platforms ignore the setting.  Default 0 disables
    # the fresh-message replacement path; set >0 to opt in.
    fresh_final_after_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "transport": self.transport,
            "edit_interval": self.edit_interval,
            "buffer_threshold": self.buffer_threshold,
            "cursor": self.cursor,
            "fresh_final_after_seconds": self.fresh_final_after_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StreamingConfig":
        if not isinstance(data, dict) or not data:
            return cls()

        # ``mode`` is an ergonomic alias for the transport that ALSO implies
        # ``enabled``.  A config like ``streaming: {mode: auto}`` reads as
        # "turn streaming on, transport=auto" — matching the natural intent
        # of someone enabling streaming without also spelling out
        # ``enabled: true``.  Without this, ``mode`` was silently ignored and
        # streaming stayed disabled (``enabled`` defaults to False), which is
        # a surprising footgun: the whole reply buffers and sends at once.
        # ``mode: off`` disables streaming; an explicit ``enabled`` key always
        # wins so callers can force either state.
        #
        # ``transport`` alone does NOT imply ``enabled``: ``streaming.enabled``
        # is the documented master switch (see website/docs/user-guide/
        # configuration.md), so a bare ``transport`` only selects HOW to stream
        # once streaming is on. Only the ``mode`` alias flips ``enabled``.
        raw_transport = data.get("transport")
        raw_mode = data.get("mode")
        # Normalize both through the same helper so TOML's bare ``off``/``on``
        # (parsed as bool False/True) become canonical tokens rather than
        # ``"false"``/``"true"``.
        picked = raw_transport if raw_transport is not None else raw_mode
        transport = _normalize_transport_token(picked)

        if "enabled" in data:
            enabled = _coerce_bool(data.get("enabled"), False)
        elif raw_mode is not None:
            # The ``mode`` alias (and only ``mode``) infers enabled:
            # ``off`` disables, anything else enables.
            enabled = _normalize_transport_token(raw_mode) != "off"
        else:
            enabled = False

        return cls(
            enabled=enabled,
            transport=transport,
            edit_interval=_coerce_float(
                data.get("edit_interval"), DEFAULT_STREAMING_EDIT_INTERVAL,
            ),
            buffer_threshold=_coerce_int(
                data.get("buffer_threshold"), DEFAULT_STREAMING_BUFFER_THRESHOLD,
            ),
            cursor=data.get("cursor", DEFAULT_STREAMING_CURSOR),
            fresh_final_after_seconds=_coerce_float(
                data.get("fresh_final_after_seconds"), 0.0
            ),
        )


@dataclass
class GatewayConfig:
    """
    Main gateway configuration.

    Manages all platform connections, session policies, and delivery settings.
    """
    # Platform configurations
    platforms: Dict[Platform, PlatformConfig] = field(default_factory=dict)

    # Storage paths
    sessions_dir: Path = field(default_factory=lambda: get_son_of_anton_home() / "sessions")

    # Hours this instance runs agent turns, as [start, end) local hours;
    # wraps past midnight, so (20, 7) is 8pm-7am. None = always active.
    # Outside the window the gateway stays connected and answers with
    # ``inactive_message`` instead of starting a turn — see
    # gateway/active_hours.py for why this is a reply and not a stopped unit.
    active_hours: Optional[tuple] = None
    # Wording for that reply. Empty = the default in active_hours.py.
    inactive_message: str = ""

    # One person owns this instance — their own account's service. Session
    # browsing and /resume then drop the per-chat origin scoping and see the
    # whole account database, the view the terminal already has, so one
    # conversation moves between TUI and messenger freely. The multi-tenant
    # guards stay for shared instances: this is refused at startup unless
    # every connected platform's allowlist names exactly one user (see
    # gateway/single_user.py).
    single_user: bool = False

    # Session isolation in shared chats
    group_sessions_per_user: bool = True  # Isolate group/channel sessions per participant when user IDs are available

    # Opt-in systemd event-loop watchdog. Zero preserves Type=simple and
    # disables sd_notify at runtime.
    systemd_watchdog_seconds: int = 0

    # Streaming configuration
    streaming: StreamingConfig = field(default_factory=StreamingConfig)

    def __post_init__(self) -> None:
        self.systemd_watchdog_seconds = coerce_systemd_watchdog_seconds(
            self.systemd_watchdog_seconds
        )

    def get_connected_platforms(self) -> List[Platform]:
        """Return list of platforms that are enabled and configured.

        Sorted by platform value so the rendered "Connected Platforms" list
        (and the home-channel blocks derived from it) is byte-stable across
        gateway restarts and mid-process platform registration — dict
        insertion order is not a stable contract and a reorder busts the
        prompt cache without any semantic change.
        """
        connected = []
        for platform, config in self.platforms.items():
            if not config.enabled:
                continue
            if platform == Platform.SIGNAL and config.extra.get("http_url"):
                connected.append(platform)
        return sorted(connected, key=lambda p: str(p.value))

    def get_home_channel(self, platform: Platform) -> Optional[HomeChannel]:
        """Get the home channel for a platform.

        A home channel is the default destination for output not tied to a
        chat — cron results, cross-platform messages.

        Signal has no choice to make. Each gateway answers exactly one
        group (``SIGNAL_GROUP_ALLOWED_USERS`` is the routing key that makes one
        instance respond and its siblings stay silent), so that group IS the
        only place it can talk. Asking for it produced a first-turn notice on
        every new Signal session telling the operator to run ``/sethome`` in
        the one conversation the bot already had. Derive it instead.
        """
        config = self.platforms.get(platform)
        if config and config.home_channel:
            return config.home_channel
        if platform == Platform.SIGNAL:
            return self._derived_signal_home_channel()
        return None

    @staticmethod
    def _derived_signal_home_channel() -> Optional[HomeChannel]:
        """The single allowed Signal group as this gateway's home channel.

        Only when exactly one concrete group is allowed. ``*`` (every group)
        and a multi-group allowlist are genuinely ambiguous, and an unset
        allowlist means no group is served at all — those keep returning None
        so the operator's explicit ``/sethome`` is still what decides.

        ``group:`` prefix: that is the chat_id form the Signal adapter builds
        on intake and the form ``send()`` parses back into a groupId. A bare
        id would be treated as a phone number and fail to deliver.
        """
        raw = os.getenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        groups = [part.strip() for part in raw.split(",") if part.strip()]
        if len(groups) != 1 or groups[0] == "*":
            return None
        return HomeChannel(
            platform=Platform.SIGNAL,
            chat_id=f"group:{groups[0]}",
            name="Signal group",
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GatewayConfig":
        """Build from config.toml's ``[gateway]`` table.

        ``platforms`` and ``streaming`` ride in the same dict:
        ``load_gateway_config`` puts the top-level ``[platforms]`` and
        ``[streaming]`` tables there.
        """
        data = _coerce_dict(data)
        platforms = {}
        for platform_name, platform_data in _coerce_dict(data.get("platforms")).items():
            try:
                platforms[Platform(platform_name)] = PlatformConfig.from_dict(platform_data)
            except ValueError:
                pass  # Skip unknown platforms
        return cls(
            platforms=platforms,
            active_hours=parse_hour_window(data.get("active_hours"), None),
            inactive_message=str(data.get("inactive_message") or "").strip(),
            single_user=_coerce_bool(data.get("single_user"), False),
            group_sessions_per_user=_coerce_bool(data.get("group_sessions_per_user"), True),
            systemd_watchdog_seconds=coerce_systemd_watchdog_seconds(
                data.get("systemd_watchdog_seconds")
            ),
            streaming=StreamingConfig.from_dict(data.get("streaming", {})),
        )


def load_gateway_config() -> GatewayConfig:
    """Load the gateway configuration from config.toml and the environment.

    The Nix module writes the gateway's settings under ``[gateway]`` and
    Signal's under ``[platforms.signal]``; SIGNAL_HTTP_URL and SIGNAL_ACCOUNT
    come from the environment and are what enable the platform.
    """
    config_toml_path = get_son_of_anton_home() / "config.toml"
    data: Dict[str, Any] = {}
    try:
        import tomllib
        if config_toml_path.exists():
            with open(config_toml_path, encoding="utf-8") as f:
                toml_cfg = tomllib.loads(f.read()) or {}
            data = dict(_coerce_dict(toml_cfg.get("gateway")))
            data["platforms"] = toml_cfg.get("platforms")
            if isinstance(toml_cfg.get("streaming"), dict):
                data["streaming"] = toml_cfg["streaming"]
    except Exception as e:
        logger.warning(
            "Failed to process config.toml — falling back to defaults. "
            "Check %s for syntax errors. Error: %s",
            config_toml_path,
            e,
        )

    config = GatewayConfig.from_dict(data)

    # SIGNAL_HTTP_URL + SIGNAL_ACCOUNT enable Signal.
    signal_url = os.getenv("SIGNAL_HTTP_URL", "")
    signal_account = os.getenv("SIGNAL_ACCOUNT", "")
    if signal_url and signal_account:
        signal_config = config.platforms.setdefault(Platform.SIGNAL, PlatformConfig())
        signal_config.enabled = True
        signal_config.extra.update({
            "http_url": signal_url,
            "account": signal_account,
        })
    return config
