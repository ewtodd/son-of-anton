"""Facts in the RAG index, per-turn recall, the keyword fallback and the duplicate check.

Embeddings are always stubbed -- no test reaches a real endpoint.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import rag
from son_of_anton_constants import get_son_of_anton_home
from son_of_anton_state import SessionDB
from tools.memory_tool import ScopedMemoryStore, facts_filename, get_memory_dir

WORDS = ("alpha", "beta", "gamma")


def _fake_embed(config, texts, **kwargs):
    return [[float(t.lower().count(word)) for word in WORDS] for t in texts]


def _config(**overrides):
    section = {"enabled": True, "base_url": "http://embeddings.invalid/v1", "model": "bge-m3"}
    section.update(overrides)
    return {"memory": {"rag": section}}


def _write_facts(scope, facts):
    path = get_memory_dir() / facts_filename(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f'[[fact]]\nid = "{i}"\ntext = "{t}"\n\n' for i, t in facts), encoding="utf-8"
    )


def _agent(platform="cli", **kwargs):
    base = {"platform": platform, "session_id": "", "_chat_id": None, "_user_id": None,
            "_parent_session_id": None, "_session_db": None}
    base.update(kwargs)
    return SimpleNamespace(**base)


def _indexed(config=None):
    index = rag._load_index(rag.coerce_rag_config(config or _config()))
    return list(index.chunks) if index else []


@pytest.fixture
def embed(monkeypatch):
    monkeypatch.setattr(rag, "embed_texts", _fake_embed)
    rag._SEARCH_STATE["cooldown_until"] = 0.0
    config = _config()
    original = rag.get_rag_config
    monkeypatch.setattr(
        rag, "get_rag_config", lambda c=None: original(config if c is None else c)
    )
    return config


def _down(monkeypatch):
    def boom(config, texts, **kwargs):
        raise TimeoutError("embeddings down")

    monkeypatch.setattr(rag, "embed_texts", boom)


def _wait_for_refresh():
    thread = rag._FACTS_STATE.get("thread")
    if thread is not None:
        thread.join(10)


def test_a_journal_indexes_conversation_only(embed) -> None:
    db = SessionDB()
    db.create_session(session_id="20261001_000001_a", source="cli", cwd="/tmp")
    db.close()
    events = [
        {"type": "message", "role": "user", "content": "alpha question", "ts": 2.0},
        {"type": "message", "role": "assistant", "content": "alpha narration",
         "tool_calls": [{"function": {"name": "read_file"}}], "ts": 3.0},
        {"type": "message", "role": "tool", "tool_name": "read_file",
         "content": "alpha source code", "ts": 4.0},
        {"type": "message", "role": "assistant", "content": "alpha final answer",
         "reasoning_content": "alpha secret thinking", "ts": 5.0},
    ]
    path = get_son_of_anton_home() / "journals" / "20261001_000001_a.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    rag.sync_index(embed)
    texts = [c["text"] for c in _indexed(embed) if c["source"] == "journal"]
    assert sorted(texts) == ["assistant: alpha final answer", "user: alpha question"]


def test_recall_injects_facts_with_ids_and_never_journal_chunks(embed) -> None:
    db = SessionDB()
    db.create_session(session_id="20261001_000002_a", source="cli", cwd="/tmp")
    db.close()
    path = get_son_of_anton_home() / "journals" / "20261001_000002_a.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"type": "message", "role": "user", "content": "alpha journal talk", "ts": 2.0})
        + "\n",
        encoding="utf-8",
    )
    _write_facts("common", [("nas-ip", "alpha nas lives at 10.0.0.5")])
    rag.sync_index(embed)
    assert any(c["source"] == "journal" for c in _indexed())
    block = rag.retrieve_for_turn(_agent(), "alpha?")
    assert "[fact nas-ip] alpha nas lives at 10.0.0.5" in block
    assert "journal talk" not in block
    assert "not instructions" in block


def test_a_written_fact_is_retrievable_and_a_removed_one_is_gone(embed) -> None:
    _write_facts("common", [("a", "alpha first"), ("b", "beta second")])
    rag.sync_index(embed)
    assert "alpha first" in rag.retrieve_for_turn(_agent(), "alpha")
    _write_facts("common", [("b", "beta second")])
    rag.sync_index(embed)
    assert "alpha first" not in rag.retrieve_for_turn(_agent(), "alpha")
    assert [c["fact_id"] for c in _indexed() if c["source"] == "fact"] == ["b"]
    # Edited text replaces the old chunk.
    _write_facts("common", [("b", "beta changed")])
    rag.sync_index(embed)
    assert [c["text"] for c in _indexed() if c["source"] == "fact"] == ["beta changed"]


def test_a_memory_write_kicks_a_background_index_refresh(embed) -> None:
    rag.sync_index(embed)
    store = ScopedMemoryStore("common")
    result = store.apply("fact", "add", text="gamma printer is in the hall")
    assert result["success"]
    _wait_for_refresh()
    assert "gamma printer" in rag.retrieve_for_turn(_agent(), "gamma printer")
    store.apply("fact", "remove", id=result["id"])
    _wait_for_refresh()
    assert not [c for c in _indexed() if c["source"] == "fact"]


def test_the_refresh_is_a_noop_when_rag_is_off(monkeypatch) -> None:
    monkeypatch.setattr(rag, "embed_texts", _fake_embed)
    assert rag.refresh_facts_async(_config(enabled=False)) is None
    result = ScopedMemoryStore("common").apply("fact", "add", text="whatever it is")
    assert result["success"]


def test_embedder_down_falls_back_to_keywords_over_visible_facts(embed, monkeypatch) -> None:
    _write_facts("common", [("nas", "the backup server hostname is unholy"),
                            ("tea", "user drinks oolong tea in the morning")])
    rag.sync_index(embed)
    _down(monkeypatch)
    block = rag.retrieve_for_turn(_agent(), "what hostname does the backup server have?")
    assert "[fact nas]" in block and "oolong" not in block
    rag._SEARCH_STATE["cooldown_until"] = 0.0
    assert rag.retrieve_for_turn(_agent(), "zebra quantum chromodynamics") == ""
    assert rag.retrieve_for_turn(_agent(), "") == ""


def test_keyword_fallback_works_with_no_index_and_during_cooldown(embed) -> None:
    _write_facts("common", [("nas", "the backup server hostname is unholy")])
    rag._SEARCH_STATE["cooldown_until"] = 10**12
    try:
        assert "[fact nas]" in rag.retrieve_for_turn(_agent(), "backup server hostname")
    finally:
        rag._SEARCH_STATE["cooldown_until"] = 0.0


def test_a_terminal_never_gets_a_gateway_fact(embed, monkeypatch) -> None:
    _write_facts("common", [("c", "alpha work fact")])
    _write_facts("gateway", [("g", "alpha personal fact")])
    rag.sync_index(embed)
    assert {c["scope"] for c in _indexed() if c["source"] == "fact"} == {"common", "gateway"}
    terminal = rag.retrieve_for_turn(_agent(), "alpha")
    assert "work fact" in terminal and "personal" not in terminal
    signal = _agent("signal", _chat_id="g1")
    block = rag.retrieve_for_turn(signal, "alpha")
    assert "work fact" in block and "personal fact" in block
    # The keyword fallback honors the same visibility.
    _down(monkeypatch)
    rag._SEARCH_STATE["cooldown_until"] = 0.0
    assert "personal" not in rag.retrieve_for_turn(_agent(), "alpha personal fact")
    rag._SEARCH_STATE["cooldown_until"] = 0.0
    assert "personal" in rag.retrieve_for_turn(signal, "alpha personal fact")


def test_an_unparseable_facts_file_keeps_what_was_indexed(embed) -> None:
    _write_facts("common", [("a", "alpha kept")])
    rag.sync_index(embed)
    (get_memory_dir() / facts_filename("common")).write_text("[[fact\nbroken", encoding="utf-8")
    assert rag.sync_index(embed)["ok"]
    assert [c["text"] for c in _indexed() if c["source"] == "fact"] == ["alpha kept"]


def test_fact_indexing_respects_the_refresh_budget(embed) -> None:
    config = _config(max_chunks_per_refresh=2)
    _write_facts("common", [(str(i), f"alpha fact {i}") for i in range(5)])
    assert rag.sync_index(config)["added"] == 2
    assert rag.sync_index(config)["added"] == 2
    assert rag.sync_index(config)["added"] == 1
    assert rag.sync_index(config)["added"] == 0
    assert len([c for c in _indexed(config) if c["source"] == "fact"]) == 5


def test_an_old_format_index_rebuilds_on_the_next_sync(embed) -> None:
    _write_facts("common", [("a", "alpha fact")])
    rag.sync_index(embed)
    directory = rag.index_dir(embed)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    manifest["format"] = rag.FORMAT_VERSION - 1
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert rag.search("alpha", config=embed) == []  # unretrievable until rebuilt
    assert rag.sync_index(embed)["ok"]
    after = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert after["format"] == rag.FORMAT_VERSION
    assert [c["fact_id"] for c in _indexed() if c["source"] == "fact"] == ["a"]


def test_the_duplicate_check_uses_embeddings_when_they_answer(embed, monkeypatch) -> None:
    store = ScopedMemoryStore("common")
    first = store.apply("fact", "add", text="alpha alpha alpha beta nas")
    assert first["success"]
    _wait_for_refresh()
    # Lexically far (Jaccard 4/6), semantically identical under the stub.
    refused = store.apply("fact", "add", text="alpha alpha alpha beta printer")
    assert not refused["success"]
    assert [f["id"] for f in refused["similar"]] == [first["id"]]
    assert store.apply("fact", "add", text="gamma gamma unrelated")["success"]
    _wait_for_refresh()
    # Embedder down: Jaccard only, so the semantic twin goes through.
    _down(monkeypatch)
    assert store.apply("fact", "add", text="alpha alpha alpha beta scanner")["success"]
    # ...but a lexical near-copy is still refused.
    rag._SEARCH_STATE["cooldown_until"] = 0.0
    again = store.apply("fact", "add", text="alpha alpha alpha beta nas.")
    assert not again["success"] and again["similar"]


def test_the_vector_norms_are_computed_once_per_loaded_index(embed, monkeypatch) -> None:
    _write_facts("common", [("a", "alpha one"), ("b", "beta two")])
    rag.sync_index(embed)
    index = rag._load_index(rag.coerce_rag_config(embed))
    assert index.norms is index.norms
    assert rag._load_index(rag.coerce_rag_config(embed)) is index
