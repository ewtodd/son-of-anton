"""What Tab (and the inline list) can complete at the prompt.

The prompt_toolkit REPL's completer went with the REPL (9906a30b) and the
Textual prompt kept only command names.  This is the rest of it, minus the
skill / personality / plugin branches whose features are gone:

  * a slash command's arguments (``/reasoning hi`` → ``high``, ``/skin``,
    ``/tools enable <toolset>``);
  * ``@`` context references (``@diff``, ``@file:src/…``, fuzzy ``@que``) —
    expanded at send time by ``agent.context_references``;
  * file paths in ordinary text (``./RE``, ``~/Doc``, ``src/ma``).

Pure on purpose: nothing here imports Textual, the slow parts (the project
file list, config reads) are injected, and the TUI only maps offsets to
cursor locations and draws the list.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional, Sequence

LIMIT = 40

# ``@`` references that need nothing after them, and the prefixes that take a
# value.  Kept in step with REFERENCE_PATTERN in agent/context_references.py.
_SIMPLE_REFS = (("@diff", "git working-tree diff"), ("@staged", "git staged diff"))
_VALUE_REFS = (
    ("@file:", "attach a file"),
    ("@folder:", "attach a folder"),
    ("@git:", "git log with diffs, e.g. @git:5"),
    ("@url:", "fetch a web page"),
)
_QUOTES = "\"'`"
# ``@file:"my dir/…`` with the quote still open: the word runs past spaces.
_OPEN_QUOTED_REF = re.compile(r"""(?<![\w/])@(?:file|folder):(["'`])(?:(?!\1)[^\n])*$""", re.IGNORECASE)


@dataclass(frozen=True)
class Candidate:
    insert: str  # replaces the word being completed
    display: str  # left column of the list
    meta: str = ""  # dim right column


@dataclass(frozen=True)
class Completions:
    start: int  # offset in the text before the cursor where the word begins
    items: tuple[Candidate, ...]
    # True: the list may pop open while typing.  False: Tab only — prose is
    # full of slashes, and an open list would turn Enter into "accept".
    auto: bool


ArgSource = Callable[[list, str], list]


def complete(
    before: str,
    *,
    explicit: bool,
    commands: Iterable[tuple],
    cwd: str,
    subcommands: Optional[Mapping[str, Sequence[str]]] = None,
    arg_sources: Optional[Mapping[str, ArgSource]] = None,
    project_files: Optional[Callable[[str], list]] = None,
    limit: int = LIMIT,
) -> Optional[Completions]:
    """Completions for the word ending at the cursor, or None.

    ``before`` is the prompt text up to the cursor.  ``commands`` are the
    palette's ``(command, category, description)`` rows; ``explicit`` is True
    for Tab, False while typing (then only ``auto`` results come back).
    """
    start = _word_start(before)
    word = before[start:]
    result: Optional[Completions] = None
    quoted = _OPEN_QUOTED_REF.search(before)
    if quoted:
        start = quoted.start()
        word = before[start:]
        kind = "folder" if word.lower().startswith("@folder") else "file"
        result = _ref_paths(kind, word, start, cwd, limit)
    elif before.startswith("/") and start == 0:
        result = _command_names(word, commands, limit)
    elif before.startswith("/") and "\n" not in before:
        result = _slash_argument(before, start, word, commands, subcommands, arg_sources, limit)
    if result is None and word.startswith("@"):
        result = _context_refs(word, start, cwd, project_files, explicit, limit)
    if result is None and explicit and _is_path_word(word) and not (start == 0 and _is_command_word(word, commands)):
        result = _paths(word, start, cwd, limit)
    if result is None or not result.items or (not explicit and not result.auto):
        return None
    return result


def insertion(cand: Candidate, after: str) -> str:
    """The text to insert: no doubled space when a space already follows."""
    if cand.insert.endswith(" ") and after[:1] in (" ", "\t"):
        return cand.insert[:-1]
    return cand.insert


def apply(text: str, cursor: int, comps: Completions, cand: Candidate) -> tuple[str, int]:
    """``text`` with the candidate accepted at ``cursor``, and the new cursor."""
    insert = insertion(cand, text[cursor:])
    return text[: comps.start] + insert + text[cursor:], comps.start + len(insert)


# ---------------------------------------------------------------- words
def _word_start(before: str) -> int:
    i = len(before)
    while i > 0 and not before[i - 1].isspace():
        i -= 1
    return i


def _is_path_word(word: str) -> bool:
    if not word or "://" in word:
        return False
    return word.startswith(("./", "../", "~/", "/")) or "/" in word


def _is_command_word(word: str, commands: Iterable[tuple]) -> bool:
    lw = word.lower()
    return any(row[0].lower().startswith(lw) for row in commands)


def _short_desc(desc: str) -> str:
    return (desc or "").split(" (usage:")[0].strip()


# ---------------------------------------------------------------- slash commands
def _command_names(word: str, commands: Iterable[tuple], limit: int) -> Optional[Completions]:
    lw = word.lower()
    seen: set[str] = set()
    items: list[Candidate] = []
    for row in commands:
        cmd, desc = row[0], row[-1]
        if cmd in seen or not cmd.lower().startswith(lw):
            continue
        seen.add(cmd)
        items.append(Candidate(cmd + " ", cmd, _short_desc(desc)))
    if not items or (len(items) == 1 and items[0].display.lower() == lw):
        return None
    return Completions(0, tuple(items[:limit]), auto=True)


def _slash_argument(before, start, word, commands, subcommands, arg_sources, limit) -> Optional[Completions]:
    base = before.split(None, 1)[0].lower()
    if base not in {row[0].lower() for row in commands}:
        return None  # not a command this surface offers (or not a command at all)
    canonical = _canonical(base)
    args = before[len(base):].split()
    completed = args if before[-1:].isspace() else args[:-1]
    sources = DEFAULT_ARG_SOURCES if arg_sources is None else arg_sources
    try:
        if canonical in sources:
            items = list(sources[canonical](list(completed), word))
        elif not completed:
            subs = _subcommands(subcommands).get(canonical) or ()
            lw = word.lower()
            items = [Candidate(s + " ", s) for s in subs if s.lower().startswith(lw) and s.lower() != lw]
        else:
            items = []
    except Exception:
        items = []
    if not items:
        return None
    return Completions(start, tuple(items[:limit]), auto=bool(word))


def _canonical(base: str) -> str:
    try:
        from son_of_anton_cli.commands import resolve_command

        cmd = resolve_command(base)
    except Exception:
        cmd = None
    return f"/{cmd.name}" if cmd is not None else base


def _subcommands(subcommands):
    if subcommands is not None:
        return subcommands
    from son_of_anton_cli.commands import SUBCOMMANDS

    return SUBCOMMANDS


def _skin_args(completed: list, word: str) -> list:
    if completed:
        return []
    from son_of_anton_cli.skin_engine import list_skins

    lw = word.lower()
    return [
        Candidate(s["name"] + " ", s["name"], s.get("description") or s.get("source", ""))
        for s in list_skins()
        if s["name"].lower().startswith(lw) and s["name"].lower() != lw
    ]


def _tools_args(completed: list, word: str) -> list:
    """``/tools`` first takes list|disable|enable, then toolsets to flip."""
    lw = word.lower()
    if not completed:
        return [Candidate(s + " ", s) for s in ("list", "disable", "enable") if s.startswith(lw) and s != lw]
    action = completed[0].lower()
    if action not in ("enable", "disable"):
        return []
    from son_of_anton_cli.config import load_config_readonly
    from son_of_anton_cli.tools_config import CONFIGURABLE_TOOLSETS, _get_platform_tools

    config = load_config_readonly()
    enabled = _get_platform_tools(config, "cli", include_default_mcp_servers=False)
    already = set(completed[1:])
    items = []
    for key, label, _desc in CONFIGURABLE_TOOLSETS:
        if key in already or not key.startswith(lw) or key == lw:
            continue
        if (action == "enable") == (key in enabled):
            continue  # only offer what the action would change
        items.append(Candidate(key + " ", key, label.split(" ", 1)[-1].strip()))
    servers = config.get("mcp_servers") or {}
    if isinstance(servers, dict):
        for server in sorted(servers):
            prefix = f"{server}:"
            if prefix.startswith(word) and prefix != word:
                items.append(Candidate(prefix, prefix, f"MCP server {server}"))
    return items


DEFAULT_ARG_SOURCES: Mapping[str, ArgSource] = {"/skin": _skin_args, "/tools": _tools_args}


# ---------------------------------------------------------------- @ references
def _context_refs(word, start, cwd, project_files, explicit, limit) -> Optional[Completions]:
    lw = word.lower()
    if lw in ("@diff", "@staged"):
        return None  # complete as typed
    for kind in ("file", "folder"):
        if lw == f"@{kind}" or lw.startswith(f"@{kind}:"):
            return _ref_paths(kind, word, start, cwd, limit)
    if ":" in word:
        return None  # @git:N, @url:…, or something we don't know: the value is the user's
    items = [Candidate(ref + " ", ref, meta) for ref, meta in _SIMPLE_REFS if ref.startswith(lw) and ref != lw]
    items += [Candidate(ref, ref, meta) for ref, meta in _VALUE_REFS if ref.startswith(lw)]
    files = []
    if project_files is not None:
        try:
            files = project_files(cwd) or []
        except Exception:
            files = []
    query = word[1:]
    if query:
        # While typing only substring matches count: "thanks @team" must not
        # turn Enter into "attach some t…e…a…m file".
        floor = 1 if explicit else 40
        scored = sorted(
            ((s, fp) for fp in files if (s := score_path(fp, query)) >= floor),
            key=lambda t: (-t[0], t[1]),
        )
        files = [fp for _, fp in scored]
    for fp in files[: max(0, limit - len(items))]:
        items.append(Candidate(f"@file:{_quote(fp)} ", os.path.basename(fp), fp))
    return Completions(start, tuple(items[:limit]), auto=True)


def _ref_paths(kind, word, start, cwd, limit) -> Optional[Completions]:
    prefix = f"@{kind}:"
    value = word[len(prefix):] if word.lower().startswith(prefix) else ""
    if value and value[0] in _QUOTES:
        value = value[1:].rstrip(value[0])
    items = []
    for path, is_dir, full in _list_dir(value, cwd, dirs_only=kind == "folder", limit=limit):
        if is_dir:
            # Unclosed quote on a spaced directory: the path keeps going.
            insert = prefix + (f'"{path}' if _needs_quote(path) else path)
            items.append(Candidate(insert, os.path.basename(path[:-1]) + "/", "dir"))
        else:
            items.append(Candidate(f"{prefix}{_quote(path)} ", os.path.basename(path), _size(full)))
    if not items:
        return None
    return Completions(start, tuple(items), auto=True)


def _needs_quote(path: str) -> bool:
    return any(c.isspace() for c in path)


def _quote(path: str) -> str:
    if not _needs_quote(path):
        return path
    q = next((q for q in _QUOTES if q not in path), '"')
    return f"{q}{path}{q}"


def score_path(filepath: str, query: str) -> int:
    """Fuzzy rank of a project file for ``@query``: higher is better, 0 is no match."""
    if not query:
        return 1
    name = os.path.basename(filepath).lower()
    q = query.lower()
    if name == q:
        return 100
    if name.startswith(q):
        return 80
    if q in name:
        return 60
    if q in filepath.lower():
        return 40
    # Letters in order (``fo`` → ``file_operations``), better on word boundaries.
    qi = hits = 0
    prev = "_"
    for c in name:
        if qi < len(q) and c == q[qi]:
            hits += prev in "_-./"
            qi += 1
        prev = c
    if qi < len(q):
        return 0
    return 35 if hits >= len(q) * 0.5 else 25


# ---------------------------------------------------------------- paths
def _paths(word, start, cwd, limit) -> Optional[Completions]:
    items = []
    for path, is_dir, full in _list_dir(word, cwd, limit=limit):
        name = os.path.basename(path.rstrip("/"))
        if is_dir:
            items.append(Candidate(path, name + "/", "dir"))
        else:
            items.append(Candidate(path + " ", name, _size(full)))
    if not items:
        return None
    return Completions(start, tuple(items), auto=False)


def _list_dir(typed: str, cwd: str, *, dirs_only: bool = False, limit: int = LIMIT) -> list:
    """Entries completing ``typed`` as ``(path as typed + name [+ "/"], is_dir, full path)``.

    The directory part stays exactly as typed (``~`` stays ``~``, relative stays
    relative).  Dotfiles show only once the name being typed starts with ``.``.
    """
    cut = typed.rfind("/") + 1
    dirpart, prefix = typed[:cut], typed[cut:]
    base = os.path.expanduser(dirpart) if dirpart else "."
    search = base if os.path.isabs(base) else os.path.join(cwd, base)
    lp = prefix.lower()
    found = []
    try:
        with os.scandir(search) as it:
            for entry in it:
                name = entry.name
                if name.startswith(".") and not prefix.startswith("."):
                    continue
                if not name.lower().startswith(lp):
                    continue
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                if dirs_only and not is_dir:
                    continue
                found.append((name, is_dir, entry.path))
    except OSError:
        return []
    found.sort(key=lambda t: t[0].lower())
    return [(dirpart + name + ("/" if is_dir else ""), is_dir, full) for name, is_dir, full in found[:limit]]


def _size(path: str) -> str:
    try:
        from son_of_anton_cli.sizefmt import format_bytes

        return format_bytes(os.path.getsize(path))
    except Exception:
        return ""


# ---------------------------------------------------------------- project files
def list_project_files(cwd: str, *, cap: int = 5000, timeout: float = 2.0) -> list:
    """Files under ``cwd`` (``rg --files``: .gitignore respected), newest first."""
    if not shutil.which("rg"):
        return []
    try:
        proc = subprocess.run(
            ["rg", "--files"], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        out = proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        # A huge tree (the TUI started in ~): keep what rg found in time.
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
    except OSError:
        return []
    files = [line for line in out.splitlines() if line][:cap]

    def mtime(rel: str) -> float:
        try:
            return os.stat(os.path.join(cwd, rel)).st_mtime
        except OSError:
            return 0.0

    return sorted(files, key=mtime, reverse=True)


class ProjectFiles:
    """The project file list for ``@`` completion, refreshed off the UI thread.

    A call never blocks: it answers from the cache (empty the first time) and
    starts a background refresh when the entry is missing or stale;
    ``on_update(cwd)`` fires when fresh results land so the list can redraw.
    """

    def __init__(
        self,
        on_update: Optional[Callable[[str], None]] = None,
        *,
        ttl: float = 10.0,
        loader: Callable[[str], list] = list_project_files,
    ) -> None:
        self._on_update = on_update
        self._ttl = ttl
        self._loader = loader
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, list]] = {}
        self._pending: set[str] = set()

    def __call__(self, cwd: str) -> list:
        with self._lock:
            entry = self._cache.get(cwd)
            stale = entry is None or time.monotonic() - entry[0] > self._ttl
            if stale and cwd not in self._pending:
                self._pending.add(cwd)
                threading.Thread(target=self._load, args=(cwd,), daemon=True).start()
        return list(entry[1]) if entry else []

    def _load(self, cwd: str) -> None:
        try:
            files = self._loader(cwd)
        except Exception:
            files = []
        with self._lock:
            self._cache[cwd] = (time.monotonic(), files)
            self._pending.discard(cwd)
        if self._on_update is not None:
            try:
                self._on_update(cwd)
            except Exception:
                pass
