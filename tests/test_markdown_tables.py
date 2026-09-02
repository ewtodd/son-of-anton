"""``agent.markdown_tables`` must not reach outside the declared dependencies.

Column widths used to come from ``wcwidth`` — a ``prompt_toolkit`` transitive
that left the tree when the REPL did (9906a30b).  Nothing declared it, so the
first call afterwards raised ``ModuleNotFoundError``.

That landed hardest on the *steer* path.  Interrupting a turn makes ``chat()``
take its non-streamed branch, which renders the interrupt notice through
``_render_final_assistant_content`` → ``realign_markdown_tables``.  The import
fires before any table is looked for, so **every** mid-turn steer died with
``Error: No module named 'wcwidth'`` whether or not a table was on screen.
Widths now come from ``rich.cells``, which is a core dependency.
"""

from __future__ import annotations

import pytest

from agent.markdown_tables import _disp_width, realign_markdown_tables


def test_widths_come_from_a_declared_dependency() -> None:
    import ast

    import agent.markdown_tables as mt

    tree = ast.parse(open(mt.__file__, encoding="utf-8").read())
    imported = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "wcwidth" not in imported, "wcwidth is not a declared dependency"

    with pytest.raises(ImportError):
        import wcwidth  # noqa: F401  — proves the fix is load-bearing, not incidental


def test_display_width_counts_cells_not_codepoints() -> None:
    assert _disp_width("ab") == 2
    assert _disp_width("日本") == 4, "CJK glyphs occupy two cells each"
    # wcswidth returned -1 for emoji carrying a variation selector and the old
    # code clamped that to 0; rich reports the real width.
    assert _disp_width("⚠️") == 2
    assert _disp_width("\x00") == 0, "control chars must never go negative"


def test_cjk_columns_line_up_after_realignment() -> None:
    table = "| 名前 | b |\n|---|---|\n| 日本語 | 2 |\n"
    lines = realign_markdown_tables(table).splitlines()
    assert len({_disp_width(line) for line in lines}) == 1, lines


def test_the_interrupt_notice_renders_without_wcwidth() -> None:
    """The exact line a steered turn puts on screen.

    ``realign_markdown_tables`` runs on it unconditionally, which is why the
    missing import took down interrupts that had no table anywhere near them.
    """
    from agent.conversation_loop import INTERRUPT_WAITING_FOR_MODEL_PREFIX
    from cli import _render_final_assistant_content

    notice = f"{INTERRUPT_WAITING_FOR_MODEL_PREFIX}3.2s elapsed)."
    for mode in ("render", "strip", "raw"):
        assert _render_final_assistant_content(notice, mode=mode) is not None
