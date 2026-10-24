"""Content search with a glob-style pattern (``*word*``) finds the word instead of failing.

A leading ``*`` is a regex parse error in rg; models write it to mean
"contains". The search runs the real local backend and rg.
"""

from __future__ import annotations

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations, _glob_style_literal


def test_only_end_wildcards_qualify() -> None:
    assert _glob_style_literal("*landlord*") == "landlord"
    assert _glob_style_literal("**landlord") == "landlord"
    # Valid regexes and anything with an inner wildcard stay regexes.
    assert _glob_style_literal("landlord*") is None
    assert _glob_style_literal("land.*lord") is None
    assert _glob_style_literal("*land*lord*") is None
    assert _glob_style_literal("*") is None


@pytest.mark.linux_only
def test_glob_style_content_search_finds_the_word(tmp_path) -> None:
    (tmp_path / "misc.md").write_text("- Buy a bike light.\n- Call the landlord on Friday.\n", encoding="utf-8")
    (tmp_path / "work.md").write_text("- Email the advisor.\n", encoding="utf-8")
    ops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path), timeout=30))

    result = ops.search("*landlord*", path=str(tmp_path), target="content")

    assert result.error is None
    assert [(m.path.rsplit("/", 1)[-1], m.line_number) for m in result.matches] == [("misc.md", 2)]
    assert "literal text 'landlord'" in (result.warning or "")
