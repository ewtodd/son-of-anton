"""Prompt completion contracts (son_of_anton_tui/completion.py).

The engine is pure: these drive it with the real command registry and a real
directory tree, and hold it to the parser that expands ``@`` references at
send time — a completed reference that the parser reads differently is a bug.
"""

from __future__ import annotations

import os
import shutil
import threading
import time

import pytest

from agent.context_references import parse_context_references
from son_of_anton_cli.commands import COMMANDS_BY_CATEGORY, SUBCOMMANDS, resolve_command
from son_of_anton_tui import completion as C

PALETTE = [(cmd, cat, desc) for cat, cmds in COMMANDS_BY_CATEGORY.items() for cmd, desc in cmds.items()]
OFFERED = {row[0] for row in PALETTE}


def _complete(before: str, *, explicit: bool = False, cwd: str = ".", files=None, **kw):
    return C.complete(
        before,
        explicit=explicit,
        commands=PALETTE,
        cwd=cwd,
        project_files=(lambda _cwd: list(files)) if files is not None else None,
        **kw,
    )


def _inserts(comps) -> list[str]:
    return [c.insert for c in comps.items] if comps else []


def _subs_for(cmd: str) -> list[str]:
    return list(SUBCOMMANDS.get(f"/{resolve_command(cmd).name}") or ())


# ---------------------------------------------------------------- commands
def test_slash_alone_offers_every_command_and_an_exact_unique_name_offers_nothing() -> None:
    comps = _complete("/")
    assert {c.display for c in comps.items} <= OFFERED
    assert len(comps.items) == min(C.LIMIT, len(OFFERED))
    unique = next(cmd for cmd in OFFERED if sum(o.lower().startswith(cmd.lower()) for o in OFFERED) == 1)
    assert _complete(unique) is None


def test_every_offered_command_completes_its_arguments_while_typing() -> None:
    checked = 0
    for cmd in sorted(OFFERED):
        for sub in _subs_for(cmd):
            got = _inserts(_complete(f"{cmd} {sub[0]}"))
            assert f"{sub} " in got, f"{cmd} {sub[0]}… should offer {sub}"
            checked += 1
    assert checked, "the registry should declare some arguments"


def test_an_empty_argument_completes_on_tab_only_so_enter_still_sends() -> None:
    for cmd in sorted(OFFERED):
        subs = _subs_for(cmd)
        if not subs:
            continue
        assert _complete(f"{cmd} ") is None, f"typing '{cmd} ' must not open a list"
        assert set(_inserts(_complete(f"{cmd} ", explicit=True))) >= {f"{s} " for s in subs}


def test_commands_this_surface_does_not_offer_complete_nothing() -> None:
    for key in SUBCOMMANDS:
        if key not in OFFERED:
            assert _complete(f"{key} ", explicit=True) is None, key


def test_a_typed_argument_is_not_offered_back() -> None:
    cmd = next(c for c in sorted(OFFERED) if _subs_for(c))
    sub = _subs_for(cmd)[0]
    assert f"{sub} " not in _inserts(_complete(f"{cmd} {sub}"))


def test_arg_sources_take_over_a_command_and_fail_soft() -> None:
    def boom(completed, word):
        raise RuntimeError("config unreadable")

    cmd = next(c for c in sorted(OFFERED) if _subs_for(c))
    canonical = f"/{resolve_command(cmd).name}"
    fake = {canonical: lambda completed, word: [C.Candidate("zz ", "zz")]}
    assert _inserts(_complete(f"{cmd} ", explicit=True, arg_sources=fake)) == ["zz "]
    assert _complete(f"{cmd} ", explicit=True, arg_sources={canonical: boom}) is None


