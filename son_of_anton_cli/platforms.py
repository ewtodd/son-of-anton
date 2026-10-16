"""
Shared platform registry for Son of Anton Agent.

Single source of truth for platform metadata consumed by tools_config
(default toolset resolution) and the other platform menus.  Import
``PLATFORMS`` from here instead of maintaining duplicate dicts in each
module.
"""

from collections import OrderedDict
from typing import NamedTuple


class PlatformInfo(NamedTuple):
    """Metadata for a single platform entry."""
    label: str
    default_toolset: str


# Ordered so that TUI menus are deterministic.
PLATFORMS: OrderedDict[str, PlatformInfo] = OrderedDict([
    ("cli",            PlatformInfo(label="🖥️  CLI",            default_toolset="son-of-anton-cli")),
    ("signal",         PlatformInfo(label="📡 Signal",          default_toolset="son-of-anton-signal")),
    ("cron",           PlatformInfo(label="⏰ Cron",            default_toolset="son-of-anton-cron")),
])
