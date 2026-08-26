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

``load_env_file`` parses a ``.env`` into a plain dict without touching
``os.environ`` — still used by callers that need to read a file's contents
rather than the live environment.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a credential by env-var name from the process environment."""
    val = os.environ.get(name)
    return val if val is not None else default


def _strip_inline_comment(value: str) -> str:
    """Strip a dotenv-style inline comment from a raw ``.env`` value.

    Mirrors python-dotenv (1.2.2) semantics, verified empirically:

    - Quoted values: scan for the matching close quote
      (backslash-escape-aware for double quotes, since ``save_env_value``
      writes ``\\"``/``\\\\`` escapes). Everything through the close quote is
      kept; a trailing ``# ...`` remainder after it is discarded, so
      ``KEY="has # inside" # trailing`` yields ``has # inside``. Non-comment
      trailing junk leaves the value untouched (lenient, unlike dotenv's
      hard parse error).
    - Unquoted values: truncate only at a ``#`` PRECEDED BY WHITESPACE, so
      ``KEY=foo#bar`` keeps ``foo#bar`` while ``KEY=value # comment`` keeps
      ``value``. A value that *starts* with ``#`` (``KEY=#leading``) is kept.
    """
    value = value.strip()
    if not value:
        return value
    quote = value[0]
    if quote in ("'", '"'):
        i = 1
        while i < len(value):
            ch = value[i]
            if quote == '"' and ch == "\\":
                i += 2  # skip the escaped character
                continue
            if ch == quote:
                remainder = value[i + 1:].lstrip()
                if remainder.startswith("#"):
                    return value[: i + 1]
                return value
            i += 1
        return value  # unterminated quote: leave as-is
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def load_env_file(env_path: Path) -> Dict[str, str]:
    """Parse a ``.env`` file into a plain dict WITHOUT touching ``os.environ``.

    Used to load a profile's secrets into an isolated mapping for
    ``set_secret_scope``. Parses the small KEY=VALUE subset Son of Anton writes
    itself (``export`` prefix, ``#`` comments — full-line and
    dotenv-compatible inline, matching quotes with the
    writer's ``\\"``/``\\\\`` escapes reversed — the same semantics as
    ``son_of_anton_cli.config._parse_env_value``) but never mutates the process
    environment — that isolation is the whole point.

    Encoding is ``utf-8-sig`` so a leading UTF-8 BOM (Windows Notepad /
    PowerShell ``Set-Content -Encoding UTF8``) does not prefix the first
    key as ``\\ufeffNAME`` and make ``get_secret('NAME')`` miss under scope.
    """
    secrets: Dict[str, str] = {}
    try:
        text = env_path.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return secrets

    # Parse values with the canonical Son of Anton parser: save_env_value
    # escapes " and \ inside double quotes, and every other reader
    # (load_env, python-dotenv) reverses those escapes. Stripping only
    # the outer quotes here would corrupt credentials containing "
    # or \ — they work interactively but fail in scoped (cron /
    # multiplex) resolution.
    from son_of_anton_cli.config import _parse_env_value

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        secrets[key] = _parse_env_value(_strip_inline_comment(value))

    return secrets
