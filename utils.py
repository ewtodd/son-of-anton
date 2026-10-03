"""Shared utility functions for son-of-anton."""

import datetime
import errno
import json
import logging
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Union
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on"})


# Decorative glyphs removed from user-facing display strings (CLI notices,
# status bar, tool feed, gateway chat replies). Coverage: emoji blocks,
# misc symbols (⚠ ⚡ ⛔), dingbats (✓ ✗ ✦ ✨), clocks and misc technical
# (⏱ ⏲ ⏳), and emoji variation selectors. Kept deliberately: the ⚛ brand
# mark, geometric shapes (◈ ◉ ░ — logo art and progress bars), kaomoji
# faces, and arrows (→), which carry structure rather than decoration.
_DECORATIVE_GLYPH_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2300-\u23FF\u2600-\u269A\u269C-\u26FF"
    "\u2700-\u27BF\uFE0F]+"
)


def strip_decorative_glyphs(text: str) -> str:
    """Remove decorative emoji/symbol glyphs from *text*.

    Runs of stripped glyphs collapse to nothing; surrounding whitespace is
    left to the caller's original layout. Safe on plain ASCII (returns the
    string unchanged) and never raises.
    """
    if not text:
        return text
    try:
        return _DECORATIVE_GLYPH_RE.sub("", text)
    except Exception:
        return text


def is_local_terminal_backend(terminal_cfg: Any) -> bool:
    """True when a ``terminal:`` config block names the local backend.

    Unset / empty / ``"local"`` all mean local (the default) — the working
    directory contract for local terminals is the process's own directory,
    so ``terminal.cwd`` must not redirect it. Accepts both the documented
    ``backend`` key and the legacy ``env_type`` alias.
    """
    if not isinstance(terminal_cfg, dict):
        return True
    backend = str(
        terminal_cfg.get("backend") or terminal_cfg.get("env_type") or ""
    ).strip().lower()
    return not backend or backend == "local"


def is_truthy_value(value: Any, default: bool = False) -> bool:
    """Coerce bool-ish values using the project's shared truthy string set."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY_STRINGS
    return bool(value)


def env_var_enabled(name: str, default: str = "") -> bool:
    """Return True when an environment variable is set to a truthy value."""
    return is_truthy_value(os.getenv(name, default), default=False)


def _preserve_file_mode(path: Path) -> "int | None":
    """Capture the permission bits of *path* if it exists, else ``None``."""
    try:
        return stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    except OSError:
        return None


def _preserve_file_owner(path: Path) -> "tuple[int, int] | None":
    """Capture the owning uid/gid of *path*."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_uid, st.st_gid


def _restore_file_owner(path: Path, owner: "tuple[int, int] | None") -> None:
    """Re-apply uid/gid after an atomic replace when permitted.

    ``os.replace`` swaps in the temp file's owner, so a root-run config write
    can leave ``config.toml`` owned by root. Best-effort chown preserves the
    existing owner for privileged callers and is harmless for unprivileged
    callers that cannot chown.
    """
    if owner is None or not hasattr(os, "chown"):
        return
    try:
        os.chown(path, owner[0], owner[1])
    except OSError:
        pass


def _restore_file_mode(path: Path, mode: "int | None") -> None:
    """Re-apply *mode* to *path* after an atomic replace.

    ``tempfile.mkstemp`` creates files with 0o600 (owner-only).  After
    ``os.replace`` swaps the temp file into place the target inherits
    those restrictive permissions, breaking Docker / NAS volume mounts
    that rely on broader permissions set by the user.  Calling this
    right after ``os.replace`` restores the original permissions.
    """
    if mode is None:
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _copy_fallback(tmp_str: str, real_path: str) -> None:
    """Copy/fsync/unlink fallback for cross-device and bind-mount renames."""
    shutil.copyfile(tmp_str, real_path)
    try:
        shutil.copystat(tmp_str, real_path)
    except OSError:
        pass
    try:
        with open(real_path, "rb") as f:
            os.fsync(f.fileno())
    except OSError:
        pass
    os.unlink(tmp_str)


