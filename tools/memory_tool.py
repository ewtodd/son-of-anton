#!/usr/bin/env python3
"""
Memory Tool Module - rules and facts

Built-in memory is split by job, in TOML files under ``$SON_OF_ANTON_HOME/memories``:

  * **Rules** (``rules.toml``, ``[[rule]]`` tables): standing instructions and core
    context that must hold every turn. They are rendered into the system prompt,
    capped (``memory.rules_char_limit``), and EVERY change needs the user's
    approval regardless of ``memory.write_approval``. A full store refuses; it
    never evicts.
  * **Facts** (``facts.toml`` / ``facts.gateway.toml``, ``[[fact]]`` tables):
    knowledge that matters only when its topic comes up. Not in the system prompt
    (retrieval brings them in per turn). The agent writes them freely, subject to
    ``memory.write_approval`` like any memory write.

The rendered rules are a frozen snapshot taken at ``load_from_disk()``: mid-session
writes update the files at once but never change the live prompt, which keeps the
prefix cache stable. Compaction's rebuild path reloads it.

Entries are addressed by a generated id, never by substring. Users hand-edit these
files, so a file that fails to parse is never overwritten (writes refuse) and renders
nothing; unknown keys inside an entry survive a rewrite. The legacy ``MEMORY*.md`` /
``USER*.md`` files are no longer read or written.
"""

import datetime
import fcntl
import json
import logging
import re
import tomllib
from contextlib import contextmanager
from pathlib import Path
from son_of_anton_constants import display_son_of_anton_home, get_son_of_anton_home
from typing import Dict, Any, List, Optional, Tuple

from utils import atomic_write_text, dump_toml, is_truthy_value
from tools.registry import no_cache_check_fn, registry, tool_error

logger = logging.getLogger(__name__)

DEFAULT_RULES_CHAR_LIMIT = 6000
DEFAULT_FACT_CHAR_LIMIT = 400

# Facts this similar (token-set Jaccard) are refused as duplicates.
FACT_SIMILARITY_THRESHOLD = 0.8
# With the embeddings endpoint answering, the cosine at which a new fact is
# refused as a duplicate of an indexed one.
FACT_EMBEDDING_THRESHOLD = 0.92

KINDS: Tuple[str, ...] = ("rule", "fact")
ACTIONS: Tuple[str, ...] = ("add", "replace", "remove")

RULES_FILENAME = "rules.toml"


def get_memory_dir() -> Path:
    """Return the memories directory (resolved per call, honoring home overrides)."""
    return get_son_of_anton_home() / "memories"


# Stable header prefix of the rules block in the system prompt. Exported so
# compaction's prompt-retention check can detect a leftover block after the rules
# were emptied -- keep in lockstep with ScopedMemoryStore._render.
RULES_BLOCK_HEADER = "STANDING RULES (from the user)"

# Built-in memory scopes. The session's scope is active; "common" is visible
# from every session regardless of front-end. Scope is what keeps a coding
# TUI session from carrying the user's personal/health/media context.
MEMORY_SCOPES: Tuple[str, ...] = ("common", "gateway")

# agent.platform values that map to the gateway (messaging) scope.
_GATEWAY_PLATFORMS = frozenset({"signal"})


# Names that older installs, staged approvals and RAG indexes still carry.
_SCOPE_ALIASES = {"common": "common", "shared": "common", "cli": "common", "gateway": "gateway"}


def normalize_memory_scope(name: object) -> Optional[str]:
    """The one place a scope name enters the system.

    ``common`` (and the legacy ``shared`` / ``cli``) -> ``common``;
    ``gateway`` -> ``gateway``; anything else is unknown (``None``) and
    callers must never treat unknown as ``gateway``.
    """
    if not isinstance(name, str):
        return None
    return _SCOPE_ALIASES.get(name.strip().lower())


def facts_filename(scope: str) -> str:
    """Facts file for a scope: ``facts.toml`` (common) or ``facts.<scope>.toml``."""
    return "facts.toml" if scope == "common" else f"facts.{scope}.toml"


