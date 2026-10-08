"""Retrieval-augmented recall over session journals and configured notes.

The index is local and rebuild-free: embeddings come from an OpenAI-compatible
``/v1/embeddings`` endpoint (Bifrost -> llama.cpp bge-m3 by default), vectors
are appended as float32, and search is a scope-filtered cosine scan in pure
Python (the sealed venv has no numpy, and the corpus is small).

Journals are the corpus. Each session's ``journals/<session>.jsonl`` grows
append-only, so indexing tracks a byte offset per file and only embeds new
events. Configured note files (``memory.rag.sources``) are re-chunked when
their mtime changes and filtered to the newest generation.

Retrieval happens per user turn in ``agent.turn_context`` and rides the
API-copy sidecar (``api_content``), so the frozen system prompt and the prompt
cache are untouched.
"""

from __future__ import annotations

import array
import json
import logging
import math
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from son_of_anton_constants import get_son_of_anton_home

logger = logging.getLogger(__name__)

_EMBED_BATCH = 16
_CHUNK_CHARS = 1200
_CHUNK_OVERLAP = 200
_INDEX_CACHE: Dict[str, Any] = {"key": None, "chunks": [], "vectors": None}

# agent.platform -> memory scope, mirroring tools.memory_tool's mapping.
_GATEWAY_PLATFORMS = frozenset({"signal", "discord", "slack"})


def get_rag_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the normalized ``memory.rag`` section with defaults applied."""
    if config is None:
        try:
            from son_of_anton_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            config = {}
    mem = config.get("memory") if isinstance(config, dict) else None
    section = mem.get("rag") if isinstance(mem, dict) else None
    if not isinstance(section, dict):
        section = {}
    return {
        "enabled": bool(section.get("enabled", False)),
        "base_url": str(section.get("base_url") or "").strip().rstrip("/"),
        "api_key": str(section.get("api_key") or "").strip(),
        "api_key_env": str(section.get("api_key_env") or "").strip(),
        "model": str(section.get("model") or "bge-m3").strip(),
        "top_k": int(section.get("top_k", 5)),
        "min_score": float(section.get("min_score", 0.0)),
        "sources": section.get("sources") if isinstance(section.get("sources"), list) else [],
        "notes_dir": str(section.get("notes_dir") or "").strip(),
        "index_dir": str(section.get("index_dir") or "").strip(),
    }


# Note files discovered under ``notes_dir``: ``notes/<scope>/<anything>.md``
# is indexed with that scope, so adding a note is a file drop, not a config
# edit. Explicit ``sources`` entries stay for files that live elsewhere.
_NOTE_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
_NOTE_SCOPES = frozenset({"shared", "cli", "gateway"})


def notes_dir(config: Optional[Dict[str, Any]] = None) -> Path:
    cfg = coerce_rag_config(config)
    configured = cfg.get("notes_dir")
    if configured:
        return Path(configured).expanduser()
    return get_son_of_anton_home() / "notes"


def iter_note_files(root: Path):
    """Yield ``(path, scope)`` for note files under *root*.

    The first directory segment names the scope (``notes/cli/...``,
    ``notes/gateway/...``); anything else, including files at the root, is
    ``shared``. Directory symlinks are followed (the legacy food/horror logs)
    with a realpath guard against loops.
    """
    seen_dirs = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(real)
        try:
            relative = Path(dirpath).relative_to(root)
        except ValueError:
            relative = Path("")
        top = relative.parts[0] if relative.parts else ""
        scope = top if top in _NOTE_SCOPES else "shared"
        for name in sorted(filenames):
            if Path(name).suffix.lower() in _NOTE_SUFFIXES:
                yield Path(dirpath) / name, scope


def coerce_rag_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Accept either a full config or an already-normalized rag section.

    ``get_rag_config()`` returns the normalized section; callers that pass it
    straight back (the CLI, per-turn retrieval) must not have it re-read as a
    full config — that silently produced empty defaults and a
    "base_url is not configured" error on an enabled, configured deployment.
    """
    if config is None:
        return get_rag_config()
    if not isinstance(config, dict):
        return get_rag_config()
    if isinstance(config.get("memory"), dict):
        return get_rag_config(config)
    return get_rag_config({"memory": {"rag": config}})


def index_dir(config: Optional[Dict[str, Any]] = None) -> Path:
    cfg = coerce_rag_config(config)
    configured = cfg.get("index_dir")
    if configured:
        return Path(configured).expanduser()
    return get_son_of_anton_home() / "rag"