def atomic_replace(tmp_path: Union[str, Path], target: Union[str, Path]) -> str:
    """Atomically move *tmp_path* onto *target*, preserving symlinks.

    ``os.replace(tmp, target)`` atomically swaps ``tmp`` into place at
    ``target``.  When ``target`` is a symlink, the symlink itself is
    replaced with a regular file — silently detaching managed deployments
    that symlink ``config.toml`` / ``SOUL.md`` / ``auth.json`` etc. from
    ``~/.son-of-anton/`` to a git-tracked profile package or dotfiles repo
    (GitHub #16743).

    This helper resolves the symlink first so ``os.replace`` writes to
    the real file in-place while the symlink survives.  For non-symlink
    and non-existent paths the behavior is identical to a plain
    ``os.replace`` call unless the rename fails with ``EXDEV`` / ``EBUSY``
    (any platform) — cross-device, bind-mount, and busy-file deployments
    fall back to copy/fsync/unlink immediately.  These never clear on
    retry.

    Returns the resolved real path used for the replace, so callers that
    need to re-apply permissions can target it instead of the symlink.
    """
    target_str = str(target)
    real_path = os.path.realpath(target_str) if os.path.islink(target_str) else target_str
    tmp_str = str(tmp_path)
    try:
        os.replace(tmp_str, real_path)
        return real_path
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EBUSY):
            raise
        _copy_fallback(tmp_str, real_path)
    return real_path


def atomic_write_text(
    path: Union[str, Path],
    content: str,
    *,
    encoding: str = "utf-8",
    tmp_prefix: str = ".tmp_",
    preserve_mode: bool = False,
    create_mode: "int | None" = None,
) -> None:
    """Write *content* to *path* via temp file + fsync + atomic rename.

    Ensures the target file is never left in a partially-written state if
    the process crashes or is interrupted.  ``atomic_replace`` preserves
    symlinks and handles cross-device / busy-file fallbacks.

    Used by the memory store, skill manager, and agent importer so that
    every destructive file rewrite in the codebase shares one implementation.

    Args:
        preserve_mode: When True, carry an existing target's permission bits
            and (POSIX, best-effort) owner across the replace, like
            ``atomic_toml_write`` does unconditionally.  ``os.replace`` swaps
            in mkstemp's 0600 temp file owned by the writing user, so without
            this a root-run rewrite of a user-owned file flips its owner and
            tightens its mode.  The mode is applied to the temp fd *before*
            the replace, so the file never transits through 0600.  Off by
            default: the historical callers (memory store, skill manager,
            cron) own their 0600-is-fine files.
        create_mode: Permission bits to apply when the target does not yet
            exist (otherwise the new file keeps mkstemp's 0600).  Never
            applied to an existing file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    original_mode = _preserve_file_mode(path) if preserve_mode else None
    original_owner = _preserve_file_owner(path) if preserve_mode else None
    effective_mode = original_mode
    if effective_mode is None and create_mode is not None and not path.exists():
        effective_mode = create_mode

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=tmp_prefix, suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            if effective_mode is not None:
                # fchmod the temp fd BEFORE the replace so the target never
                # transits through mkstemp's 0600.
                os.fchmod(handle.fileno(), effective_mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        real_path = atomic_replace(tmp_path, path)
        if preserve_mode:
            _restore_file_owner(Path(real_path), original_owner)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_json_write(
    path: Union[str, Path],
    data: Any,
    *,
    indent: int = 2,
    mode: int | None = None,
    **dump_kwargs: Any,
) -> None:
    """Write JSON data to a file atomically.

    Uses temp file + fsync + os.replace to ensure the target file is never
    left in a partially-written state. If the process crashes mid-write,
    the previous version of the file remains intact.

    Args:
        path: Target file path (will be created or overwritten).
        data: JSON-serializable data to write.
        indent: JSON indentation (default 2).
        mode: Optional final permission mode. When set, the temp file is
            created and replaced with this mode, avoiding chmod-after-write
            TOCTOU exposure for secret-bearing files.
        **dump_kwargs: Additional keyword args forwarded to json.dump(), such
            as default=str for non-native types.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    original_mode = None if mode is not None else _preserve_file_mode(path)
    original_owner = _preserve_file_owner(path)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        if mode is not None:
            # Apply the mode to the temp fd BEFORE the replace so the target
            # never transits through mkstemp's 0600.
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                indent=indent,
                ensure_ascii=False,
                **dump_kwargs,
            )
            f.flush()
            os.fsync(f.fileno())
        # Preserve symlinks — swap in-place on the real file (GitHub #16743).
        real_path = atomic_replace(tmp_path, path)
        real_path_obj = Path(real_path)
        _restore_file_owner(real_path_obj, original_owner)
        if mode is not None:
            try:
                os.chmod(real_path_obj, mode)
            except OSError:
                pass
        else:
            _restore_file_mode(real_path_obj, original_mode)
    except BaseException:
        # Intentionally catch BaseException so temp-file cleanup still runs for
        # KeyboardInterrupt/SystemExit before re-raising the original signal.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def warn_if_credential_file_broadly_readable(
    path: Union[str, Path],
    *,
    label: str = "",
    log: logging.Logger | None = None,
) -> bool:
    """Warn (once per call) when a credential file is group/world-readable.

    Secret-bearing files that users create by hand (or that older Son of Anton
    versions wrote without an explicit mode) commonly end up 0o644 under the
    default umask. This helper is the shared read-time check for that class:
    call it before loading any token/credential file so the owner gets a
    remediation hint in the logs.

    Returns True when a warning was emitted. No-ops (returns False) on
    platforms without POSIX permission bits semantics (best effort), when the
    file is missing, or when permissions are already tight.
    """
    p = Path(path)
    _log = log or logger
    try:
        file_mode = p.stat().st_mode
    except OSError:
        return False
    if not (file_mode & (stat.S_IRGRP | stat.S_IROTH)):
        return False
    _log.warning(
        "%s%s is group/world-readable (mode 0%o) and contains secrets. "
        "Run: chmod 600 %s",
        f"{label} " if label else "",
        p.name,
        stat.S_IMODE(file_mode),
        p,
    )
    return True


