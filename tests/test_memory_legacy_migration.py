"""One-time import of the legacy MEMORY*.md / USER*.md stores into rules and facts.

Behavior contracts against a temp SON_OF_ANTON_HOME: nothing is lost, it runs
once, it never touches what it cannot understand, and concurrent starts agree.
"""

from __future__ import annotations

import tomllib
from concurrent.futures import ThreadPoolExecutor

from son_of_anton_constants import get_son_of_anton_home
from tools.memory_tool import migrate_legacy_memory
from utils import dump_toml

SEP = "\n§\n"


def _mem():
    d = get_son_of_anton_home() / "memories"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _legacy(name: str, *entries: str) -> None:
    (_mem() / name).write_text(SEP.join(entries), encoding="utf-8")


def _load(name: str, key: str) -> list:
    path = _mem() / name
    return tomllib.loads(path.read_text(encoding="utf-8")).get(key, []) if path.exists() else []


def _rules(scope: str) -> list:
    return [r["text"] for r in _load("rules.toml", "rule") if r.get("scope", "common") == scope]


def _facts(scope: str) -> list:
    name = "facts.toml" if scope == "common" else "facts.gateway.toml"
    return [f["text"] for f in _load(name, "fact")]


def _agent(platform: str):
    from run_agent import AIAgent
    from son_of_anton_cli.config import save_config

    save_config(
        {
            "model": {"default": "m", "provider": "custom"},
            "custom_providers": {"custom": {"base_url": "http://127.0.0.1:9/v1"}},
        }
    )
    return AIAgent(
        base_url="http://127.0.0.1:9/v1",
        provider="custom",
        model="m",
        api_mode="chat_completions",
        max_iterations=1,
        quiet_mode=True,
        platform=platform,
        skip_context_files=True,
    )


def _all_files():
    return (
        "USER.md", "USER.cli.md", "USER.gateway.md",
        "MEMORY.md", "MEMORY.cli.md", "MEMORY.gateway.md",
    )


def test_each_file_lands_in_its_kind_and_scope_and_the_first_agent_renders_it(son_of_anton_home):
    _legacy("USER.md", "shared rule")
    _legacy("USER.cli.md", "cli rule")
    _legacy("USER.gateway.md", "gateway rule")
    _legacy("MEMORY.md", "shared fact one", "shared fact two")
    _legacy("MEMORY.cli.md", "cli fact")
    _legacy("MEMORY.gateway.md", "gateway fact")

    cli = _agent("cli")  # first init after "deploy" migrates before the snapshot
    block = cli._memory_store.format_for_system_prompt() or ""
    assert "shared rule" in block and "cli rule" in block
    assert "gateway rule" not in block
    assert "gateway rule" in (_agent("signal")._memory_store.format_for_system_prompt() or "")

    assert _rules("common") == ["shared rule", "cli rule"]
    assert _rules("gateway") == ["gateway rule"]
    assert _facts("common") == ["shared fact one", "shared fact two", "cli fact"]
    assert _facts("gateway") == ["gateway fact"]
    for entry in _load("rules.toml", "rule") + _load("facts.toml", "fact"):
        assert entry["source"] == "agent"
    ids = [r["id"] for r in _load("rules.toml", "rule")]
    assert len(ids) == len(set(ids))


def test_skipped_when_a_facts_file_exists(son_of_anton_home):
    (_mem() / "facts.toml").write_text(dump_toml({"fact": []}), encoding="utf-8")
    _legacy("USER.md", "a rule")
    assert migrate_legacy_memory() is None
    assert (_mem() / "USER.md").exists()
    assert not (_mem() / "rules.toml").exists()


def test_runs_with_an_existing_rules_file_and_keeps_its_rules(son_of_anton_home):
    (_mem() / "rules.toml").write_text(
        dump_toml({"rule": [{"id": "seeded", "text": "nix rule", "scope": "common"}]}),
        encoding="utf-8",
    )
    _legacy("USER.md", "nix rule", "old rule")
    counts = migrate_legacy_memory()
    assert counts["duplicates"] == 1
    assert _rules("common") == ["nix rule", "old rule"]
    assert _load("rules.toml", "rule")[0]["id"] == "seeded"


def test_idempotent_and_nothing_deleted(son_of_anton_home):
    _legacy("USER.md", "r")
    _legacy("MEMORY.md", "f")
    assert migrate_legacy_memory() is not None
    before = {p.name: p.read_bytes() for p in _mem().iterdir() if p.suffix != ".lock"}
    assert migrate_legacy_memory() is None
    after = {p.name: p.read_bytes() for p in _mem().iterdir() if p.suffix != ".lock"}
    assert before == after
    assert (_mem() / "USER.md.legacy").read_text(encoding="utf-8") == "r"
    assert (_mem() / "MEMORY.md.legacy").read_text(encoding="utf-8") == "f"
    assert not any((_mem() / n).exists() for n in _all_files())
    assert (_mem() / "facts.gateway.toml").exists()  # written even when empty


def test_exact_duplicates_across_memory_files_collapse(son_of_anton_home):
    _legacy("MEMORY.md", "same", "other")
    _legacy("MEMORY.cli.md", "same")
    counts = migrate_legacy_memory()
    assert _facts("common") == ["same", "other"]
    assert counts["duplicates"] == 1


def test_rules_over_the_cap_overflow_to_facts_and_nothing_is_lost(son_of_anton_home):
    texts = [f"rule number {i} " + "x" * 40 for i in range(10)]
    _legacy("USER.md", *texts)
    counts = migrate_legacy_memory(rules_char_limit=300)
    assert counts["overflow"] > 0
    kept, spilled = _rules("common"), _facts("common")
    assert kept == texts[: len(kept)]  # the last ones overflow
    assert kept + spilled == texts
    assert len(texts) == counts["rules"] + counts["overflow"] + counts["duplicates"]


def test_unreadable_legacy_file_migrates_nothing(son_of_anton_home):
    _legacy("USER.md", "a rule")
    (_mem() / "MEMORY.md").write_bytes(b"\xff\xfe not utf-8 \x80")
    assert migrate_legacy_memory() is None
    assert not (_mem() / "rules.toml").exists()
    assert not (_mem() / "facts.toml").exists()
    assert (_mem() / "USER.md").exists() and (_mem() / "MEMORY.md").exists()
    assert not list(_mem().glob("*.legacy"))


def test_unparseable_rules_file_is_never_overwritten(son_of_anton_home):
    (_mem() / "rules.toml").write_text("[[rule]\nbroken", encoding="utf-8")
    _legacy("USER.md", "a rule")
    assert migrate_legacy_memory() is None
    assert (_mem() / "rules.toml").read_text(encoding="utf-8") == "[[rule]\nbroken"
    assert (_mem() / "USER.md").exists()
    assert not (_mem() / "facts.toml").exists()


def test_concurrent_migrations_produce_one_result(son_of_anton_home):
    _legacy("USER.md", "r1", "r2")
    _legacy("MEMORY.md", "f1", "f2")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: migrate_legacy_memory(), range(8)))
    assert sum(r is not None for r in results) == 1
    assert _rules("common") == ["r1", "r2"]
    assert _facts("common") == ["f1", "f2"]