def _resolve_api_key(config: Dict[str, Any]) -> str:
    key = str(config.get("api_key") or "")
    if key:
        return key
    env_name = str(config.get("api_key_env") or "")
    if env_name:
        return os.environ.get(env_name, "")
    return ""


def resolve_session_scope(session_id: str) -> str:
    """Map a session id to its memory scope via the session store source."""
    try:
        from son_of_anton_state import SessionDB

        db = SessionDB()
        try:
            row = db.get_session(session_id)
        finally:
            db.close()
    except Exception:
        return "shared"
    source = str((row or {}).get("source") or "").strip().lower()
    if source == "cli":
        return "cli"
    if source in _GATEWAY_PLATFORMS:
        return "gateway"
    return "shared"


def embed_texts(config: Dict[str, Any], texts: List[str]) -> List[List[float]]:
    """Embed *texts* via the configured OpenAI-compatible endpoint."""
    base_url = str(config.get("base_url") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("memory.rag.base_url is not configured")
    model = str(config.get("model") or "bge-m3")
    headers = {"Content-Type": "application/json"}
    key = _resolve_api_key(config)
    if key:
        # Bifrost accepts the virtual key as x-bf-vk; the bearer form keeps
        # plain OpenAI-compatible endpoints working.
        headers["Authorization"] = f"Bearer {key}"
        headers["x-bf-vk"] = key
    vectors: List[List[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH):
        batch = texts[start:start + _EMBED_BATCH]
        payload = json.dumps({"model": model, "input": batch}).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}/embeddings", data=payload, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected embeddings response: {str(body)[:200]}")
        ordered = sorted(rows, key=lambda r: r.get("index", 0))
        for row in ordered:
            vector = row.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise RuntimeError("Embeddings response row without a vector")
            vectors.append([float(v) for v in vector])
    return vectors