# ─── TOML support ─────────────────────────────────────────────────────────────
#
# TOML is the preferred format for user-authored configuration, skins, and
# problem specs; TOML stays readable for one deprecation window. Reading is
# stdlib-only (``tomllib``); writing needs a small serializer because there is
# no stdlib TOML writer. ``None`` has no TOML representation, so null mapping
# values and null list elements are dropped on write.

_BARE_TOML_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(key: Any) -> str:
    name = str(key)
    if _BARE_TOML_KEY_RE.fullmatch(name):
        return name
    return json.dumps(name, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            return "nan"
        if value == float("inf"):
            return "inf"
        if value == float("-inf"):
            return "-inf"
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(
            _toml_value(item) for item in value if item is not None
        ) + "]"
    raise TypeError(
        f"cannot serialize {type(value).__name__} to TOML: {value!r}"
    )


def _is_array_of_tables(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(element, dict) for element in value)
    )


def _dump_toml_table(lines: list, prefix: list, table: dict) -> None:
    # TOML requires every scalar/array key of a table before its sub-tables,
    # so emit in two passes instead of insertion order.
    for key, value in table.items():
        if value is None or isinstance(value, dict) or _is_array_of_tables(value):
            continue
        if isinstance(value, list) and any(
            isinstance(element, dict) for element in value
        ):
            raise TypeError(
                "TOML cannot mix tables and values in array "
                f"{'.'.join(prefix + [str(key)])!r}"
            )
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, value in table.items():
        if value is None:
            continue
        path = prefix + [str(key)]
        if isinstance(value, dict):
            lines.append("")
            lines.append(f"[{'.'.join(_toml_key(part) for part in path)}]")
            _dump_toml_table(lines, path, value)
        elif _is_array_of_tables(value):
            for element in value:
                lines.append("")
                lines.append(
                    f"[[{'.'.join(_toml_key(part) for part in path)}]]"
                )
                _dump_toml_table(lines, path, element)


def dump_toml(data: dict) -> str:
    """Serialize a plain mapping to TOML text.

    ``None`` values cannot be represented in TOML and are dropped. Any other
    unsupported value raises ``TypeError`` rather than writing a document that
    reloads differently. Key order is preserved; scalar keys precede
    sub-tables as TOML requires.
    """
    if not isinstance(data, dict):
        raise TypeError("TOML document root must be a mapping")
    lines: list = []
    _dump_toml_table(lines, [], data)
    while lines and lines[0] == "":
        lines.pop(0)
    return "\n".join(lines) + "\n" if lines else ""