# ---------------------------------------------------------------- @ references
@pytest.fixture
def tree(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "main.py").write_text("print()\n", encoding="utf-8")
    (tmp_path / "my dir").mkdir()
    (tmp_path / "my dir" / "a b.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("n\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    return tmp_path


def _parsed(insert: str):
    refs = parse_context_references(f"look at {insert.strip()} please")
    assert len(refs) == 1, insert
    return refs[0].kind, refs[0].target


def test_bare_at_offers_the_reference_kinds_and_recent_files(tree) -> None:
    comps = _complete("@", cwd=str(tree), files=["src/main.py"])
    got = _inserts(comps)
    assert {"@diff ", "@staged ", "@file:", "@folder:", "@git:", "@url:"} <= set(got)
    assert "@file:src/main.py " in got
    assert comps.auto


def test_file_references_list_the_directory_and_parse_back(tree) -> None:
    comps = _complete("@file:", cwd=str(tree))
    by_display = {c.display: c for c in comps.items}
    assert by_display["src/"].insert == "@file:src/", "a directory keeps going"
    assert _parsed(by_display["notes.txt"].insert) == ("file", "notes.txt")
    assert ".env" not in by_display, "dotfiles only once the name starts with '.'"
    assert ".env" in {c.display for c in _complete("@file:.", cwd=str(tree)).items}
    inside = _complete("@file:src/", cwd=str(tree))
    assert _parsed(next(c.insert for c in inside.items if c.display == "main.py")) == ("file", "src/main.py")


def test_folder_references_offer_directories_only(tree) -> None:
    comps = _complete("@folder:", cwd=str(tree))
    assert comps.items and all(c.insert.endswith("/") or c.insert.endswith('/"') for c in comps.items)
    kind, target = _parsed(next(c.insert for c in _complete("@folder:sr", cwd=str(tree)).items))
    assert (kind, target.rstrip("/")) == ("folder", "src")


def test_a_path_with_spaces_is_quoted_and_completes_inside_the_quote(tree) -> None:
    opened = next(c.insert for c in _complete("@file:my", cwd=str(tree)).items)
    assert opened.startswith('@file:"my dir/')
    inside = _complete(f"see {opened}", cwd=str(tree))
    assert inside.start == len("see ")
    final = next(c.insert for c in inside.items if c.display == "a b.txt")
    assert _parsed(final) == ("file", "my dir/a b.txt")


def test_complete_references_are_left_alone(tree) -> None:
    for word in ("@diff", "thanks @staged", "@git:3", "@url:https://example.com/x"):
        assert _complete(word, cwd=str(tree), files=["diff.py", "staged.txt"]) is None, word


def test_fuzzy_files_while_typing_need_a_substring_but_tab_takes_letters_in_order(tree) -> None:
    files = ["tools/file_operations.py", "agent/other.py"]
    assert _inserts(_complete("@operations", files=files))[0] == "@file:tools/file_operations.py "
    assert "@file:tools/file_operations.py " not in _inserts(_complete("@fops", files=files))
    assert "@file:tools/file_operations.py " in _inserts(_complete("@fops", explicit=True, files=files))


# ---------------------------------------------------------------- paths
def test_paths_complete_on_tab_only_and_keep_the_typed_form(tree, monkeypatch) -> None:
    assert _complete("look at ./no", cwd=str(tree)) is None, "prose has slashes: Tab only"
    assert _inserts(_complete("look at ./no", explicit=True, cwd=str(tree))) == ["./notes.txt "]
    assert _inserts(_complete("see src/m", explicit=True, cwd=str(tree))) == ["src/main.py "]
    assert "src/pkg/" in _inserts(_complete("see src/", explicit=True, cwd=str(tree)))
    monkeypatch.setenv("HOME", str(tree))
    assert _inserts(_complete("open ~/no", explicit=True, cwd="/")) == ["~/notes.txt "]


def test_urls_are_never_paths(tree) -> None:
    assert _complete("read https://example.com/no", explicit=True, cwd=str(tree)) is None


def test_an_absolute_path_at_the_start_still_completes(tree) -> None:
    got = _inserts(_complete(f"{tree}/no", explicit=True))
    assert got == [f"{tree}/notes.txt "]


# ---------------------------------------------------------------- applying
def test_apply_replaces_only_the_word_and_keeps_what_follows() -> None:
    text = "/reasoning hi and more"
    cursor = len("/reasoning hi")
    comps = _complete(text[:cursor])
    new, at = C.apply(text, cursor, comps, comps.items[0])
    assert new == f"/reasoning {comps.items[0].insert.strip()} and more", "no doubled space"
    assert new[:at].endswith(comps.items[0].insert.strip())


# ---------------------------------------------------------------- project files
def test_project_files_never_block_and_report_when_loaded() -> None:
    gate = threading.Event()
    landed = threading.Event()

    def loader(cwd):
        gate.wait(5)
        return ["a.py"]

    pf = C.ProjectFiles(on_update=lambda cwd: landed.set(), loader=loader)
    t0 = time.monotonic()
    assert pf("/x") == []
    assert time.monotonic() - t0 < 1.0
    gate.set()
    assert landed.wait(5)
    assert pf("/x") == ["a.py"]


def test_list_project_files_newest_first(tmp_path) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not on PATH")
    old, new = tmp_path / "old.py", tmp_path / "new.py"
    old.write_text("", encoding="utf-8")
    new.write_text("", encoding="utf-8")
    os.utime(old, (1_000_000, 1_000_000))
    assert C.list_project_files(str(tmp_path)) == ["new.py", "old.py"]
