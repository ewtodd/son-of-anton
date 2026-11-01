"""Rules (standing, approved, in the prompt) and facts (retrieved, not in the prompt).

Behavior contracts: a rule never lands without the user's approval, a full store
refuses instead of evicting, entries are addressed by id, the prompt block is
frozen for the session, and a file that does not parse is never overwritten.
Real store and config against a temp SON_OF_ANTON_HOME.
"""

from __future__ import annotations

import json
import tomllib
from types import SimpleNamespace

import pytest

from son_of_anton_cli.write_approval_commands import handle_pending_subcommand
from tools import write_approval as wa
from tools.memory_tool import (
    ScopedMemoryStore,
    find_similar_facts,
    make_entry_id,
    memory_tool,
)
from tools.terminal_tool import set_approval_callback

RULE = "Ask before deleting any mail."


def _dir(home):
    return home / "memories"


def _file(home, name, key):
    path = _dir(home) / name
    return tomllib.loads(path.read_text(encoding="utf-8")).get(key, []) if path.exists() else []


def _store(scope="common", **kw) -> ScopedMemoryStore:
    store = ScopedMemoryStore(active_scope=scope, **kw)
    store.load_from_disk()
    return store


def _call(store, **kw) -> dict:
    return json.loads(memory_tool(store=store, **kw))


def _approve(store, result) -> str:
    assert result.get("staged") is True, result
    return handle_pending_subcommand(wa.MEMORY, ["approve", result["pending_id"]], memory_store=store)


def _add_rule(store, text=RULE, **kw) -> str:
    """Add a rule the way a user would: staged, then approved. Returns the rule id."""
    before = {r["id"] for r in _file_rules()}
    out = _approve(store, _call(store, kind="rule", action="add", text=text, **kw))
    assert "Approved 1" in out, out
    return ({r["id"] for r in _file_rules()} - before).pop()


def _file_rules():
    from son_of_anton_constants import get_son_of_anton_home

    return _file(get_son_of_anton_home(), "rules.toml", "rule")


@pytest.fixture(autouse=True)
def _no_inline_prompt():
    set_approval_callback(None)
    yield
    set_approval_callback(None)


def _set_gate(enabled: bool) -> None:
    from son_of_anton_cli.config import save_config

    save_config({"memory": {"write_approval": enabled}})


# -- approval ---------------------------------------------------------------


@pytest.mark.parametrize("gate", [False, True])
def test_a_rule_write_never_lands_without_approval(son_of_anton_home, gate) -> None:
    _set_gate(gate)
    store = _store()
    result = _call(store, kind="rule", action="add", text=RULE)
    assert result.get("staged") is True
    assert _file_rules() == []
    assert wa.pending_count(wa.MEMORY) == 1


def test_rule_replace_and_remove_are_gated_too(son_of_anton_home) -> None:
    _set_gate(False)
    store = _store()
    rid = _add_rule(store)
    for kw in ({"action": "replace", "text": "Ask first."}, {"action": "remove"}):
        result = _call(store, kind="rule", id=rid, **kw)
        assert result.get("staged") is True
    assert [r["text"] for r in _file_rules()] == [RULE]


def test_an_inline_approval_lands_the_rule_and_a_denial_does_not(son_of_anton_home) -> None:
    _set_gate(False)
    store = _store()
    set_approval_callback(lambda command, description, allow_permanent=False: "deny")
    denied = _call(store, kind="rule", action="add", text=RULE)
    assert denied.get("success") is not True and _file_rules() == []
    set_approval_callback(lambda command, description, allow_permanent=False: "once")
    approved = _call(store, kind="rule", action="add", text=RULE)
    assert approved.get("success") is True and [r["text"] for r in _file_rules()] == [RULE]


def test_facts_follow_the_configured_gate(son_of_anton_home) -> None:
    _set_gate(False)
    store = _store()
    assert _call(store, kind="fact", action="add", text="The NAS is at 10.0.0.5.")["success"]
    _set_gate(True)
    staged = _call(store, kind="fact", action="add", text="The printer is at 10.0.0.9.")
    assert staged.get("staged") is True
    assert [f["text"] for f in _file(son_of_anton_home, "facts.toml", "fact")] == ["The NAS is at 10.0.0.5."]


def test_an_old_shape_staged_payload_is_rejected_on_approve(son_of_anton_home) -> None:
    record = wa.stage_write(
        wa.MEMORY, {"action": "add", "target": "memory", "content": "old shape"}, summary="old"
    )
    out = handle_pending_subcommand(
        wa.MEMORY, ["approve", record["id"]], memory_store=_store()
    )
    assert "predates" in out
    assert not (_dir(son_of_anton_home) / "facts.toml").exists()