def resolve_memory_scope(
    platform: object,
    config: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
) -> str:
    """Map a session's platform to its built-in memory scope.

    Explicit ``memory.scope`` in config wins (normalized; an invalid pin is
    ignored). Otherwise signal gets the personal ``gateway`` scope and
    everything else (cli/tui, delegates, unknown front-ends) gets ``common``.

    A cron run (platform ``cron`` plus a ``cron_<job>_<stamp>`` *session_id*)
    takes the scope RAG places the same session in (``agent.rag._cron_identity``):
    where its job delivers, else the chat that created it, else common. A job
    that is gone or unresolvable gets ``common``; unknown never means gateway.
    """
    section = get_builtin_memory_config(config)
    pinned = normalize_memory_scope(section.get("scope"))
    if pinned:
        return pinned
    name = str(platform or "").strip().lower()
    if name in _GATEWAY_PLATFORMS:
        return "gateway"
    if name == "cron" and session_id:
        try:
            from agent.rag import _cron_identity

            identity = _cron_identity(session_id, {})
        except Exception:
            identity = None
        if identity is not None:
            return normalize_memory_scope(identity[0]) or "common"
    return "common"


# ---------------------------------------------------------------------------
# Memory content scanning — lightweight check for injection/exfiltration
# in content that gets injected into the system prompt.
#
# Patterns live in ``tools/threat_patterns.py`` — the single source of truth
# shared with the context-file scanner and the tool-result delimiter system.
# Memory uses the "strict" scope (broadest pattern set) because rules enter the
# system prompt as a FROZEN snapshot, so a poisoned entry persists for the
# entire session and across sessions until explicitly removed.
# ---------------------------------------------------------------------------

from tools.threat_patterns import first_threat_message as _first_threat_message


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    return _first_threat_message(content, scope="strict")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_SLUG_MAX = 32


def make_entry_id(text: str, taken: set, fallback: str = "entry") -> str:
    """Short slug of *text*, uniquified against *taken* with -2, -3, ...

    Stable once written: an entry's id never changes when its text is replaced.
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    slug = ""
    for word in words[:6]:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > _SLUG_MAX:
            break
        slug = candidate
    slug = slug or (words[0][:_SLUG_MAX] if words else fallback)
    candidate, n = slug, 2
    while candidate in taken:
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


def _tokens(text: str) -> frozenset:
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


def _lexical_similar(text: str, facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    mine = _tokens(text)
    if not mine:
        return []
    similar = []
    for fact in facts:
        theirs = _tokens(str(fact.get("text", "")))
        if not theirs:
            continue
        if len(mine & theirs) / len(mine | theirs) >= FACT_SIMILARITY_THRESHOLD:
            similar.append(fact)
    return similar


def find_similar_facts(
    text: str, facts: List[Dict[str, Any]], scope: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Existing facts near-identical to *text*.

    The one place duplicate detection lives. With a *scope* and the RAG index
    answering, facts that are indexed are compared by embedding (cosine >=
    ``FACT_EMBEDDING_THRESHOLD``); every fact the index does not cover, and
    every fact when it cannot answer (RAG off, endpoint down or slow), is
    compared by normalized token-set Jaccard.
    """
    lexical_pool = facts
    semantic: List[Dict[str, Any]] = []
    if scope is not None:
        try:
            from agent.rag import similar_indexed_facts

            answer = similar_indexed_facts(text, scope, facts, FACT_EMBEDDING_THRESHOLD)
        except Exception:
            logger.debug("semantic duplicate check failed", exc_info=True)
            answer = None
        if answer is not None:
            semantic, covered = answer
            lexical_pool = [f for f in facts if f["id"] not in covered]
    found = {f["id"] for f in semantic}
    return semantic + [f for f in _lexical_similar(text, lexical_pool) if f["id"] not in found]


