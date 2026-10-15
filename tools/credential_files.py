"""File passthrough registry for remote terminal backends.

Remote backends (e.g. SSH) create sandboxes with no host files.
This module ensures that credential files and host-side cache directories
(documents, images, audio, screenshots) are mounted or synced into those
sandboxes so the agent can access them.

**Credentials** — user config (``terminal.credential_files``).

**Cache directories** — gateway-cached uploads, browser screenshots, TTS
audio, and processed images.  Mounted read-only so the remote terminal can
reference files the host side created (e.g. ``unzip`` an uploaded archive).

Remote backends call :func:`get_credential_file_mounts` and
:func:`get_cache_directory_mounts` / :func:`iter_cache_files` at sandbox
creation time and before each command (for resync).
"""

from __future__ import annotations

import logging
import os
import posixpath
from pathlib import Path
from typing import Dict, List, Optional
from son_of_anton_cli.config import cfg_get

logger = logging.getLogger(__name__)

# Cache for config-based file list (loaded once per process).
_config_files: List[Dict[str, str]] | None = None


def _resolve_son_of_anton_home() -> Path:
    from son_of_anton_constants import get_son_of_anton_home
    return get_son_of_anton_home()


def _load_config_files() -> List[Dict[str, str]]:
    """Load ``terminal.credential_files`` from config.toml (cached)."""
    global _config_files
    if _config_files is not None:
        return _config_files

    result: List[Dict[str, str]] = []
    try:
        from son_of_anton_cli.config import read_raw_config
        son_of_anton_home = _resolve_son_of_anton_home()
        cfg = read_raw_config()
        cred_files = cfg_get(cfg, "terminal", "credential_files")
        if isinstance(cred_files, list):
            from tools.path_security import validate_within_dir

            for item in cred_files:
                if isinstance(item, str) and item.strip():
                    rel = item.strip()
                    if os.path.isabs(rel):
                        logger.warning(
                            "credential_files: rejected absolute config path %r", rel,
                        )
                        continue
                    host_path = son_of_anton_home / rel
                    containment_error = validate_within_dir(host_path, son_of_anton_home)
                    if containment_error:
                        logger.warning(
                            "credential_files: rejected config path traversal %r (%s)",
                            rel, containment_error,
                        )
                        continue
                    resolved_path = host_path.resolve()
                    if resolved_path.is_file():
                        container_path = f"/root/.son-of-anton/{rel}"
                        result.append({
                            "host_path": str(resolved_path),
                            "container_path": container_path,
                        })
    except Exception as e:
        logger.warning("Could not read terminal.credential_files from config: %s", e)

    _config_files = result
    return _config_files


def get_credential_file_mounts() -> List[Dict[str, str]]:
    """Return all credential files that should be mounted into remote sandboxes.

    Each item has ``host_path`` and ``container_path`` keys, from the user's
    ``terminal.credential_files`` config.
    """
    mounts: Dict[str, str] = {}
    for entry in _load_config_files():
        cp = entry["container_path"]
        if cp not in mounts and Path(entry["host_path"]).is_file():
            mounts[cp] = entry["host_path"]

    return [
        {"host_path": hp, "container_path": cp}
        for cp, hp in mounts.items()
    ]


# ---------------------------------------------------------------------------
# Cache directory mounts (documents, images, audio, videos, screenshots)
# ---------------------------------------------------------------------------

# The cache subdirectories that should be mirrored into remote backends.
# Each tuple is (new_subpath, old_name) matching son_of_anton_constants.get_son_of_anton_dir().
_CACHE_DIRS: list[tuple[str, str]] = [
    ("cache/documents", "document_cache"),
    ("cache/images", "image_cache"),
    ("cache/audio", "audio_cache"),
    ("cache/videos", "video_cache"),
    ("cache/screenshots", "browser_screenshots"),
    ("cache/web", "web_cache"),
    ("cache/delegation", "delegation_cache"),
    # Oversized tool results (tools/tool_result_storage.py). Host-side is the
    # single canonical location; mounting/syncing it lets remote backends
    # read spilled results at the translated path instead of needing a
    # separate in-sandbox copy.
    ("cache/spillover", "cache/spillover"),
    # Desktop/clipboard/PDF uploads land in the flat top-level ``images/`` dir
    # (tui_gateway attach RPCs), not under ``cache/``. Mount it so vision can
    # reach uploads inside sandbox containers (#69575). No legacy alias exists,
    # so both tuple slots are ``images``.
    ("images", "images"),
    # Desktop non-image file attachments (tui_gateway ``file.attach`` staging)
    # land in the flat top-level ``attachments/`` dir. Mount it so the agent's
    # file tools can read dropped binaries (zip/pdf/...) from inside sandbox
    # containers instead of dangling host paths (#76577).
    ("attachments", "attachments"),
]


