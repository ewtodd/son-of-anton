"""Markdown parsing with TOML frontmatter support."""

import re
import tomllib



FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Shared critique regex constants


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split text into (frontmatter_dict, body). Returns ({}, text) on failure."""
    # Strip code fences wrapping frontmatter (LLMs sometimes emit ```toml\n---\n...\n---\n```)
    stripped = re.sub(r"^```\w*\s*\n", "", text)
    stripped = re.sub(r"\n```\s*(?:\n|$)", "\n", stripped, count=1)
    match = FRONTMATTER_RE.match(stripped)
    if match:
        text = stripped
    else:
        match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    toml_str = match.group(1)
    body = text[match.end() :]
    try:
        meta = tomllib.loads(toml_str)
        if not isinstance(meta, dict):
            meta = {}
    except tomllib.TOMLDecodeError:
        meta = _fallback_parse(toml_str)
    return meta, body


def _fallback_parse(toml_str: str) -> dict:
    """Regex fallback for broken TOML frontmatter."""
    result = {}
    for line in toml_str.strip().splitlines():
        m = re.match(r"^(\w[\w_]*)\s*=\s*(.+)$", line)
        if m:
            key = m.group(1)
            val = m.group(2).strip().strip('"').strip("'")
            # Try to parse as int
            try:
                val = int(val)
            except (ValueError, TypeError):
                pass
            result[key] = val
    return result


def tail_entries(text: str, n: int) -> str:
    """Return the last N '## ' sections from text."""
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    # First part is everything before the first ## section (frontmatter + intro)
    sections = [p for p in parts if p.startswith("## ")]
    if not sections:
        return text
    selected = sections[-n:]
    return "\n".join(selected)


# --- Nested bracket flattening ---


# --- Computation log parsing and stall detection ---


# ER/WH section detection — matches both ## headers and **bold** line-start formats.
# LLMs sometimes write **ER-001 — Title** instead of ## ER-001 — Title.


