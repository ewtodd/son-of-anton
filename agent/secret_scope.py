"""Credential resolution by env-var name.

One gateway process serves one ``SON_OF_ANTON_HOME``, so credentials resolve
from a single source: the process environment, into which
``son_of_anton_cli.env_loader.load_son_of_anton_dotenv`` has already merged
``<home>/.env`` at startup.

This module exists as the seam every credential read goes through rather than
calling ``os.getenv`` directly. It previously carried a context-local,
fail-closed *secret scope*: the multiplexing gateway served several profiles
from one process, so a credential read outside a scope could return another
profile's value, and ``get_secret`` raised instead of falling back. With one
home per process there is no other profile to leak from, and the scope, the
multiplex flag, and the global-env allowlist that exempted deployment settings
from it are all gone.
"""
from __future__ import annotations

import os
from typing import Optional


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a credential by env-var name from the process environment."""
    val = os.environ.get(name)
    return val if val is not None else default