def get_cache_directory_mounts(
    container_base: str = "/root/.son-of-anton",
) -> List[Dict[str, str]]:
    """Return mount entries for each cache directory that exists on disk.

    Used by backends that bind-mount cache directories.  Each entry has
    ``host_path`` and ``container_path`` keys.  The host path is resolved via
    ``get_son_of_anton_dir()`` for backward compatibility with old directory layouts.
    """
    from son_of_anton_constants import get_son_of_anton_dir

    mounts: List[Dict[str, str]] = []
    for new_subpath, old_name in _CACHE_DIRS:
        host_dir = get_son_of_anton_dir(new_subpath, old_name)
        if not host_dir.is_dir():
            # Create missing staging dirs instead of skipping them: a
            # directory that appears later (first desktop attachment, first
            # clipboard image) would otherwise dangle at sync time
            # (#76577). An empty mounted dir costs nothing; a missing
            # mount costs the feature. get_son_of_anton_dir() already resolved
            # new-vs-legacy layout, so creating its answer cannot shadow a
            # populated legacy dir.
            try:
                host_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue  # unwritable home (tests, RO mounts) — skip as before
        # Always map to the *new* container layout regardless of host layout.
        container_path = f"{container_base.rstrip('/')}/{new_subpath}"
        mounts.append({
            "host_path": str(host_dir),
            "container_path": container_path,
        })
    return mounts


def map_cache_path_to_container(
    host_path: str,
    container_base: str = "/root/.son-of-anton",
) -> Optional[str]:
    """Map a host cache path to its mounted path under *container_base*.

    Returns the POSIX container path when *host_path* lives under one of the
    auto-mounted cache directories, otherwise ``None``.  Backend-agnostic: the
    caller decides which ``container_base`` applies (SSH ``<remote_home>/.son-of-anton``,
    etc.) and whether translation is wanted.
    Always joins with ``posixpath`` because container/remote paths are POSIX
    regardless of the host OS.
    """
    path = Path(host_path)
    for mount in get_cache_directory_mounts(container_base=container_base):
        host_dir = Path(mount["host_path"])
        try:
            rel = path.relative_to(host_dir)
        except ValueError:
            continue
        return posixpath.join(mount["container_path"], rel.as_posix())
    return None


def from_agent_visible_cache_path(
    container_path: str,
    container_base: str = "/root/.son-of-anton",
) -> str:
    """Translate a sandbox/remote cache path back to its host path.

    Inverse of :func:`to_agent_visible_cache_path`. Returns the input
    unchanged — local and SSH backends have no container-side cache mirror,
    so there is nothing to translate; the caller then treats a still-remote
    path as "no host file" and falls back to an in-sandbox read.
    """
    return container_path


def to_agent_visible_cache_path(
    host_path: str,
    container_base: str = "/root/.son-of-anton",
) -> str:
    """Translate a host cache path to its mounted path inside the sandbox.

    Returns the input unchanged if it is not under any auto-mounted cache
    directory, or if the active terminal backend does not require path
    translation (local).

    Per-backend base (mirrors ``_agent_cache_base_for_env`` in
    tools/image_generation_tool.py, the proven heuristics for where each
    backend's Son of Anton cache lands):

    * ssh — file-synced under the remote user's home; ``~/.son-of-anton`` is
      shell-expanded by the remote shell, so tool commands resolve it
      regardless of the actual remote home.

    Backend is identified by TERMINAL_ENV (same env var
    tools/terminal_tool.py reads in _get_environment_config).
    """
    backend = (os.environ.get("TERMINAL_ENV") or "local").strip().lower()
    if backend == "ssh":
        container_base = "~/.son-of-anton"
    else:
        return host_path  # local / unknown: host path is correct

    mapped = map_cache_path_to_container(host_path, container_base=container_base)
    return mapped if mapped is not None else host_path


def iter_cache_files(
    container_base: str = "/root/.son-of-anton",
) -> List[Dict[str, str]]:
    """Return individual (host_path, container_path) entries for cache files.

    Used by backends that upload files individually and resync before each
    command.  Skips symlinks.  The container paths use the new ``cache/<subdir>``
    layout.
    """
    from son_of_anton_constants import get_son_of_anton_dir

    result: List[Dict[str, str]] = []
    for new_subpath, old_name in _CACHE_DIRS:
        host_dir = get_son_of_anton_dir(new_subpath, old_name)
        if not host_dir.is_dir():
            continue
        container_root = f"{container_base.rstrip('/')}/{new_subpath}"
        for item in host_dir.rglob("*"):
            if item.is_symlink() or not item.is_file():
                continue
            rel = item.relative_to(host_dir)
            result.append({
                "host_path": str(item),
                "container_path": f"{container_root}/{rel}",
            })
    return result