def atomic_toml_write(
    path: Union[str, Path],
    data: Any,
    *,
    extra_content: str | None = None,
    create_mode: "int | None" = None,
) -> None:
    """Write TOML to a file atomically.

    Uses temp file + fsync + os.replace to ensure the target file is never
    left in a partially-written state. If the process crashes mid-write, the
    previous version of the file remains intact.

    Args:
        path: Target file path (will be created or overwritten).
        data: TOML-serializable mapping to write.
        extra_content: Optional comment block appended after the document.
            Lines must already start with ``#`` (TOML comment syntax).
        create_mode: Permission bits to apply when the target does not yet
            exist (a created file otherwise keeps mkstemp's 0600).  Never
            applied to an existing file, whose mode is always preserved.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    original_mode = _preserve_file_mode(path)
    original_owner = _preserve_file_owner(path)
    if original_mode is None and create_mode is not None and not path.exists():
        original_mode = create_mode

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if original_mode is not None:
                # Apply the mode to the temp fd BEFORE the replace so the
                # target never transits through mkstemp's 0600.
                os.fchmod(f.fileno(), original_mode)
            f.write(dump_toml(data))
            if extra_content:
                f.write(extra_content)
            f.flush()
            os.fsync(f.fileno())
        # Preserve symlinks — swap in-place on the real file (GitHub #16743).
        real_path = atomic_replace(tmp_path, path)
        real_path_obj = Path(real_path)
        _restore_file_owner(real_path_obj, original_owner)
        _restore_file_mode(real_path_obj, original_mode)
    except BaseException:
        # Match atomic_json_write: cleanup must also happen for process-level
        # interruptions before we re-raise them.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_toml_update(
    path: Union[str, Path],
    key_path: str,
    value: Any,
) -> None:
    """Set one dotted key in a TOML document, rewriting the whole file.

    There is no comment-preserving TOML writer in the stdlib (and no tomlkit
    dependency), so a single-key edit rewrites the document; comments are not
    preserved. Uses the same temp file + fsync + atomic replace pattern as
    :func:`atomic_toml_write`.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = load_toml_file(path)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a TOML mapping")
    current = data
    keys = key_path.split(".")
    for key in keys[:-1]:
        next_value = current.get(key)
        if not isinstance(next_value, dict):
            next_value = {}
            current[key] = next_value
        current = next_value
    current[keys[-1]] = value
    atomic_toml_write(path, data)


def atomic_toml_save(
    path: Union[str, Path],
    new_state: dict,
) -> None:
    """Persist a full config-state mapping as TOML.

    The old ruamel round-trip saver preserved comments; TOML has no stdlib
    equivalent, so the document is rewritten wholesale from *new_state* and
    comments are lost. Keeps the fail-closed guard against clobbering an
    existing-but-unreadable config file. The import is lazy because
    ``son_of_anton_cli.config`` imports from this module.
    """
    from son_of_anton_cli.config import require_readable_config_before_write

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require_readable_config_before_write(path)
    atomic_toml_write(path, new_state)


# ─── JSON Helpers ─────────────────────────────────────────────────────────────


