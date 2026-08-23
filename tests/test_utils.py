"""Glyph-strip and terminal-backend helper contracts.

The fork strips decorative emoji from user-facing display strings while
keeping the ⚛ brand mark, geometric shapes (logo art / progress bars),
kaomoji faces, and arrows. The local terminal backend's working directory
contract is the process's own directory — ``terminal.cwd`` must not
redirect it.
"""

from __future__ import annotations

from utils import is_local_terminal_backend, strip_decorative_glyphs


def test_strip_decorative_glyphs_removes_decoration() -> None:
    assert strip_decorative_glyphs("⚠ 2 commits behind ✓").strip() == "2 commits behind"
    out = strip_decorative_glyphs("🔎 preparing search_files…")
    assert "🔎" not in out
    assert "preparing search_files…" in out
    assert strip_decorative_glyphs("⏲ 7s ✦ ✨ 🎉 💡").strip() == "7s"


def test_strip_decorative_glyphs_keeps_brand_and_structure() -> None:
    kept = "⚛ ◈ ◉ ░ ◕‿◕ → ├─ ┊"
    assert strip_decorative_glyphs(kept) == kept


def test_strip_decorative_glyphs_passes_ascii_through() -> None:
    plain = "Model switched: deepseek-v4-flash"
    assert strip_decorative_glyphs(plain) == plain
    assert strip_decorative_glyphs("") == ""


def test_is_local_terminal_backend_defaults_true() -> None:
    assert is_local_terminal_backend(None) is True
    assert is_local_terminal_backend("not-a-dict") is True
    assert is_local_terminal_backend({}) is True
    assert is_local_terminal_backend({"backend": "local"}) is True
    assert is_local_terminal_backend({"env_type": "local"}) is True


def test_is_local_terminal_backend_detects_remote() -> None:
    assert is_local_terminal_backend({"backend": "ssh"}) is False
    assert is_local_terminal_backend({"env_type": "docker"}) is False