# -- cap --------------------------------------------------------------------


def test_a_full_rules_store_refuses_and_keeps_every_rule(son_of_anton_home) -> None:
    store = _store(rules_char_limit=130)
    first = _add_rule(store, "Never delete Proton mail.")
    second = _add_rule(store, "Use metric units in every answer.")
    before = _file_rules()

    result = _call(store, kind="rule", action="add", text="Always answer in formal English prose.")

    assert result.get("success") is False and "retire" in result["error"]
    assert wa.pending_count(wa.MEMORY) == 0, "a refused add must not be staged"
    assert _file_rules() == before
    assert {r["id"] for r in before} == {first, second}


def test_shrinking_a_rule_is_allowed_when_the_store_is_over_the_cap(son_of_anton_home) -> None:
    store = _store(rules_char_limit=4000)
    rid = _add_rule(store, "Never delete Proton mail, not even spam folders or drafts.")
    tight = _store(rules_char_limit=20)
    result = _call(tight, kind="rule", action="replace", id=rid, text="Keep Proton mail.")
    assert result.get("staged") is True


# -- ids --------------------------------------------------------------------


def test_ids_are_unique_and_stable_across_replace(son_of_anton_home) -> None:
    store = _store()
    a = _add_rule(store, "Keep answers short.")
    b = _add_rule(store, "Keep answers short.")
    assert a != b
    out = _approve(store, _call(store, kind="rule", action="replace", id=a, text="Keep answers terse."))
    assert "Approved 1" in out
    assert {r["id"]: r["text"] for r in _file_rules()}[a] == "Keep answers terse."


def test_make_entry_id_uniquifies_and_handles_empty_text() -> None:
    assert make_entry_id("Ask before deleting mail", {"ask-before-deleting-mail"}) == "ask-before-deleting-mail-2"
    assert make_entry_id("!!!", set(), "rule") == "rule"


def test_unknown_id_errors_with_the_ids_in_scope(son_of_anton_home) -> None:
    store = _store()
    rid = _add_rule(store)
    for action in ("replace", "remove"):
        result = _call(store, kind="rule", action=action, id="nope", text="x")
        assert result["success"] is False and result["ids"] == [rid]
    fact = _call(store, kind="fact", action="add", text="The NAS is at 10.0.0.5.")
    result = _call(store, kind="fact", action="remove", id="nope")
    assert result["success"] is False and result["ids"] == [fact["id"]]


def test_a_gateway_session_can_change_a_common_rule_by_id(son_of_anton_home) -> None:
    common = _store("common")
    rid = _add_rule(common)
    gateway = _store("gateway")
    result = _call(gateway, kind="rule", action="remove", id=rid)
    assert result.get("staged") is True
    assert "Approved 1" in _approve(gateway, result)
    assert _file_rules() == []


# -- prompt -----------------------------------------------------------------


def test_facts_never_render_but_rules_do(son_of_anton_home) -> None:
    store = _store()
    _call(store, kind="fact", action="add", text="The NAS is at 10.0.0.5.")
    _add_rule(store)
    store.load_from_disk()
    block = store.format_for_system_prompt() or ""
    assert RULE in block and "10.0.0.5" not in block


def test_the_prompt_block_is_frozen_until_reload_and_compaction_accepts_the_new_one(
    son_of_anton_home,
) -> None:
    from agent.conversation_compaction import _cached_prompt_reflects_builtin_memory

    store = _store()
    _add_rule(store, "Never delete Proton mail.")
    store.load_from_disk()
    frozen = store.format_for_system_prompt()
    agent = SimpleNamespace(_memory_store=store, _memory_enabled=True)
    prompt = f"stable prefix\n{frozen}\nvolatile tail"
    assert _cached_prompt_reflects_builtin_memory(agent, prompt)

    _add_rule(store, "Prefer metric units.")  # approved mid-session
    assert store.format_for_system_prompt() == frozen, "a mid-session approval changed the prompt"

    store.load_from_disk()  # compaction's reload path
    assert store.format_for_system_prompt() != frozen
    assert not _cached_prompt_reflects_builtin_memory(agent, prompt)
    assert _cached_prompt_reflects_builtin_memory(
        agent, f"stable prefix\n{store.format_for_system_prompt()}\nvolatile tail"
    )