def safe_json_loads(text: str, default: Any = None) -> Any:
    """Parse JSON, returning *default* on any parse error.

    Replaces the ``try: json.loads(x) except (JSONDecodeError, TypeError)``
    pattern duplicated across display.py, anthropic_adapter.py,
    auxiliary_client.py, and others.
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


# ── TOML loading ────────────────────────────────────────────────────────
#
# tomllib is stdlib and C-accelerated; there is no slower fallback path. One
# parse entry point keeps config, manifests, problem specs and markdown
# frontmatter on identical semantics.


def fast_toml_load(stream: Any) -> Any:
    """Parse TOML from a ``str``/``bytes`` document or a readable file object."""
    import tomllib

    payload = stream.read() if hasattr(stream, "read") else stream
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    return tomllib.loads(payload)


def load_toml_file(path: Union[str, Path]) -> Any:
    """Read a TOML file into plain Python data, or ``None`` when absent."""
    path = Path(path)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return fast_toml_load(handle)


# ─── Environment Variable Helpers ─────────────────────────────────────────────


def env_int(key: str, default: int = 0) -> int:
    """Read an environment variable as an integer, with fallback."""
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def env_float(key: str, default: float = 0.0) -> float:
    """Read an environment variable as a float, with fallback."""
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def env_bool(key: str, default: bool = False) -> bool:
    """Read an environment variable as a boolean."""
    return is_truthy_value(os.getenv(key, ""), default=default)


# ─── Proxy Helpers ────────────────────────────────────────────────────────────


_PROXY_ENV_KEYS = (
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
    "https_proxy", "http_proxy", "all_proxy",
)


def normalize_proxy_url(proxy_url: str | None) -> str | None:
    """Normalize proxy URLs for httpx/aiohttp compatibility.

    WSL/Clash-style environments often export SOCKS proxies as
    ``socks://127.0.0.1:PORT``. httpx rejects that alias and expects the
    explicit ``socks5://`` scheme instead.
    """
    candidate = str(proxy_url or "").strip()
    if not candidate:
        return None
    if candidate.lower().startswith("socks://"):
        return f"socks5://{candidate[len('socks://'):]}"
    return candidate


def normalize_proxy_env_vars() -> None:
    """Rewrite supported proxy env vars to canonical URL forms in-place."""
    for key in _PROXY_ENV_KEYS:
        value = os.getenv(key, "")
        normalized = normalize_proxy_url(value)
        if normalized and normalized != value:
            os.environ[key] = normalized


# ─── URL Parsing Helpers ──────────────────────────────────────────────────────


def base_url_hostname(base_url: str) -> str:
    """Return the lowercased hostname for a base URL, or ``""`` if absent.

    Use exact-hostname comparisons against known provider hosts
    (``api.openai.com``, ``api.x.ai``, ``api.anthropic.com``) instead of
    substring matches on the raw URL. Substring checks treat attacker- or
    proxy-controlled paths/hosts like ``https://api.openai.com.example/v1``
    or ``https://proxy.test/api.openai.com/v1`` as native endpoints, which
    leads to wrong api_mode / auth routing.
    """
    raw = (base_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    return (parsed.hostname or "").lower().rstrip(".")


# ─── Model Capability Detection ──────────────────────────────────────────────


def model_forces_max_completion_tokens(model: str) -> bool:
    """Return True for model families that require ``max_completion_tokens``.

    OpenAI's newer families reject ``max_tokens`` on /v1/chat/completions with
    HTTP 400 ``unsupported_parameter`` — the caller must send
    ``max_completion_tokens`` instead. This covers:

    - ``gpt-4o`` / ``gpt-4o-mini`` / ``gpt-4o-*``
    - ``gpt-4.1`` / ``gpt-4.1-*``
    - ``gpt-5`` / ``gpt-5.x`` / ``gpt-5-*``
    - ``o1`` / ``o1-*``
    - ``o3`` / ``o3-*``
    - ``o4`` / ``o4-*``

    Handles vendor prefixes like ``openai/gpt-5.4`` by stripping to the tail.
    The URL-based check (``base_url_hostname == "api.openai.com"``) misses
    third-party OpenAI-compatible endpoints (custom OpenAI gateways,
    OpenRouter) that front these models and enforce the same parameter
    constraint, so name-based detection is required as a fallback.
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    if "/" in m:
        m = m.rsplit("/", 1)[-1]
    return (
        m.startswith("gpt-4o")
        or m.startswith("gpt-4.1")
        or m.startswith("gpt-5")
        or m.startswith("o1")
        or m.startswith("o3")
        or m.startswith("o4")
    )


def base_url_host_matches(base_url: str, domain: str) -> bool:
    """Return True when the base URL's hostname is ``domain`` or a subdomain.

    Safer counterpart to ``domain in base_url``, which is the substring
    false-positive class documented on ``base_url_hostname``. Accepts bare
    hosts, full URLs, and URLs with paths.

        base_url_host_matches("https://api.moonshot.ai/v1", "moonshot.ai") == True
        base_url_host_matches("https://moonshot.ai", "moonshot.ai")        == True
        base_url_host_matches("https://evil.com/moonshot.ai/v1", "moonshot.ai") == False
        base_url_host_matches("https://moonshot.ai.evil/v1", "moonshot.ai")     == False
    """
    hostname = base_url_hostname(base_url)
    if not hostname:
        return False
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        return False
    return hostname == domain or hostname.endswith("." + domain)
