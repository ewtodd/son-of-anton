"""RAG visibility under two scopes: common (everywhere) and gateway (Signal only).

Embeddings are stubbed; indexes live in the isolated SON_OF_ANTON_HOME.
"""

from __future__ import annotations

import json

import pytest

from agent import rag
from son_of_anton_constants import get_son_of_anton_home
from son_of_anton_state import SessionDB


def _fake_embed(config, texts, **kwargs):
    return [[float(t.lower().count("alpha"))] for t in texts]


def _config(**overrides):
    section = {"enabled": True, "base_url": "http://embeddings.invalid/v1", "model": "bge-m3"}
    section.update(overrides)
    return {"memory": {"rag": section}}


@pytest.fixture
def embed(monkeypatch):
    monkeypatch.setattr(rag, "embed_texts", _fake_embed)
    rag._SEARCH_STATE["cooldown_until"] = 0.0


def _session(session_id: str, source: str, text: str, **kwargs) -> None:
    db = SessionDB()
    db.create_session(session_id=session_id, source=source, cwd="/tmp", **kwargs)
    db.close()
    path = get_son_of_anton_home() / "journals" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "session", "session_id": session_id, "ts": 1.0},
        {"type": "message", "role": "user", "content": text, "ts": 2.0},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def test_legacy_scope_stamps_read_as_common_and_unknown_is_invisible() -> None:
    for stamp in ("cli", "shared", "common"):
        chunk = {"scope": stamp}
        assert rag.chunk_visible(chunk, "common", None)
        assert rag.chunk_visible(chunk, "gateway", "signal:a")
    assert not rag.chunk_visible({"scope": "bogus"}, "common", None)
    assert not rag.chunk_visible({"scope": "bogus"}, "gateway", "signal:a")
    assert not rag.chunk_visible({}, "gateway", "signal:a")


def test_terminal_never_sees_gateway_or_chat_stamped_chunks() -> None:
    assert not rag.chunk_visible({"scope": "gateway"}, "common", None)
    assert not rag.chunk_visible({"scope": "gateway"}, "common", "signal:a")
    assert not rag.chunk_visible({"scope": "common", "chats": ["signal:a"]}, "common", None)
    assert not rag.chunk_visible({"scope": "cli", "chats": ["signal:a"]}, "common", "signal:b")
    assert rag.chunk_visible({"scope": "gateway"}, "gateway", "signal:a")
    assert rag.chunk_visible({"scope": "common", "chats": ["signal:a"]}, "gateway", "signal:a")


def test_indexed_legacy_chunks_are_searchable_from_tui_and_signal(embed) -> None:
    _session("20261001_000300_tui", "tui", "alpha terminal fact")
    _session("20261001_000301_sig", "signal", "alpha personal chat fact", chat_id="groupA")
    rag.sync_index(_config())
    index = rag._load_index(rag.coerce_rag_config(_config()))
    for chunk in index.chunks:
        # What an index built before the collapse says.
        chunk["scope"] = "gateway" if chunk.get("chats") else "cli"

    def texts(scope, chat=None):
        hits = rag.search("alpha", config=_config(), top_k=10, scope=scope, chat=chat)
        return " | ".join(h["text"] for h in hits)

    from_tui = texts("common")
    assert "terminal fact" in from_tui and "personal" not in from_tui
    from_signal = texts("gateway", rag.chat_identity("signal", "groupA"))
    assert "terminal fact" in from_signal and "personal chat fact" in from_signal


def test_stored_identity_with_legacy_scope_normalizes() -> None:
    for old in ("cli", "shared"):
        assert rag._stored_identity({"identity": {"scope": old, "chats": []}}) == ("common", [])
    assert rag._stored_identity({"identity": {"scope": "bogus", "chats": []}}) is None


def test_notes_under_legacy_cli_dir_and_sources_index_as_common(embed, tmp_path) -> None:
    notes = get_son_of_anton_home() / "notes"
    (notes / "cli").mkdir(parents=True)
    (notes / "cli" / "work.md").write_text("alpha legacy workstation note", encoding="utf-8")
    (notes / "root.md").write_text("alpha root note", encoding="utf-8")
    assert {scope for _, scope in rag.iter_note_files(notes)} == {"common"}
    src = tmp_path / "extra.md"
    src.write_text("alpha extra note", encoding="utf-8")
    config = _config(sources=[{"path": str(src), "scope": "cli"}])
    rag.sync_index(config)
    hits = " | ".join(
        h["text"] for h in rag.search("alpha", config=config, scope="common", top_k=10)
    )
    assert "legacy workstation" in hits and "root note" in hits and "extra note" in hits