def test_compaction_notices_a_leftover_block_once_the_rules_are_gone(son_of_anton_home) -> None:
    from agent.conversation_compaction import _cached_prompt_reflects_builtin_memory

    store = _store()
    rid = _add_rule(store)
    store.load_from_disk()
    prompt = store.format_for_system_prompt()
    assert "Approved 1" in _approve(store, _call(store, kind="rule", action="remove", id=rid))
    store.load_from_disk()
    agent = SimpleNamespace(_memory_store=store, _memory_enabled=True)
    assert not _cached_prompt_reflects_builtin_memory(agent, prompt)
    assert _cached_prompt_reflects_builtin_memory(agent, "no rules here")


# -- files the user edits ----------------------------------------------------


@pytest.mark.parametrize("kind,name", [("rule", "rules.toml"), ("fact", "facts.toml")])
def test_an_unparseable_file_is_never_overwritten(son_of_anton_home, kind, name) -> None:
    _dir(son_of_anton_home).mkdir(parents=True)
    broken = '[[rule]\nid = "oops"\n'
    (_dir(son_of_anton_home) / name).write_text(broken, encoding="utf-8")
    store = _store()
    assert store.format_for_system_prompt() is None

    result = _call(store, kind=kind, action="add", text="A brand new entry.")

    assert result.get("success") is False and name in result["error"]
    assert wa.pending_count(wa.MEMORY) == 0
    assert (_dir(son_of_anton_home) / name).read_text(encoding="utf-8") == broken
    assert "could not be parsed" in apply_staged(store, kind)
    assert (_dir(son_of_anton_home) / name).read_text(encoding="utf-8") == broken


def apply_staged(store, kind):
    from tools.memory_tool import apply_memory_pending

    result = apply_memory_pending(
        {"kind": kind, "action": "add", "text": "A brand new entry.", "scope": "common"}, store
    )
    assert result["success"] is False
    return result["error"]


def test_unknown_keys_in_an_entry_survive_a_rewrite(son_of_anton_home) -> None:
    _dir(son_of_anton_home).mkdir(parents=True)
    (_dir(son_of_anton_home) / "rules.toml").write_text(
        '[[rule]]\nid = "hand"\ntext = "Hand written."\nscope = "common"\npriority = 7\n',
        encoding="utf-8",
    )
    store = _store()
    rid = _add_rule(store)
    kept = {r["id"]: r for r in _file_rules()}
    assert kept["hand"]["priority"] == 7
    assert rid in kept


# -- facts --------------------------------------------------------------------


def test_a_near_duplicate_fact_is_refused_with_the_existing_id(son_of_anton_home) -> None:
    store = _store()
    first = _call(store, kind="fact", action="add", text="The NAS lives at 10.0.0.5 on the lab VLAN.")
    result = _call(store, kind="fact", action="add", text="the NAS lives at 10.0.0.5 on the lab vlan")
    assert result["success"] is False
    assert [s["id"] for s in result["similar"]] == [first["id"]]
    assert len(_file(son_of_anton_home, "facts.toml", "fact")) == 1


def test_similarity_is_a_token_set_threshold() -> None:
    facts = [{"id": "a", "text": "The NAS is at 10.0.0.5"}]
    assert find_similar_facts("the nas is at 10.0.0.5!", facts)
    assert not find_similar_facts("The printer is at 10.0.0.9", facts)
    assert not find_similar_facts("", facts)


def test_an_over_long_fact_is_refused(son_of_anton_home) -> None:
    store = _store(fact_char_limit=40)
    result = _call(store, kind="fact", action="add", text="x" * 41)
    assert result["success"] is False and "notes/" in result["error"]
    assert _file(son_of_anton_home, "facts.toml", "fact") == []


def test_fact_replace_and_remove_by_id(son_of_anton_home) -> None:
    store = _store()
    fid = _call(store, kind="fact", action="add", text="The NAS is at 10.0.0.5.")["id"]
    assert _call(store, kind="fact", action="replace", id=fid, text="The NAS moved to 10.0.0.6.")["success"]
    assert [f["text"] for f in _file(son_of_anton_home, "facts.toml", "fact")] == ["The NAS moved to 10.0.0.6."]
    assert _call(store, kind="fact", action="remove", id=fid)["success"]
    assert _file(son_of_anton_home, "facts.toml", "fact") == []


def test_an_actionless_call_fails_without_writing(son_of_anton_home) -> None:
    store = _store()
    result = _call(store, kind="rule")
    assert result.get("success") is False
    assert not (_dir(son_of_anton_home) / "rules.toml").exists()
