"""RAG index and retrieval: chunking, scope filtering, append-only indexing."""

from __future__ import annotations

import json
import urllib.request

from agent import rag
from agent.turn_context import compose_user_api_content
from son_of_anton_constants import get_son_of_anton_home
from son_of_anton_state import SessionDB

WORDS = ("alpha", "beta", "gamma")


def _fake_embed(config, texts):
    """Deterministic keyword-count vectors — good enough for ranking tests."""
    return [[float(t.lower().count(word)) for word in WORDS] for t in texts]


def _config(**rag_overrides):
    section = {"enabled": True, "base_url": "http://embeddings.invalid/v1", "model": "bge-m3"}
    section.update(rag_overrides)
    return {"memory": {"rag": section}}


def _write_journal(session_id: str, events: list) -> None:
    directory = get_son_of_anton_home() / "journals"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "session", "session_id": session_id, "ts": 1.0})]
    lines += [json.dumps(event) for event in events]
    (directory / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_session(session_id: str, source: str) -> None:
    db = SessionDB()
    db.create_session(session_id=session_id, source=source, cwd="/tmp")
    db.close()


def test_chunk_text_caps_and_overlaps() -> None:
    text = " ".join(f"word{i}" for i in range(600))
    chunks = rag._chunk_text(text, limit=200, overlap=50)
    assert len(chunks) > 1
    assert all(len(chunk) <= 200 for chunk in chunks)


def test_journal_event_text_carries_reasoning_and_tools() -> None:
    text = rag._journal_event_text(
        {
            "role": "assistant",
            "content": "done",
            "tool_name": "read_file",
            "reasoning_content": "checked it",
        }
    )
    assert "assistant:" in text
    assert "read_file" in text
    assert "done" in text
    assert "checked it" in text


def test_sync_index_and_search_respect_scope(monkeypatch) -> None:
    monkeypatch.setattr(rag, "embed_texts", _fake_embed)
    _make_session("20261001_000001_aaaaaa", "cli")
    _make_session("20261001_000002_bbbbbb", "signal")
    _write_journal(
        "20261001_000001_aaaaaa",
        [{"type": "message", "role": "user", "content": "alpha workstation fact", "ts": 2.0}],
    )
    _write_journal(
        "20261001_000002_bbbbbb",
        [{"type": "message", "role": "user", "content": "alpha personal fact", "ts": 3.0}],
    )

    stats = rag.sync_index(_config())
    assert stats["ok"] and stats["added"] >= 2

    cli_hits = rag.search("alpha", config=_config(), scope="cli", top_k=5)
    assert cli_hits and all(hit["scope"] != "gateway" for hit in cli_hits)
    assert any("workstation" in hit["text"] for hit in cli_hits)

    gateway_hits = rag.search("alpha", config=_config(), scope="gateway", top_k=5)
    assert any("personal" in hit["text"] for hit in gateway_hits)


def test_indexing_is_incremental(monkeypatch) -> None:
    monkeypatch.setattr(rag, "embed_texts", _fake_embed)
    _write_journal(
        "20261001_000003_cccccc",
        [{"type": "message", "role": "user", "content": "alpha first", "ts": 2.0}],
    )
    first = rag.sync_index(_config())
    assert first["added"] >= 1

    assert rag.sync_index(_config())["added"] == 0

    path = get_son_of_anton_home() / "journals" / "20261001_000003_cccccc.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "message", "role": "user", "content": "beta second", "ts": 4.0}) + "\n")
    assert rag.sync_index(_config())["added"] >= 1


def test_stale_note_generation_is_not_searchable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(rag, "embed_texts", _fake_embed)
    note = tmp_path / "notes.md"
    note.write_text("alpha old note", encoding="utf-8")
    config = _config(sources=[{"path": str(note), "scope": "shared"}])
    rag.sync_index(config)
    assert any(
        "note" == hit["source"]
        for hit in rag.search("alpha", config=config, scope="cli", min_score=0.5)
    )

    note.write_text("beta new note", encoding="utf-8")
    rag.sync_index(config)
    note_hits = [
        hit
        for hit in rag.search("alpha", config=config, scope="cli", min_score=0.5)
        if hit["source"] == "note"
    ]
    assert note_hits == []
    assert any(
        hit["source"] == "note"
        for hit in rag.search("beta", config=config, scope="cli", min_score=0.5)
    )


def test_embed_request_sends_bifrost_and_bearer_headers(monkeypatch) -> None:
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "data": [
                        {"index": 1, "embedding": [3.0, 4.0]},
                        {"index": 0, "embedding": [1.0, 2.0]},
                    ]
                }
            ).encode("utf-8")

    def _fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setenv("TEST_RAG_KEY", "vk-123")
    vectors = rag.embed_texts(
        {
            "base_url": "http://bifrost.invalid/v1",
            "model": "bge-m3",
            "api_key_env": "TEST_RAG_KEY",
        },
        ["first", "second"],
    )
    # Sorted by the response's index field, not arrival order.
    assert vectors == [[1.0, 2.0], [3.0, 4.0]]
    assert captured["headers"].get("X-bf-vk") == "vk-123"
    assert captured["headers"].get("Authorization") == "Bearer vk-123"
    assert captured["body"]["model"] == "bge-m3"
    assert captured["body"]["input"] == ["first", "second"]


def test_compose_user_api_content_includes_rag_block() -> None:
    composed = compose_user_api_content("question", "", "", "<recalled-notes>x</recalled-notes>")
    assert composed is not None
    assert "question" in composed
    assert "<recalled-notes>x</recalled-notes>" in composed


def test_normalized_section_is_accepted_by_sync_and_search(monkeypatch) -> None:
    """The CLI and per-turn retrieval pass ``get_rag_config()``'s output back in."""
    monkeypatch.setattr(rag, "embed_texts", _fake_embed)
    _write_journal(
        "20261001_000007_gggggg",
        [{"type": "message", "role": "user", "content": "alpha section fact", "ts": 2.0}],
    )
    section = rag.get_rag_config(_config())
    assert section["base_url"] == "http://embeddings.invalid/v1"

    stats = rag.sync_index(section)
    assert stats["ok"] and stats["added"] >= 1
    assert rag.search("alpha", config=section, scope="shared", top_k=3)


def test_rag_parser_registers_index_command() -> None:
    import argparse

    from son_of_anton_cli.subcommands.rag import build_rag_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    seen = []
    build_rag_parser(subparsers, cmd_rag=lambda args: seen.append(args.rag_action))

    args = parser.parse_args(["rag", "index", "--rebuild"])
    assert args.rag_action == "index"
    assert args.rebuild is True
    args.func(args)
    assert seen == ["index"]
