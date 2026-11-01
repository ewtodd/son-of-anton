"""The Nix memoryRules seed merges into rules.toml three-way, by rule id.

Every row of the seed contract: Nix creates the file and the entries it
declares, follows its own declared value while the on-disk copy is still what
it wrote, never overwrites what someone changed, retires only what is
untouched, and on a first run (no state) never removes anything.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PY = REPO_ROOT / "nix" / "rules_merge.py"


@pytest.fixture(scope="module")
def rm():
    """Import nix/rules_merge.py by path — it ships as a Nix script, not a module."""
    spec = importlib.util.spec_from_file_location("nix_rules_merge", RULES_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rule(rid, text="Keep answers short.", **kw):
    return {"id": rid, "text": text, "scope": "common", "source": "user", **kw}


def written(rm, *rules):
    """The state Nix would have recorded for *rules*."""
    return {r["id"]: rm.canonical(r) for r in rules}


def by_id(result):
    return {r["id"]: r for r in result}


def test_declared_and_missing_is_added_and_recorded(rm) -> None:
    result, state, report = rm.merge_rules([rule("a")], [], None)
    assert by_id(result)["a"]["text"] == "Keep answers short."
    assert "a" in state
    assert any("added a" in line for line in report)


def test_declared_and_still_what_nix_wrote_follows_the_declared_value(rm) -> None:
    old = rule("a", "Old text.")
    disk = [{**old, "added": "2026-11-01"}]
    result, state, _ = rm.merge_rules([rule("a", "New text.")], disk, written(rm, old))
    assert by_id(result)["a"]["text"] == "New text."
    assert by_id(result)["a"]["added"] == "2026-11-01", "user-owned keys are left alone"
    assert state["a"]["text"] == "New text."


def test_declared_but_changed_on_disk_keeps_disk_and_reports(rm) -> None:
    old = rule("a", "Old text.")
    edited = {**old, "text": "Edited by the user."}
    result, state, report = rm.merge_rules([rule("a", "New text.")], [edited], written(rm, old))
    assert by_id(result)["a"]["text"] == "Edited by the user."
    assert any("kept a" in line for line in report)
    assert state["a"]["text"] == "Old text.", "state keeps what Nix last wrote"


def test_dropping_a_declared_why_clears_it_while_untouched(rm) -> None:
    old = rule("a", why="because")
    result, _, _ = rm.merge_rules([rule("a")], [dict(old)], written(rm, old))
    assert "why" not in by_id(result)["a"]


def test_no_longer_declared_and_untouched_is_removed(rm) -> None:
    old = rule("a")
    result, state, report = rm.merge_rules([], [dict(old)], written(rm, old))
    assert result == [] and state == {}
    assert any("removed a" in line for line in report)


def test_no_longer_declared_but_changed_is_kept(rm) -> None:
    old = rule("a")
    edited = {**old, "text": "Rewritten."}
    result, state, report = rm.merge_rules([], [edited], written(rm, old))
    assert by_id(result)["a"]["text"] == "Rewritten."
    assert "a" not in state, "once the user owns it Nix stops tracking it"
    assert any("kept a" in line for line in report)


def test_ids_nix_never_declared_are_untouched(rm) -> None:
    theirs = {"id": "agent-made", "text": "Added by the agent.", "scope": "gateway", "source": "agent"}
    result, _, _ = rm.merge_rules([rule("a")], [theirs], None)
    assert by_id(result)["agent-made"] == theirs
    result, _, _ = rm.merge_rules([], [theirs], written(rm, rule("a")))
    assert by_id(result)["agent-made"] == theirs


def test_first_run_adds_missing_keeps_differing_and_removes_nothing(rm) -> None:
    differing = {"id": "b", "text": "Mine.", "scope": "common", "source": "agent"}
    same = rule("c")
    leftover = rule("old")
    result, state, report = rm.merge_rules(
        [rule("a"), rule("b", "Theirs."), rule("c")], [differing, dict(same), dict(leftover)], None
    )
    ids = by_id(result)
    assert ids["a"] and ids["b"]["text"] == "Mine." and ids["old"]
    assert "b" not in state, "an unattributed value is not recorded as Nix's"
    assert set(state) == {"a", "c"}
    assert any("kept b" in line for line in report)


def test_a_kept_conflict_keeps_being_reported_until_resolved(rm) -> None:
    old = rule("a", "Old text.")
    edited = {**old, "text": "Edited."}
    declared = [rule("a", "New text.")]
    state = written(rm, old)
    for _ in range(2):
        result, state, report = rm.merge_rules(declared, [edited], state)
        assert any("kept a" in line for line in report)
        assert by_id(result)["a"]["text"] == "Edited."


def test_merge_is_idempotent(rm) -> None:
    declared = [rule("a"), rule("b", "Other.")]
    result, state, _ = rm.merge_rules(declared, [], None)
    again, state2, report = rm.merge_rules(declared, result, state)
    assert again == result and state2 == state and report == []


# -- the file glue ---------------------------------------------------------------


def run_main(rm, tmp_path, declared, *, existing=None):
    from utils import dump_toml

    nix = tmp_path / "declared.toml"
    nix.write_text(dump_toml({"rule": declared}), encoding="utf-8")
    rules_path = tmp_path / "memories" / "rules.toml"
    if existing is not None:
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        rules_path.write_text(existing, encoding="utf-8")
    state_path = tmp_path / "memories" / ".nix-rules.json"
    saved = sys.argv
    sys.argv = ["rules_merge", str(nix), str(rules_path), "--state", str(state_path)]
    try:
        assert rm.main() == 0
    finally:
        sys.argv = saved
    return rules_path, state_path


def test_main_creates_the_file_and_the_state(rm, tmp_path) -> None:
    rules_path, state_path = run_main(rm, tmp_path, [rule("a")])
    assert tomllib.loads(rules_path.read_text(encoding="utf-8"))["rule"][0]["id"] == "a"
    assert "a" in json.loads(state_path.read_text(encoding="utf-8"))


def test_main_never_overwrites_a_file_that_does_not_parse(rm, tmp_path) -> None:
    broken = "[[rule]\nid = "
    rules_path, state_path = run_main(rm, tmp_path, [rule("a")], existing=broken)
    assert rules_path.read_text(encoding="utf-8") == broken
    assert not state_path.exists()


def test_main_with_nothing_declared_and_no_history_creates_nothing(rm, tmp_path) -> None:
    rules_path, state_path = run_main(rm, tmp_path, [])
    assert not rules_path.exists() and not state_path.exists()


# -- a declared file that is not trustworthy changes nothing ----------------------


def run_file(rm, tmp_path, value):
    """Seed a good state, then run main against *value*; return the files before/after."""
    run_main(rm, tmp_path, [rule("a"), rule("b", "Other.")])
    rules_path = tmp_path / "memories" / "rules.toml"
    state_path = tmp_path / "memories" / ".nix-rules.json"
    before = (rules_path.read_bytes(), state_path.read_bytes())
    nix = tmp_path / "bad.toml"
    if isinstance(value, bytes):
        nix.write_bytes(value)
    elif value is not None:
        nix.write_text(value, encoding="utf-8")
    saved = sys.argv
    sys.argv = ["rules_merge", str(nix), str(rules_path), "--state", str(state_path)]
    try:
        assert rm.main() == 0
    finally:
        sys.argv = saved
    return before, (rules_path.read_bytes(), state_path.read_bytes())


GOOD = '[[rule]]\nid = "a"\ntext = "Keep answers short."\n'
BAD_DECLARED = {
    "missing": None,
    "unparseable": "[[rule]\nid = ",
    "not utf8": b"\xff\xfe\x00",
    "empty file": "",
    "rule not a list": 'rule = "x"\n',
    "bad id": '[[rule]]\nid = "Bad_ID"\ntext = "t"\n',
    "no id": '[[rule]]\ntext = "t"\n',
    "empty text": '[[rule]]\nid = "a"\ntext = "  "\n',
    "text not string": '[[rule]]\nid = "a"\ntext = 3\n',
    "why not string": '[[rule]]\nid = "a"\ntext = "t"\nwhy = 3\n',
    "bad scope": '[[rule]]\nid = "a"\ntext = "t"\nscope = "everywhere"\n',
    "bad source": '[[rule]]\nid = "a"\ntext = "t"\nsource = "bot"\n',
    "duplicate ids": GOOD + GOOD,
    "one bad among good": GOOD + '[[rule]]\nid = "c"\ntext = ""\n',
}


@pytest.mark.parametrize("kind", list(BAD_DECLARED))
def test_an_unusable_declared_file_changes_nothing(rm, tmp_path, capsys, kind) -> None:
    before, after = run_file(rm, tmp_path, BAD_DECLARED[kind])
    assert after == before, "rules.toml and the state file stay byte-identical"
    assert "left untouched" in capsys.readouterr().err


def test_a_valid_runtime_file_matches_the_equivalent_list(rm, tmp_path) -> None:
    text = (
        '[[rule]]\nid = "a"\ntext = "Keep answers short."\n\n'
        '[[rule]]\nid = "b"\ntext = "Other."\nwhy = "because"\n'
        'scope = "gateway"\nsource = "agent"\n'
    )
    from_file = tmp_path / "file"
    from_list = tmp_path / "list"
    (from_file / "memories").mkdir(parents=True)
    from_list.mkdir()
    (from_file / "declared.toml").write_text(text, encoding="utf-8")
    saved = sys.argv
    sys.argv = [
        "rules_merge",
        str(from_file / "declared.toml"),
        str(from_file / "memories" / "rules.toml"),
        "--state",
        str(from_file / "memories" / ".nix-rules.json"),
    ]
    try:
        assert rm.main() == 0
    finally:
        sys.argv = saved
    run_main(
        rm,
        from_list,
        [rule("a"), rule("b", "Other.", why="because", scope="gateway", source="agent")],
    )
    for name in ("rules.toml", ".nix-rules.json"):
        assert (from_file / "memories" / name).read_text(encoding="utf-8") == (
            from_list / "memories" / name
        ).read_text(encoding="utf-8")
