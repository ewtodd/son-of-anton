"""Markdown section helpers."""

import re


def tail_entries(text: str, n: int) -> str:
    """Return the last N '## ' sections from text."""
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    # First part is everything before the first ## section (frontmatter + intro)
    sections = [p for p in parts if p.startswith("## ")]
    if not sections:
        return text
    selected = sections[-n:]
    return "\n".join(selected)
