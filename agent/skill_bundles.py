"""Skill bundles — aliases that load multiple skills under one slash command.

A skill bundle is a small TOML file that names a set of skills to load
together. Invoking ``/<bundle-name>`` from the CLI or gateway loads every
referenced skill's full content into a single user message, the same way
``/<skill-name>`` does — but for N skills at once.

Storage
-------
Bundles live in ``~/.son-of-anton/skill-bundles/*.toml`` (and the equivalent
profile-aware directory under ``SON_OF_ANTON_HOME``). Each file looks like::

    name: backend-dev
    description: Backend feature work — code review, testing, PR workflow.
    skills:
      - github-code-review
      - test-driven-development
      - github-pr-workflow
    instruction: |
      Optional extra guidance to inject above the skill bodies.

The file's stem is treated as a fallback name when ``name:`` is absent, so
dropping a TOML into the directory is enough to register a new bundle.

Conflict resolution
-------------------
If a bundle and a skill share the same slash name, the bundle wins. The
slash command dispatch checks bundles first, then falls back to skills.
This is the intended behavior — a user who names a bundle ``research``
explicitly wants ``/research`` to mean their bundle, not whatever skill
happens to share the slug.

Public API
----------
- :func:`get_skill_bundles` — return ``{"/slug": bundle_info}``
- :func:`resolve_bundle_command_key` — map a user-typed command to its slug
- :func:`build_bundle_invocation_message` — produce the full user message
- :func:`reload_bundles` — re-scan disk and return a diff
- :func:`list_bundles` — return rich info for display (``son-of-anton bundles``)
- :func:`save_bundle` / :func:`delete_bundle` — file-level operations
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from son_of_anton_constants import get_son_of_anton_home

logger = logging.getLogger(__name__)

# Slug normalization — matches agent/skill_commands.py so a bundle and a
# skill called "Foo Bar" both resolve to "/foo-bar".
_BUNDLE_INVALID_CHARS = re.compile(r"[^a-z0-9-]")
_BUNDLE_MULTI_HYPHEN = re.compile(r"-{2,}")

_bundles_cache: Dict[str, Dict[str, Any]] = {}
_bundles_cache_mtime: Optional[float] = None


def _bundles_dir() -> Path:
    """Return the canonical bundles directory under SON_OF_ANTON_HOME.

    Honors ``SON_OF_ANTON_BUNDLES_DIR`` for tests; falls back to
    ``<SON_OF_ANTON_HOME>/skill-bundles``.
    """
    override = os.environ.get("SON_OF_ANTON_BUNDLES_DIR")
    if override:
        return Path(override).expanduser()
    return get_son_of_anton_home() / "skill-bundles"


def _slugify(name: str) -> str:
    cmd = name.lower().replace(" ", "-").replace("_", "-")
    cmd = _BUNDLE_INVALID_CHARS.sub("", cmd)
    cmd = _BUNDLE_MULTI_HYPHEN.sub("-", cmd).strip("-")
    return cmd


def _iter_bundle_files() -> List[Path]:
    base = _bundles_dir()
    try:
        if not base.exists():
            return []
    except PermissionError:
        # A stale/cross-user SON_OF_ANTON_HOME (e.g. `su` between accounts)
        # makes the dir untraversable; bundles are convenience aliases, so
        # treat the region as empty rather than crashing every CLI invocation.
        logger.warning(
            "Cannot read skill bundles directory %s — treating it as empty.",
            base,
        )
        return []
    files: List[Path] = []
    for ext in ("*.toml",):
        files.extend(sorted(base.glob(ext)))
    return files


def _max_mtime(files: List[Path]) -> float:
    """Highest mtime across the bundle files plus the dir itself.

    Watching the directory mtime catches deletions; watching individual
    files catches edits. Together they're a cheap freshness check.
    """
    base = _bundles_dir()
    mtimes = []
    try:
        base_exists = base.exists()
    except PermissionError:
        base_exists = False
        logger.warning(
            "Cannot stat skill bundles directory %s — treating it as empty.",
            base,
        )
    if base_exists:
        try:
            mtimes.append(base.stat().st_mtime)
        except OSError:
            pass
    for f in files:
        try:
            mtimes.append(f.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else 0.0


def _load_bundle_file(path: Path) -> Optional[Dict[str, Any]]:
    """Parse a single bundle TOML file. Returns ``None`` on any error.

    Errors are logged at WARNING level. We don't raise — a broken bundle
    shouldn't take down slash command discovery.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read bundle %s: %s", path, exc)
        return None
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        logger.warning("Invalid TOML in bundle %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("Bundle %s is not a mapping; skipping", path)
        return None

    name = str(data.get("name") or path.stem).strip()
    if not name:
        logger.warning("Bundle %s has no name; skipping", path)
        return None

    skills = data.get("skills") or []
    if not isinstance(skills, list) or not skills:
        logger.warning("Bundle %s has no skills list; skipping", path)
        return None
    skills = [str(s).strip() for s in skills if str(s).strip()]
    if not skills:
        logger.warning("Bundle %s has empty skills list; skipping", path)
        return None

    description = str(data.get("description") or "").strip()
    instruction = str(data.get("instruction") or "").strip()

    slug = _slugify(name)
    if not slug:
        logger.warning("Bundle %s yielded empty slug; skipping", path)
        return None

    return {
        "name": name,
        "slug": slug,
        "description": description or f"Load {len(skills)} skills as a bundle",
        "skills": skills,
        "instruction": instruction,
        "path": str(path),
    }


def scan_bundles() -> Dict[str, Dict[str, Any]]:
    """Scan the bundles directory and rebuild the cache.

    Returns the same mapping as :func:`get_skill_bundles` — ``"/slug"`` →
    bundle info dict. Later bundles with a duplicate slug are skipped with
    a warning (first wins, alphabetical order).
    """
    global _bundles_cache, _bundles_cache_mtime
    files = _iter_bundle_files()
    out: Dict[str, Dict[str, Any]] = {}
    for f in files:
        info = _load_bundle_file(f)
        if not info:
            continue
        key = f"/{info['slug']}"
        if key in out:
            logger.warning(
                "Duplicate bundle slug %s from %s; keeping %s",
                key, f, out[key]["path"],
            )
            continue
        out[key] = info
    _bundles_cache = out
    _bundles_cache_mtime = _max_mtime(files)
    return out


def get_skill_bundles() -> Dict[str, Dict[str, Any]]:
    """Return the current bundle mapping, rescanning when disk changed.

    Cheap to call repeatedly: only rescans when the bundles directory or
    any bundle file's mtime is newer than the cached snapshot.
    """
    files = _iter_bundle_files()
    current_mtime = _max_mtime(files)
    if not _bundles_cache or _bundles_cache_mtime != current_mtime:
        scan_bundles()
    return _bundles_cache


def resolve_bundle_command_key(command: str) -> Optional[str]:
    """Resolve a user-typed command to its canonical bundle slash key.

    Hyphens and underscores are treated interchangeably to mirror the
    skill-command behavior (Telegram converts hyphens to underscores in
    bot command names).
    """
    if not command:
        return None
    cmd_key = f"/{command.replace('_', '-')}"
    return cmd_key if cmd_key in get_skill_bundles() else None


def list_bundles() -> List[Dict[str, Any]]:
    """Return a sorted list of bundle info dicts for display."""
    bundles = get_skill_bundles()
    return sorted(bundles.values(), key=lambda b: b["slug"])


def build_bundle_invocation_message(
    cmd_key: str,
    user_instruction: str = "",
    task_id: str | None = None,
    platform: str | None = None,
) -> Optional[Tuple[str, List[str], List[str]]]:
    """Build the user message content for a bundle slash command invocation.

    Returns ``(message, loaded_skill_names, missing_skill_names)`` or
    ``None`` if the bundle wasn't found.

    A bundle that references skills the user doesn't have installed still
    loads — the agent gets a note about which ones were skipped. This is
    the same forgiving stance ``build_preloaded_skills_prompt`` uses for
    ``-s`` CLI preloading.

    Disabled skills are also skipped: bundles load members via
    ``_load_skill_payload`` directly, bypassing the scan-time disabled
    filter in ``get_skill_commands()``, so the disabled list must be
    re-applied here.  ``platform`` scopes the check to a specific
    platform's ``skills.platform_disabled`` config (gateway dispatch
    passes it explicitly because the gateway handles multiple platforms
    in one process); when *None*, the platform resolves from session env
    vars and the global disabled list still applies.  Mirrors the
    stacked-skill gate in gateway dispatch (#58888).
    """
    bundles = get_skill_bundles()
    info = bundles.get(cmd_key)
    if not info:
        return None

    # Late import to avoid pulling tools/* at module import time and to
    # keep skill_bundles cheap to import in test environments.
    from agent.skill_commands import _load_skill_payload, _build_skill_message

    try:
        from agent.skill_utils import get_disabled_skill_names
        disabled_names = get_disabled_skill_names(platform=platform)
    except Exception:
        disabled_names = set()

    loaded_names: List[str] = []
    missing: List[str] = []
    disabled: List[str] = []
    skill_blocks: List[str] = []
    seen: set[str] = set()

    bundle_name = info["name"]
    skills = info["skills"]
    extra_instruction = info.get("instruction") or ""

    for skill_id in skills:
        identifier = (skill_id or "").strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)

        loaded = _load_skill_payload(identifier, task_id=task_id)
        if not loaded:
            missing.append(identifier)
            continue
        loaded_skill, skill_dir, skill_name = loaded

        # Per-platform / global disabled gate. Checked against the loaded
        # skill's canonical name (identifiers may be paths or aliases).
        if skill_name in disabled_names or identifier in disabled_names:
            disabled.append(skill_name or identifier)
            continue

        try:
            from tools.skill_usage import bump_use
            bump_use(skill_name, task_id=task_id)
        except Exception:
            pass

        activation_note = (
            f'[Loaded as part of the "{bundle_name}" skill bundle.]'
        )
        skill_blocks.append(
            _build_skill_message(
                loaded_skill,
                skill_dir,
                activation_note,
                session_id=task_id,
            )
        )
        loaded_names.append(skill_name)

    if not skill_blocks:
        return None

    # Header — tells the agent this is a bundle, lists the skills, and
    # provides any author-supplied instruction.
    header_lines = [
        f'[IMPORTANT: The user has invoked the "{bundle_name}" skill bundle, '
        f"loading {len(loaded_names)} skills together. Treat every skill below "
        "as active guidance for this turn.]",
        "",
        f"Bundle: {bundle_name}",
        f"Skills loaded: {', '.join(loaded_names)}",
    ]
    if missing:
        header_lines.append(f"Skills missing (skipped): {', '.join(missing)}")
    if disabled:
        header_lines.append(
            f"Skills disabled for this platform (skipped): {', '.join(disabled)}"
        )
    if extra_instruction:
        header_lines.extend(["", f"Bundle instruction: {extra_instruction}"])
    if user_instruction:
        header_lines.extend(
            ["", f"User instruction: {user_instruction}"]
        )

    header = "\n".join(header_lines)
    return ("\n\n".join([header, *skill_blocks]), loaded_names, missing)


# ---------------------------------------------------------------------------
# File-level CRUD helpers — used by `son-of-anton bundles` CLI subcommand.
# ---------------------------------------------------------------------------