def _chunk_text(text: str, limit: int = _CHUNK_CHARS, overlap: int = _CHUNK_OVERLAP) -> List[str]:
    """Split long text on whitespace into overlapping chunks."""
    text = " ".join(str(text).split())
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            split_at = text.rfind(" ", start + limit // 2, end)
            if split_at > start:
                end = split_at
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def _journal_event_text(event: Dict[str, Any]) -> str:
    role = str(event.get("role") or "").strip()
    if not role:
        return ""
    parts = [f"{role}:"]
    tool = str(event.get("tool_name") or "").strip()
    if tool:
        parts.append(f"({tool})")
    content = event.get("content")
    if isinstance(content, str) and content.strip():
        parts.append(content.strip())
    reasoning = event.get("reasoning_content") or event.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        parts.append(f"[reasoning] {reasoning.strip()}")
    tool_calls = event.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        try:
            parts.append("[tool_calls] " + json.dumps(tool_calls, ensure_ascii=False)[:800])
        except Exception:
            pass
    return " ".join(p for p in parts if p)


def _load_manifest(directory: Path) -> Dict[str, Any]:
    path = directory / "manifest.json"
    if not path.exists():
        return {"model": "", "dim": 0, "journals": {}, "sources": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("journals", {})
            data.setdefault("sources", {})
            return data
    except Exception:
        logger.warning("RAG manifest unreadable; starting a fresh index", exc_info=True)
    return {"model": "", "dim": 0, "journals": {}, "sources": {}}


def _write_manifest(directory: Path, manifest: Dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, directory / "manifest.json")


def _append_vectors(directory: Path, vectors: List[List[float]]) -> int:
    """Append float32 vectors; returns the number of vectors written."""
    if not vectors:
        return 0
    flat = array.array("f")
    for vector in vectors:
        flat.extend(vector)
    path = directory / "vectors.f32"
    with open(path, "ab") as handle:
        handle.write(flat.tobytes())
    return len(vectors)


def _append_chunks(directory: Path, chunks: List[Dict[str, Any]]) -> None:
    if not chunks:
        return
    path = directory / "chunks.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False, default=str))
            handle.write("\n")


def _index_journal(
    path: Path,
    manifest: Dict[str, Any],
    directory: Path,
    config: Dict[str, Any],
    display_name: str,
) -> int:
    """Index new complete lines of one journal file. Returns chunks added."""
    entry = manifest["journals"].get(display_name) or {}
    offset = int(entry.get("offset") or 0)
    size = path.stat().st_size
    if size <= offset:
        return 0
    with open(path, "rb") as handle:
        handle.seek(offset)
        raw = handle.read()
    last_newline = raw.rfind(b"\n")
    if last_newline < 0:
        return 0
    complete, remainder = raw[:last_newline + 1], raw[last_newline + 1:]
    session_id = path.stem
    scope = resolve_session_scope(session_id)
    pending_texts: List[str] = []
    pending_meta: List[Dict[str, Any]] = []
    for line in complete.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict) or event.get("type") == "session":
            continue
        text = _journal_event_text(event)
        if not text:
            continue
        chunks = _chunk_text(text)
        for index, chunk in enumerate(chunks):
            pending_texts.append(chunk)
            pending_meta.append(
                {
                    "text": chunk,
                    "source": "journal",
                    "source_name": display_name,
                    "session": session_id,
                    "scope": scope,
                    "role": event.get("role"),
                    "tool": event.get("tool_name"),
                    "ts": event.get("ts"),
                    "chunk": index,
                }
            )
    if not pending_texts:
        manifest["journals"][display_name] = {
            "offset": offset + len(complete),
            "mtime": path.stat().st_mtime,
        }
        return 0
    vectors = embed_texts(config, pending_texts)
    written = _append_vectors(directory, vectors)
    if written != len(pending_texts):
        raise RuntimeError("Embedding batch returned fewer vectors than inputs")
    _append_chunks(directory, pending_meta)
    manifest["journals"][display_name] = {
        "offset": offset + len(complete) + len(remainder),
        "mtime": path.stat().st_mtime,
        "chunks": int(entry.get("chunks") or 0) + len(pending_meta),
    }
    return len(pending_meta)


def _index_source(
    path: Path,
    manifest: Dict[str, Any],
    directory: Path,
    config: Dict[str, Any],
    scope: str,
) -> int:
    """Chunk one configured note file, bumping its generation on change."""
    display_name = str(path)
    try:
        stat = path.stat()
    except OSError:
        return 0
    entry = manifest["sources"].get(display_name) or {}
    if entry.get("mtime") == stat.st_mtime and entry.get("size") == stat.st_size:
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    chunks = _chunk_text(text)
    generation = int(entry.get("generation") or 0) + 1
    if not chunks:
        manifest["sources"][display_name] = {
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "generation": generation,
            "chunks": 0,
        }
        return 0
    vectors = embed_texts(config, chunks)
    _append_vectors(directory, vectors)
    _append_chunks(
        directory,
        [
            {
                "text": chunk,
                "source": "note",
                "source_name": display_name,
                "scope": scope,
                "generation": generation,
                "chunk": index,
            }
            for index, chunk in enumerate(chunks)
        ],
    )
    manifest["sources"][display_name] = {
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "generation": generation,
        "chunks": len(chunks),
    }
    return len(chunks)


def sync_index(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Bring the index up to date. Returns stats; raises on embedding failure."""
    config = coerce_rag_config(config)
    if not config.get("base_url"):
        return {"ok": False, "error": "memory.rag.base_url is not configured"}
    directory = index_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(directory)
    manifest["model"] = config.get("model") or "bge-m3"
    added = 0

    journals_dir = get_son_of_anton_home() / "journals"
    if journals_dir.is_dir():
        for path in sorted(journals_dir.glob("*.jsonl")):
            added += _index_journal(path, manifest, directory, config, path.name)
    # A journal that no longer exists stops being searchable: prune its
    # manifest entry so the current-generation filter drops its chunks.
    manifest["journals"] = {
        name: entry
        for name, entry in manifest["journals"].items()
        if (journals_dir / name).exists()
    }

    configured_sources = set()
    for source in config.get("sources") or []:
        if isinstance(source, str):
            source = {"path": source, "scope": "shared"}
        if not isinstance(source, dict):
            continue
        raw_path = str(source.get("path") or "").strip()
        if not raw_path:
            continue
        scope = str(source.get("scope") or "shared").strip().lower()
        if scope not in {"shared", "cli", "gateway"}:
            scope = "shared"
        expanded = Path(raw_path).expanduser()
        configured_sources.add(str(expanded))
        added += _index_source(expanded, manifest, directory, config, scope)
    # Discovered notes: notes/<scope>/... needs no config entry at all.
    discovered_sources = set()
    root = notes_dir(config)
    if root.is_dir():
        for note_path, note_scope in iter_note_files(root):
            discovered_sources.add(str(note_path))
            added += _index_source(note_path, manifest, directory, config, note_scope)
    # Same switch for note files: a source dropped from the config (or from
    # the notes tree) must stop being searchable — its chunks are append-only,
    # the manifest is the gate.
    manifest["sources"] = {
        name: entry
        for name, entry in manifest["sources"].items()
        if name in configured_sources or name in discovered_sources
    }

    _write_manifest(directory, manifest)
    return {
        "ok": True,
        "added": added,
        "journals": len(manifest["journals"]),
        "sources": len(manifest["sources"]),
        "index_dir": str(directory),
    }


def _load_index(config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Any, int]:
    """Load chunks + vectors, cached by (chunks mtime, size). Pure local I/O."""
    directory = index_dir(config)
    chunks_path = directory / "chunks.jsonl"
    vectors_path = directory / "vectors.f32"
    if not chunks_path.exists() or not vectors_path.exists():
        return [], None, 0
    try:
        stat = chunks_path.stat()
        cache_key = (str(chunks_path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return [], None, 0
    if _INDEX_CACHE.get("key") == cache_key:
        return _INDEX_CACHE["chunks"], _INDEX_CACHE["vectors"], _INDEX_CACHE["dim"]
    chunks: List[Dict[str, Any]] = []
    try:
        with open(chunks_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    chunks.append(json.loads(line))
                except Exception:
                    continue
        flat = array.array("f")
        with open(vectors_path, "rb") as handle:
            flat.fromfile(handle, vectors_path.stat().st_size // 4)
    except Exception:
        logger.warning("RAG index unreadable", exc_info=True)
        return [], None, 0
    dim = (len(flat) // len(chunks)) if chunks else 0
    _INDEX_CACHE.update({"key": cache_key, "chunks": chunks, "vectors": flat, "dim": dim})
    return chunks, flat, dim


def _vector_at(flat: Any, index: int, dim: int) -> Optional[List[float]]:
    start = index * dim
    end = start + dim
    if end > len(flat):
        return None
    return flat[start:end]


def search(
    query: str,
    *,
    config: Optional[Dict[str, Any]] = None,
    scope: str = "shared",
    top_k: int = 5,
    min_score: float = 0.0,
) -> List[Dict[str, Any]]:
    """Return the closest indexed chunks visible from *scope*."""
    config = coerce_rag_config(config)
    if not query.strip():
        return []
    chunks, flat, dim = _load_index(config)
    if not chunks or not flat or dim <= 0:
        return []
    # Keep only the current generation of each note source and the visible
    # scopes; journals carry their scope per chunk.
    visible = {"shared", scope}
    current_generations: Dict[str, int] = {}
    directory = index_dir(config)
    manifest = _load_manifest(directory)
    for name, entry in manifest.get("sources", {}).items():
        current_generations[name] = int(entry.get("generation") or 0)
    candidates = []
    for index, chunk in enumerate(chunks):
        if chunk.get("scope") not in visible:
            continue
        if chunk.get("source") == "note":
            if current_generations.get(chunk.get("source_name")) != int(
                chunk.get("generation") or 0
            ):
                continue
        candidates.append((index, chunk))
    if not candidates:
        return []
    query_vector = embed_texts(config, [query])[0]
    query_norm = math.sqrt(sum(v * v for v in query_vector)) or 1.0
    scored = []
    for index, chunk in candidates:
        vector = _vector_at(flat, index, dim)
        if vector is None:
            continue
        dot = 0.0
        norm_sq = 0.0
        for a, b in zip(query_vector, vector):
            dot += a * b
            norm_sq += b * b
        if norm_sq <= 0:
            continue
        score = dot / (query_norm * math.sqrt(norm_sq))
        if score >= min_score:
            scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    results = []
    for score, chunk in scored[: max(int(top_k), 1)]:
        results.append(dict(chunk, score=score))
    return results


def format_recalled_context(results: List[Dict[str, Any]]) -> str:
    """Render search results as a compact, fenced block for the API copy."""
    if not results:
        return ""
    lines = ["<recalled-notes>"]
    for result in results:
        source = str(result.get("source") or "")
        if source == "journal":
            label = f"session {str(result.get('session') or '?')[:8]} {result.get('role') or ''}".strip()
        else:
            label = Path(str(result.get("source_name") or "note")).name
        lines.append(f"[{label}] {result.get('text', '')}")
    lines.append("</recalled-notes>")
    return "\n".join(lines)


def retrieve_for_turn(agent: Any, query: str) -> str:
    """Search the index for the active session's scope. Never raises."""
    try:
        config = get_rag_config()
        if not config.get("enabled"):
            return ""
        scope = getattr(agent, "_memory_scope", "shared") or "shared"
        results = search(
            query,
            config=config,
            scope=scope,
            top_k=config.get("top_k", 5),
            min_score=config.get("min_score", 0.0),
        )
        if not results:
            return ""
        return format_recalled_context(results)
    except Exception:
        logger.debug("RAG retrieval failed", exc_info=True)
        return ""