def read_facts(scope: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """The facts file of *scope* as ``(entries, error)``; the one reader RAG uses."""
    return _read_entries(get_memory_dir() / facts_filename(scope), "fact")


def render_rule_line(rule: Dict[str, Any]) -> str:
    """``- [id] text (why)`` -- the one shape of a rule in the prompt."""
    why = str(rule.get("why") or "").strip()
    suffix = f" ({why})" if why else ""
    return f"- [{rule['id']}] {rule['text']}{suffix}"


def _rules_size(rules: List[Dict[str, Any]]) -> int:
    """Rendered size of *rules*: their lines, newline-joined."""
    return sum(len(render_rule_line(r)) + 1 for r in rules)


def _entry_scope(entry: Dict[str, Any], default: str = "common") -> Optional[str]:
    """Scope of a rules.toml entry; None when it names an unknown scope."""
    raw = entry.get("scope")
    if raw is None:
        return default
    return normalize_memory_scope(raw)


def _read_entries(path: Path, key: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Read ``[[key]]`` tables from *path*: ``(entries, error)``.

    A missing file is ``([], None)``. Anything that exists but cannot be taken
    as a clean list of entries (unreadable, invalid UTF-8 or TOML, wrong shape,
    an entry with no id/text) is an error: callers must refuse to write rather
    than rewrite a file they could not understand.
    """
    if not path.exists():
        return [], None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return [], f"{path.name} could not be parsed ({e})"
    raw = data.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
        return [], f"{path.name}: '{key}' must be an array of tables"
    seen = set()
    for i, entry in enumerate(raw, 1):
        if not isinstance(entry.get("id"), str) or not entry["id"].strip():
            return [], f"{path.name}: [[{key}]] entry {i} has no id"
        if not isinstance(entry.get("text"), str) or not entry["text"].strip():
            return [], f"{path.name}: entry '{entry['id']}' has no text"
        if entry["id"] in seen:
            return [], f"{path.name}: duplicate id '{entry['id']}'"
        seen.add(entry["id"])
    return raw, None


@contextmanager
def _file_lock(path: Path):
    """Exclusive lock around a read-modify-write of *path* (separate .lock file)."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        fd.close()


def _write_entries(path: Path, key: str, entries: List[Dict[str, Any]]) -> None:
    try:
        atomic_write_text(path, dump_toml({key: entries}), tmp_prefix=".mem_")
    except (OSError, IOError) as e:
        raise RuntimeError(f"Failed to write memory file {path}: {e}")


def _kick_fact_index() -> None:
    """Index the facts files in the background so a change is retrievable soon.

    A no-op when RAG is off; never blocks and never raises.
    """
    try:
        from agent.rag import refresh_facts_async

        refresh_facts_async()
    except Exception:
        logger.debug("fact index refresh not started", exc_info=True)


def _fail(message: str, **extra: Any) -> Dict[str, Any]:
    return {"success": False, "error": message, **extra}


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class ScopedMemoryStore:
    """Rules and facts for one session.

    ``active_scope`` is the session's scope: common rules always render, gateway
    rules only when it is ``gateway``. An explicit ``scope`` on a write selects
    any store, which is how one surface files something for another.
    """

    def __init__(
        self,
        active_scope: str = "common",
        *,
        rules_char_limit: int = DEFAULT_RULES_CHAR_LIMIT,
        fact_char_limit: int = DEFAULT_FACT_CHAR_LIMIT,
        memory_enabled: bool = True,
    ):
        self.active_scope = normalize_memory_scope(active_scope) or "common"
        self.rules_char_limit = int(rules_char_limit)
        self.fact_char_limit = int(fact_char_limit)
        self.memory_enabled = memory_enabled
        self._snapshot: str = ""

    # -- scope helpers ------------------------------------------------------

    def visible_scopes(self) -> List[str]:
        return ["common"] if self.active_scope == "common" else ["common", self.active_scope]

    def resolve_scope(self, scope: Optional[str]) -> str:
        """Tool-supplied scope -> canonical; None/''/'current' is the active one."""
        if scope is None:
            return self.active_scope
        name = str(scope).strip().lower()
        if name in {"", "current", "session"}:
            return self.active_scope
        canonical = normalize_memory_scope(name)
        if canonical is None:
            raise KeyError(scope)
        return canonical

    def _path(self, kind: str, scope: str) -> Path:
        name = RULES_FILENAME if kind == "rule" else facts_filename(scope)
        return get_memory_dir() / name

    # -- the frozen prompt snapshot -----------------------------------------

    def load_from_disk(self) -> None:
        """Capture the rules block for the system prompt.

        Each rule is scanned for injection patterns; a hit replaces its text in
        the SNAPSHOT with a placeholder (the file keeps the original so the user
        can see and remove it). Deterministic from disk bytes, so the snapshot is
        stable for the session. Facts are never rendered.
        """
        self._snapshot = self._render()

    def format_for_system_prompt(self) -> Optional[str]:
        return self._snapshot or None

    def _render(self) -> str:
        from tools.threat_patterns import scan_for_threats

        path = self._path("rule", "common")
        rules, error = _read_entries(path, "rule")
        if error:
            logger.warning("Memory rules not rendered: %s", error)
            return ""
        by_scope: Dict[str, List[str]] = {s: [] for s in self.visible_scopes()}
        for rule in rules:
            scope = _entry_scope(rule)
            if scope is None:
                logger.warning(
                    "Rule '%s' in %s names unknown scope %r; not rendered",
                    rule["id"], path.name, rule.get("scope"),
                )
                continue
            if scope not in by_scope:
                continue
            findings = scan_for_threats(f"{rule['text']} {rule.get('why', '')}", scope="strict")
            if findings:
                logger.warning(
                    "Rule '%s' blocked at load time: %s", rule["id"], ", ".join(findings)
                )
                line = (
                    f"- [{rule['id']}] [BLOCKED: rule contained threat pattern(s): "
                    f"{', '.join(findings)}. Removed from system prompt.]"
                )
            else:
                line = render_rule_line(rule)
            by_scope[scope].append(line)
        if not any(by_scope.values()):
            return ""
        used = _rules_size(rules)
        limit = self.rules_char_limit
        pct = min(100, int(used / limit * 100)) if limit > 0 else 0
        header = f"{RULES_BLOCK_HEADER} [{pct}% — {used:,}/{limit:,} chars]"
        separator = "═" * 46
        parts = [f"{separator}\n{header}\n{separator}"]
        for scope in self.visible_scopes():
            if not by_scope[scope]:
                continue
            if scope != "common":
                parts.append(f"[scope: {scope}]")
            parts.append("\n".join(by_scope[scope]))
        return "\n".join(parts)

    # -- mutations ------------------------------------------------------------

    def _list_ids(self, entries: List[Dict[str, Any]], scopes: List[str], kind: str) -> List[str]:
        if kind == "fact":
            return [e["id"] for e in entries]
        return [e["id"] for e in entries if _entry_scope(e) in scopes]

    def apply(
        self,
        kind: str,
        action: str,
        *,
        id: Optional[str] = None,
        text: Optional[str] = None,
        why: Optional[str] = None,
        scope: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Validate and (unless *dry_run*) perform one change.

        Validation runs under the file lock against the freshly-read file, so a
        dry run and the real write apply the same checks. Never overwrites a file
        that failed to parse.
        """
        if kind not in KINDS:
            return _fail(f"Invalid kind '{kind}'. Use 'rule' or 'fact'.")
        if action not in ACTIONS:
            return _fail(
                f"Unknown action '{action}'. Use add, replace or remove. There is no "
                "read action: rules are already in your system prompt and relevant "
                "facts arrive with the turn."
            )
        if not self.memory_enabled:
            return _fail("Built-in memory is disabled in memory config.")
        try:
            dest = self.resolve_scope(scope)
        except KeyError:
            return _fail(
                f"Unknown memory scope '{scope}'. Use one of: {', '.join(MEMORY_SCOPES)}."
            )
        id = (id or "").strip() or None
        if kind == "rule":
            text = " ".join(text.split()) if text else text
            why = " ".join(why.split()) if why else why
        else:
            text = text.strip() if text else text
        why = why if kind == "rule" else None

        if action in {"add", "replace"}:
            if not text:
                return _fail(f"'text' is required for '{action}'.")
            for field in (text, why):
                if field:
                    blocked = _scan_memory_content(field)
                    if blocked:
                        return _fail(blocked)
        if action in {"replace", "remove"} and not id:
            return _fail(f"'id' is required for '{action}'.")

        path = self._path(kind, dest)
        key = kind
        with _file_lock(path):
            entries, error = _read_entries(path, key)
            if error:
                return _fail(
                    f"Refusing to write {path.name}: {error}. The file is left exactly as "
                    "it is -- the user must fix it by hand, then retry."
                )
            if action == "add":
                result = self._add(kind, entries, dest, text, why, path)
            else:
                result = self._change(kind, action, entries, dest, id, text, why, path)
            if "entries" not in result:
                return result
            new_entries = result.pop("entries")
            if not dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_entries(path, key, new_entries)
                if kind == "fact":
                    _kick_fact_index()
        result["success"] = True
        result["kind"] = kind
        result["scope"] = result.pop("scope", dest)
        if kind == "rule":
            result["usage"] = f"{_rules_size(new_entries):,}/{self.rules_char_limit:,} chars"
        if dry_run:
            result["dry_run"] = True
        return result

    def _add(self, kind, entries, dest, text, why, path) -> Dict[str, Any]:
        if kind == "fact":
            if len(text) > self.fact_char_limit:
                return _fail(
                    f"Fact is {len(text)} chars; the limit is {self.fact_char_limit}. Split it "
                    "into several facts, or put the details in a note under notes/ and save a "
                    "pointer fact."
                )
            similar = find_similar_facts(text, entries, dest)
            if similar:
                return _fail(
                    "A near-identical fact already exists; use action='replace' with its id "
                    "instead of adding.",
                    similar=[{"id": f["id"], "text": f["text"]} for f in similar],
                )
        taken = {e["id"] for e in entries}
        new: Dict[str, Any] = {"id": make_entry_id(text, taken, kind), "text": text}
        if kind == "rule":
            if why:
                new["why"] = why
            new["scope"] = dest
        new["source"] = "agent"
        new["added"] = datetime.date.today()
        new_entries = entries + [new]
        if kind == "rule":
            refusal = self._cap_refusal(entries, new_entries)
            if refusal:
                return refusal
        return {"id": new["id"], "entries": new_entries}

    def _change(self, kind, action, entries, dest, id, text, why, path) -> Dict[str, Any]:
        # Rules live in one file shared by both scopes, so an id is found among
        # the scopes this session can see (or the one named explicitly).
        scopes = [dest] if dest != self.active_scope else self.visible_scopes()
        in_scope = self._list_ids(entries, scopes, kind)
        target = next((e for e in entries if e["id"] == id and e["id"] in in_scope), None)
        if target is None:
            where = f" in scope {'/'.join(scopes)}" if kind == "rule" else f" in {path.name}"
            return _fail(
                f"No {kind} with id '{id}'{where}.",
                ids=in_scope,
            )
        if kind == "rule":
            dest = _entry_scope(target) or dest
        if action == "remove":
            return {"id": id, "scope": dest, "entries": [e for e in entries if e is not target]}
        if kind == "fact" and len(text) > self.fact_char_limit:
            return _fail(
                f"Fact is {len(text)} chars; the limit is {self.fact_char_limit}. Split it "
                "into several facts, or put the details in a note under notes/ and save a "
                "pointer fact."
            )
        updated = dict(target)
        updated["text"] = text
        if kind == "rule" and why is not None:
            if why:
                updated["why"] = why
            else:
                updated.pop("why", None)
        new_entries = [updated if e is target else e for e in entries]
        if kind == "rule":
            refusal = self._cap_refusal(entries, new_entries)
            if refusal:
                return refusal
        return {"id": id, "scope": dest, "entries": new_entries}

    def _cap_refusal(self, before, after) -> Optional[Dict[str, Any]]:
        """Refuse a rules change that ends over the cap (and grew the store)."""
        new_total, old_total = _rules_size(after), _rules_size(before)
        if new_total > self.rules_char_limit and new_total > old_total:
            return _fail(
                f"The rules store is full: this change makes it {new_total:,} chars; the cap "
                f"is {self.rules_char_limit:,}. Nothing was changed and nothing is removed "
                "automatically. Tell the user it is full and ask which existing rule they "
                "want to retire (or to shorten); do not pick one yourself."
            )
        return None

    def usage(self) -> str:
        rules, error = _read_entries(self._path("rule", "common"), "rule")
        return f"{_rules_size(rules):,}/{self.rules_char_limit:,} chars" if not error else "unreadable"


def load_on_disk_store() -> "ScopedMemoryStore":
    """Build a fresh on-disk :class:`ScopedMemoryStore`, honoring configured limits.

    Approved writes route to the store named by the staged payload's ``scope``
    (the active scope is ``common`` when there is no live agent). Use this from
    any context that has no live agent (the messaging gateway, the CLI
    ``/memory`` handler); it applies the SAME caps as ``agent/agent_init.py``.
    Falls back to the built-in defaults if config can't be loaded.
    """
    limits = {}
    enabled = True
    try:
        from son_of_anton_cli.config import load_config

        config = load_config() or {}
        limits = _store_limits(get_builtin_memory_config(config))
        enabled = builtin_memory_enabled(config)
    except Exception:
        pass  # config optional — fall back to defaults rather than break /memory
    store = ScopedMemoryStore(active_scope="common", memory_enabled=enabled, **limits)
    store.load_from_disk()
    return store


def _store_limits(section: Dict[str, Any]) -> Dict[str, int]:
    """``memory.rules_char_limit`` / ``memory.fact_char_limit`` as constructor kwargs."""
    return {
        "rules_char_limit": int(section.get("rules_char_limit", DEFAULT_RULES_CHAR_LIMIT)),
        "fact_char_limit": int(section.get("fact_char_limit", DEFAULT_FACT_CHAR_LIMIT)),
    }


def _write_gate(summary: str, detail: str, payload: Dict[str, Any], *, force: bool) -> Optional[str]:
    """Evaluate the approval gate for one write.

    Returns a JSON tool-result string when the write should NOT proceed
    (denied or staged), or None when the caller should perform it. *force*
    makes the gate apply whatever ``memory.write_approval`` says (rules).
    """
    from tools import write_approval as wa

    decision = wa.evaluate_gate(
        wa.MEMORY, inline_summary=summary, inline_detail=detail, force=force
    )
    if decision.allow:
        return None
    if decision.blocked:
        return tool_error(decision.message, success=False)
    record = wa.stage_write(wa.MEMORY, payload, summary=f"{summary}: {detail[:120]}")
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "message": decision.message},
        ensure_ascii=False,
    )


def memory_tool(
    kind: str = None,
    action: str = None,
    id: str = None,
    text: str = None,
    why: str = None,
    scope: str = None,
    store: Optional[Any] = None,
) -> str:
    """Single entry point for the memory tool. Returns a JSON string.

    The change is validated first (a dry run against the file as it is now), so
    an invalid write is rejected immediately rather than staged. Rule writes then
    ALWAYS go through the approval gate; fact writes go through it when
    ``memory.write_approval`` is on.
    """
    if store is None:
        return tool_error("Memory is not available. It may be disabled in config or this environment.", success=False)

    kind = (kind or "").strip().lower() if isinstance(kind, str) else kind
    action = (action or "").strip().lower() if isinstance(action, str) else action
    checked = store.apply(kind, action, id=id, text=text, why=why, scope=scope, dry_run=True)
    if not checked.get("success"):
        return json.dumps(checked, ensure_ascii=False)
    dest = checked["scope"]

    if action == "add":
        summary, detail = f"add {kind} ({dest})", text or ""
    elif action == "replace":
        summary, detail = f"replace {kind} '{id}' ({dest})", text or ""
    else:
        summary, detail = f"remove {kind} '{id}' ({dest})", id or ""
    if kind == "rule" and why and action != "remove":
        detail = f"{detail}\nwhy: {why}"
    payload = {"kind": kind, "action": action, "id": id, "text": text, "why": why, "scope": dest}
    gated = _write_gate(summary, detail, payload, force=(kind == "rule"))
    if gated is not None:
        return gated

    result = store.apply(kind, action, id=id, text=text, why=why, scope=scope)
    if result.get("success"):
        result["note"] = "Saved. This update is complete — do not repeat it."
    return json.dumps(result, ensure_ascii=False)


def apply_memory_pending(payload: Dict[str, Any], store: "ScopedMemoryStore") -> Dict[str, Any]:
    """Replay a staged memory write directly against the store, bypassing the
    gate (the user just approved it). Called by the /memory approve handler.

    A payload in the pre-redesign shape (target/content/old_text/operations) is
    rejected: it cannot be mapped onto rules and facts.
    """
    if "kind" not in payload or any(k in payload for k in ("target", "content", "old_text", "operations")):
        return _fail(
            "This staged write predates the rules/facts memory format and cannot be "
            "applied. Reject it and have the agent save it again."
        )
    return store.apply(
        payload.get("kind"),
        payload.get("action"),
        id=payload.get("id"),
        text=payload.get("text"),
        why=payload.get("why"),
        scope=payload.get("scope"),
    )


def get_builtin_memory_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a normalized built-in memory config mapping.

    Missing, unreadable, or malformed sections become an empty mapping, whose
    missing flags resolve to the enabled defaults. ``agent_init`` consumes this
    same normalized section so tool availability and store construction cannot
    diverge.
    """
    if config is None:
        try:
            from son_of_anton_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            logger.debug("Could not read memory config for availability", exc_info=True)
            return {}

    section = config.get("memory") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def builtin_memory_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """``memory.memory_enabled`` from resolved config (default on)."""
    return is_truthy_value(get_builtin_memory_config(config).get("memory_enabled"), default=True)


@no_cache_check_fn
def check_memory_requirements() -> bool:
    """Report whether the built-in memory tool is available (config-backed, uncached)."""
    return builtin_memory_enabled()


MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save to memory. A rule (kind='rule') is a standing instruction or core context that must "
        "hold every turn: rules sit in your system prompt, are capped, and every change needs the "
        "user's approval. A fact (kind='fact') is knowledge that matters only when its topic comes "
        "up (hosts, IPs, quirks, procedures, personal details): saved freely, kept out of the "
        "prompt, and relevant facts arrive with the turn, so there is no read action.\n"
        "replace and remove address an entry by id; add returns the id. A full rules store refuses: "
        "ask the user which rule to retire. Skip task progress and anything stale within a week. "
        f"Files: {display_son_of_anton_home()}/memories/rules.toml, facts.toml, facts.gateway.toml."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": list(KINDS),
                "description": "'rule' (standing, approved by the user) or 'fact' (looked up when relevant).",
            },
            "action": {"type": "string", "enum": list(ACTIONS)},
            "id": {"type": "string", "description": "Entry id, for replace and remove."},
            "text": {
                "type": "string",
                "description": "Rule: an instruction. Fact: a declarative statement. For add and replace.",
            },
            "why": {"type": "string", "description": "Rules only: the reason, optional."},
            "scope": {
                "type": "string",
                "enum": ["current", "common", "gateway"],
                "description": (
                    "Omit for this session's own. 'common': every session. "
                    "'gateway': personal, never shown in the terminal."
                ),
            },
        },
        "required": ["kind", "action"],
    },
}


registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        kind=args.get("kind"),
        action=args.get("action"),
        id=args.get("id"),
        text=args.get("text"),
        why=args.get("why"),
        scope=args.get("scope"),
        store=kw.get("store")),
    check_fn=check_memory_requirements,
    emoji="🧠",
)
