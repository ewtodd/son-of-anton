"""What the physics mode needs from the son-of-anton home, in one place.

The mode used to import its config loading, the home path, the model-context
resolver, the reasoning-effort clamp, the TOML writer and the ``.env`` loader
from the Python agent. That agent is gone; these are the small parts of each
that the physics code relies on, and no more. Until the mode's Go port
(daemon/phases/11), this module is its only contact with the outside.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

import httpx

__all__ = [
    "clamp_effort",
    "context_length",
    "dump_toml",
    "home",
    "load_config",
    "load_dotenv",
    "reasoning_decl",
]


# --- the home and its files ---------------------------------------------------


def home() -> Path:
    """``$SON_OF_ANTON_HOME``, else ``~/.son-of-anton``."""
    raw = os.environ.get("SON_OF_ANTON_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".son-of-anton"


def load_config() -> dict:
    """``<home>/config.toml`` parsed, or ``{}`` when missing or unparseable.

    Read on every call (the file is small, and a long run should see an edit).
    Defaults are not merged in: every physics reader treats an absent key as
    "use the mode's own default".
    """
    import tomllib

    try:
        with open(home() / "config.toml", "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


_CREDENTIAL_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_KEY")


def _parse_env_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        inner = raw[1:-1]
        if raw[0] == '"':
            inner = re.sub(
                r"\\([\\\"nrt])",
                lambda m: {"n": "\n", "r": "\r", "t": "\t"}.get(m.group(1), m.group(1)),
                inner,
            )
        return inner
    return re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()


def load_dotenv() -> list[Path]:
    """Load ``<home>/.env`` (``KEY=value`` lines) into ``os.environ``.

    Its values override variables already set, as the agent's loader did, so
    the model key written to the home wins over a stale shell export. A
    credential-named variable (``*_API_KEY``, ``*_TOKEN``, ``*_SECRET``,
    ``*_KEY``) loses any non-ASCII character, which an HTTP header cannot
    carry. Returns the files loaded.
    """
    path = home() / ".env"
    try:
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or "\x00" in line:
            continue
        value = _parse_env_value(value)
        if key.endswith(_CREDENTIAL_SUFFIXES):
            value = value.encode("ascii", errors="ignore").decode("ascii")
        os.environ[key] = value
    return [path]


# --- the model route ----------------------------------------------------------


def _route(url: Any) -> str:
    """A base URL reduced to what identifies the endpoint."""
    raw = str(url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.rstrip("/")
    if not parts.scheme or not parts.hostname:
        return raw.rstrip("/")
    port = parts.port
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    return f"{parts.scheme.lower()}://{host}{parts.path.rstrip('/')}"


def _declared_model(model: str, base_url: Optional[str], config: Optional[dict]) -> dict:
    """The ``models.<model>`` table of the custom provider serving *base_url*.

    Providers are the entries of ``custom_providers`` (a table keyed by name,
    or a list) and of ``providers``; one with no ``base_url`` / ``url`` /
    ``api`` is skipped, and with *base_url* given only the entry for that
    route matches.
    """
    cfg = load_config() if config is None else config
    entries: list = []
    for section in (cfg.get("custom_providers"), cfg.get("providers")):
        if isinstance(section, dict):
            entries.extend(section.values())
        elif isinstance(section, list):
            entries.extend(section)
    target = _route(base_url) if base_url else ""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_url = next(
            (str(entry[k]) for k in ("base_url", "url", "api") if entry.get(k)), ""
        )
        if not entry_url or (target and _route(entry_url) != target):
            continue
        models = entry.get("models")
        declared = models.get(model) if isinstance(models, dict) else None
        if isinstance(declared, dict):
            return declared
    return {}


def reasoning_decl(model: str, base_url: Optional[str], config: Optional[dict] = None) -> dict:
    """``{"efforts": (...)}`` from the route's ``reasoning_efforts``, or ``{}``.

    That is the only part of the declaration the physics mode reads (it clamps
    its configured effort onto the endpoint's vocabulary).
    """
    if not model:
        return {}
    declared = _declared_model(model, base_url, config).get("reasoning_efforts")
    if not isinstance(declared, (list, tuple)):
        return {}
    levels = tuple(
        str(level).strip().lower()
        for level in declared
        if isinstance(level, (str, int)) and str(level).strip()
    )
    return {"efforts": levels} if levels else {}


_EFFORT_LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def clamp_effort(effort: Optional[str], supported: Optional[Sequence[str]]) -> Optional[str]:
    """Map a requested reasoning effort onto an endpoint's supported levels.

    A supported level, a level not on the ladder, or an unknown set passes
    through. Otherwise the nearest weaker supported level, and when none is
    weaker, the weakest one. ``none`` is never a target for an enabled ask.
    """
    requested = str(effort or "").strip().lower()
    if not requested or not supported:
        return effort
    allowed = [
        s for s in (str(level).strip().lower() for level in supported) if s in _EFFORT_LADDER
    ]
    if not allowed or requested in allowed or requested not in _EFFORT_LADDER:
        return effort
    candidates = [level for level in allowed if level != "none"]
    if not candidates:
        return effort
    rank = _EFFORT_LADDER.index
    below = [level for level in candidates if rank(level) < rank(requested)]
    return max(below, key=rank) if below else min(candidates, key=rank)


_CONTEXT_KEYS = (
    "n_ctx",
    "context_length",
    "context_window",
    "max_model_len",
    "max_context_length",
    "max_tokens",
    "n_ctx_train",
)


def _positive(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def context_length(
    model: str, base_url: str = "", api_key: str = "", config: Optional[dict] = None
) -> int:
    """The model's context window in tokens, or 0 when nothing says.

    The route's declared ``context_length`` first, then what the endpoint's
    ``/models`` lists for the model (a lone listed model stands in for any
    name, as on llama.cpp). Never guesses: the caller bounds a run by its own
    assumed window when this is 0.
    """
    if not model:
        return 0
    declared = _positive(_declared_model(model, base_url or None, config).get("context_length"))
    if declared or not base_url:
        return declared
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        reply = httpx.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=3.0)
        listed = reply.json().get("data") if reply.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return 0
    if not isinstance(listed, list):
        return 0
    entries = [m for m in listed if isinstance(m, dict)]
    matched = next((m for m in entries if m.get("id") == model), None)
    if matched is None and len(entries) == 1:
        matched = entries[0]
    if matched is None:
        return 0
    for source in (matched, matched.get("meta")):
        if isinstance(source, dict):
            for key in _CONTEXT_KEYS:
                if _positive(source.get(key)):
                    return int(source[key])
    return 0


# --- TOML out -----------------------------------------------------------------

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _key(key: Any) -> str:
    name = str(key)
    return name if _BARE_KEY.fullmatch(name) else json.dumps(name, ensure_ascii=False)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            return "nan"
        if value in (float("inf"), float("-inf")):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value if item is not None) + "]"
    raise TypeError(f"cannot serialize {type(value).__name__} to TOML: {value!r}")


def _is_tables(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(e, dict) for e in value)


def _table(lines: list, prefix: list, table: dict) -> None:
    # Scalars and arrays before sub-tables, as TOML requires.
    for key, value in table.items():
        if value is None or isinstance(value, dict) or _is_tables(value):
            continue
        if isinstance(value, list) and any(isinstance(e, dict) for e in value):
            raise TypeError(
                f"TOML cannot mix tables and values in array {'.'.join(prefix + [str(key)])!r}"
            )
        lines.append(f"{_key(key)} = {_value(value)}")
    for key, value in table.items():
        path = prefix + [str(key)]
        dotted = ".".join(_key(part) for part in path)
        if isinstance(value, dict):
            lines.extend(["", f"[{dotted}]"])
            _table(lines, path, value)
        elif _is_tables(value):
            for element in value:
                lines.extend(["", f"[[{dotted}]]"])
                _table(lines, path, element)


def dump_toml(data: dict) -> str:
    """A mapping as TOML text. ``None`` values are dropped; anything TOML
    cannot hold raises ``TypeError`` rather than writing a document that
    reloads differently. Key order is kept."""
    if not isinstance(data, dict):
        raise TypeError("TOML document root must be a mapping")
    lines: list = []
    _table(lines, [], data)
    while lines and lines[0] == "":
        lines.pop(0)
    return "\n".join(lines) + "\n" if lines else ""
