"""Prompt-size diagnostic: ``son-of-anton prompt-size``.

Reports a byte/char breakdown of the system prompt the agent would build for
a fresh session — system prompt total, the ``<available_skills>`` index,
memory + user profile, and tool-schema JSON. Lets users see where their fixed
prompt budget goes (issue #34667) without parsing a saved session JSON by hand.

The diagnostic builds a real inspection agent (so the numbers match what
actually ships on the wire) but never makes a network call: it passes dummy
credentials so ``AIAgent.__init__`` takes the direct-construction path, then
calls ``build_system_prompt_parts`` / inspects ``agent.tools`` offline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# The skills index is wrapped in this tag pair inside the stable tier.
_SKILLS_BLOCK_RE = re.compile(r"<available_skills>.*?</available_skills>", re.DOTALL)

# A rendered skill entry inside <available_skills> is ``    - name: desc`` (or
# ``    - name`` when the skill has no description). Category headers use two
# leading spaces, so the four-space + ``- `` prefix isolates skill lines.
_SKILL_LINE_PREFIX = "    - "

# Posture-demoted categories render all visible skill names on one shared line.
_NAMES_ONLY_LINE_RE = re.compile(r"^  .+ \[names only\]: (?P<names>.+)$")

# Cap the human-readable "Skills by size" table; ``--json`` always has them all.


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _tool_name(tool: Any) -> str:
    """Return the callable name of a tool schema (OpenAI ``function`` shape)."""
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return str(tool.get("name", ""))


def _skill_md_paths_by_name() -> Dict[str, Path]:
    """Map each installed skill's name to its ``SKILL.md`` path on disk.

    Keyed by both the frontmatter ``name`` (what the index renders) and the
    skill directory name, so either resolves. Local skills win over external
    dirs (``get_all_skills_dirs`` yields local first), matching the index's own
    precedence. Used to attribute the real on-disk read cost per skill.
    """
    from agent.skill_utils import (
        get_all_skills_dirs,
        iter_skill_index_files,
        parse_frontmatter,
    )

    mapping: Dict[str, Path] = {}
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.exists():
            continue
        for skill_file in iter_skill_index_files(skills_dir, "SKILL.md"):
            frontmatter_name = skill_file.parent.name
            try:
                frontmatter, _ = parse_frontmatter(
                    skill_file.read_text(encoding="utf-8")
                )
                frontmatter_name = str(frontmatter.get("name") or frontmatter_name)
            except Exception:
                pass
            # setdefault keeps the first (local) occurrence on name collisions.
            mapping.setdefault(frontmatter_name, skill_file)
            mapping.setdefault(skill_file.parent.name, skill_file)
    return mapping


def _compute_skills_breakdown(skills_block: str) -> List[Dict[str, Any]]:
    """Per-skill byte breakdown parsed from the rendered ``<available_skills>``.

    Two honest, distinct numbers per skill:

    * ``index_line_bytes`` — the skill's attributed bytes in the always-on
      index (the fixed per-call cost of *listing* the skill). For a compact
      ``[names only]`` line, each name keeps its own bytes and receives an
      even share of the category prefix and separators. The attributed bytes
      therefore sum exactly to the shared rendered line.
    * ``skill_md_bytes`` — the on-disk size of the skill's ``SKILL.md`` (the
      real token cost paid only when the model loads it via ``skill_view``).
      ``None`` when the name can't be mapped to a file (e.g. a plugin skill
      whose source lives outside the scanned skill dirs).

    Sorted largest-first by ``skill_md_bytes`` (the read cost that dominates
    pruning decisions), tie-broken by name.
    """
    name_to_path = _skill_md_paths_by_name()
    entries: List[Dict[str, Any]] = []

    def append_entry(
        name: str,
        *,
        attributed_bytes: int,
        total_bytes: int,
        shared_bytes: int,
        skill_count: int,
    ) -> None:
        path = name_to_path.get(name)
        md_bytes: Optional[int] = None
        if path is not None:
            try:
                md_bytes = path.stat().st_size
            except OSError:
                md_bytes = None
        entries.append({
            "name": name,
            "index_line_bytes": attributed_bytes,
            "index_line_total_bytes": total_bytes,
            "index_line_shared_bytes": shared_bytes,
            "index_line_skill_count": skill_count,
            "skill_md_bytes": md_bytes,
            "path": str(path) if path is not None else "",
        })

    for line in skills_block.splitlines():
        compact_match = _NAMES_ONLY_LINE_RE.match(line)
        if compact_match is not None:
            names = [
                name.strip()
                for name in compact_match.group("names").split(",")
                if name.strip()
            ]
            if not names:
                continue
            total_bytes = _bytes(line)
            name_bytes = [_bytes(name) for name in names]
            shared_total = total_bytes - sum(name_bytes)
            shared_base, shared_remainder = divmod(shared_total, len(names))
            for index, name in enumerate(names):
                shared_bytes = shared_base + (1 if index < shared_remainder else 0)
                append_entry(
                    name,
                    attributed_bytes=name_bytes[index] + shared_bytes,
                    total_bytes=total_bytes,
                    shared_bytes=shared_bytes,
                    skill_count=len(names),
                )
            continue

        if not line.startswith(_SKILL_LINE_PREFIX):
            continue
        rest = line[len(_SKILL_LINE_PREFIX):]
        # ``name: desc`` — the first ``": "`` separates name from description.
        # Namespaced names (``codex:rescue``) have no space after their colon,
        # so partitioning on ``": "`` keeps the full name intact.
        name = rest.partition(": ")[0].strip()
        if not name:
            continue
        line_bytes = _bytes(line)
        append_entry(
            name,
            attributed_bytes=line_bytes,
            total_bytes=line_bytes,
            shared_bytes=0,
            skill_count=1,
        )
    entries.sort(key=lambda e: (-(e["skill_md_bytes"] or 0), e["name"]))
    return entries


def _compute_toolsets_breakdown(tools: List[Any]) -> List[Dict[str, Any]]:
    """Per-toolset schema-byte breakdown of the resolved tool list.

    Each tool is attributed to its single canonical toolset from the registry,
    so ``json_bytes`` sums are fully attributable: the grand total equals the
    sum of the individual tool serializations (which is the array total from
    ``tools['json_bytes']`` minus JSON framing of ``2 * count`` bytes). Sorted
    largest-first by ``json_bytes``, tie-broken by toolset name.
    """
    from tools.registry import registry

    tool_to_toolset = registry.get_tool_to_toolset_map()
    groups: Dict[str, Dict[str, Any]] = {}
    for tool in tools:
        name = _tool_name(tool)
        toolset = tool_to_toolset.get(name) or "(unknown)"
        group = groups.setdefault(
            toolset, {"toolset": toolset, "tool_count": 0, "json_bytes": 0}
        )
        group["tool_count"] += 1
        group["json_bytes"] += _bytes(json.dumps(tool, ensure_ascii=False))
    out = list(groups.values())
    out.sort(key=lambda g: (-g["json_bytes"], g["toolset"]))
    return out


