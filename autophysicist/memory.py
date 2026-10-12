"""Permanent memory and scratchpad for the autophysicist Research Manager.

Because each iteration runs with fresh LLM context, these two files are the
Manager's only durable state between iterations:

* :class:`PermanentMemory` — append-only. Reserved for independently
  verified results. Every entry is injected into every future iteration
  while the whole file fits its share of the model's context window; past
  that, a bounded view is injected instead (see below).
* :class:`Scratchpad` — append-only on disk, but only the *last N* entries
  (default ``window_size=5``) are injected into the next iteration.
  Storage is full; visibility is windowed — the window is a render-time
  concern, not a retention policy. Older entries still live in
  ``SCRATCHPAD.md`` for git history and post-hoc analysis.

Consequence for the Manager prompt: important intermediate results must be
promoted to permanent memory *before* they scroll off the scratchpad
window, otherwise the next iteration will not see them.

Bounding permanent memory
-------------------------

Memory is append-only and used to be injected in full every iteration, so a
long run eventually built a prompt larger than the model's window — and from
then on every iteration failed. :meth:`PermanentMemory.read_for_prompt`
bounds what is injected to a token budget (a share of the window, set by the
runner through :meth:`PermanentMemory.configure_view`):

* While the whole file fits, it is injected verbatim — exactly as before.
* Past that, the newest entries are injected in full and the older ones as a
  **digest** persisted in ``PERMANENT_MEMORY_DIGEST.md``. The digest records
  how many of the oldest entries it covers and a hash of their text; it is
  rebuilt only when that hash stops matching (the file was edited), and
  extended only when the full-text part outgrows its share — and then folded
  far enough back that it is stable for several more iterations, rather than
  churning (and re-costing a model call) every iteration.
* The digest is written by the model when the runner supplies a condenser,
  and by a deterministic excerpt otherwise or whenever that call fails or
  overshoots — so bounding never depends on a model call succeeding.
* ``PERMANENT_MEMORY.md`` itself is never rewritten. It stays the complete,
  authoritative record, and the view tells the Manager to read it from the
  workspace when it needs an older entry verbatim.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .utils.markdown import tail_entries

#: Conservative characters-per-token for sizing prompts without a tokenizer.
#: English prose is ~4; maths, code and numbers tokenize denser, and
#: overestimating only means bounding a little early.
CHARS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    """A deliberately pessimistic token estimate for *text*."""
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def clip_middle(text: str, max_chars: int, note: str) -> str:
    """Keep the head and tail of *text* within *max_chars*, marking the cut."""
    if len(text) <= max_chars:
        return text
    marker = f"\n\n[... {len(text) - max_chars:,} characters omitted — {note} ...]\n\n"
    keep = max(max_chars - len(marker), 0)
    head = (keep * 2) // 3
    tail = keep - head
    return text[:head] + marker + (text[-tail:] if tail else "")


_ENTRY_RE = re.compile(r"^## Iteration \d+ — ", re.MULTILINE)
_DIGEST_HEADER_RE = re.compile(r"^<!-- digest covers=(\d+) sha256=([0-9a-f]{64}) -->\n")

#: Share of the memory budget the newest full-text entries may fill before
#: older ones are folded into the digest, and how far back a fold goes. The
#: gap is the hysteresis that keeps the digest stable between folds.
_RECENT_SHARE = 0.7
_RECENT_AFTER_FOLD = 0.45
#: Share of the memory budget the digest itself may take.
_DIGEST_SHARE = 0.3

Condenser = Callable[[str, int], str]


def _split_entries(text: str) -> tuple[str, list[str]]:
    """``(preamble, entries)`` — entries are the ``## Iteration N — ...`` blocks
    :meth:`PermanentMemory.append` writes, each with its trailing text."""
    starts = [m.start() for m in _ENTRY_RE.finditer(text)]
    if not starts:
        return text, []
    entries = [
        text[a:b].strip("\n") for a, b in zip(starts, starts[1:] + [len(text)])
    ]
    return text[: starts[0]], entries


def _entries_hash(entries: list[str]) -> str:
    return hashlib.sha256("\n\n".join(entries).encode("utf-8")).hexdigest()


def _entry_title(entry: str) -> str:
    return entry.splitlines()[0].lstrip("# ").strip() if entry else ""


def _excerpt_digest(previous: str, entries: list[str], max_chars: int) -> str:
    """The deterministic digest: each entry's heading and opening lines."""
    blocks = []
    if previous.strip():
        blocks.append(previous.strip())
    per_entry = max((max_chars // max(len(entries), 1)) - 80, 200)
    for entry in entries:
        lines = entry.splitlines()
        body = "\n".join(lines[1:]).strip()
        blocks.append(
            f"- {_entry_title(entry)}: "
            + clip_middle(body, per_entry, "full entry in PERMANENT_MEMORY.md")
        )
    return clip_middle(
        "\n".join(blocks), max_chars, "full text in PERMANENT_MEMORY.md"
    )


class PermanentMemory:
    """Append-only persistent memory, injected in full or as a bounded view."""

    FILENAME = "PERMANENT_MEMORY.md"
    DIGEST_FILENAME = "PERMANENT_MEMORY_DIGEST.md"

    def __init__(self, workspace_root: Path):
        self._path = workspace_root / self.FILENAME
        self._digest_path = workspace_root / self.DIGEST_FILENAME
        self._budget_tokens = 0
        self._condense: Optional[Condenser] = None
        if not self._path.exists():
            self._path.write_text("# Permanent Memory\n\n", encoding="utf-8")

    def configure_view(
        self, budget_tokens: int, condense: Optional[Condenser] = None
    ) -> None:
        """Bound :meth:`read_for_prompt` to *budget_tokens* (0 = unbounded).

        *condense(text, target_tokens)* writes the digest of older entries; a
        failure, an empty reply or one over target falls back to excerpts.
        """
        self._budget_tokens = max(int(budget_tokens or 0), 0)
        self._condense = condense

    def append(self, content: str, iteration: int) -> str:
        """Append an iteration-tagged entry. Return confirmation message."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        entry = f"\n## Iteration {iteration} — {timestamp}\n\n{content.strip()}\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(entry)
        return f"Written to permanent memory (iteration {iteration}, {len(content)} chars)."

    def read_full(self) -> str:
        """Read the entire permanent memory file."""
        return self._path.read_text(encoding="utf-8") if self._path.exists() else ""

    @property
    def size_chars(self) -> int:
        return len(self.read_full())

    # --- the bounded view ---------------------------------------------------

    def read_for_prompt(self) -> str:
        """What to inject into a prompt: the whole file while it fits."""
        full = self.read_full()
        budget = self._budget_tokens
        if not budget or estimate_tokens(full) <= budget:
            return full
        _preamble, entries = _split_entries(full)
        if not entries:
            return clip_middle(
                full, budget * CHARS_PER_TOKEN, "full text in PERMANENT_MEMORY.md"
            )

        covered, digest = self._current_digest(entries)
        recent = entries[covered:]
        if estimate_tokens("\n\n".join(recent)) > budget * _RECENT_SHARE:
            covered = self._fold_point(entries, budget)
            digest = self._write_digest(entries, covered)
            recent = entries[covered:]
        return self._render_view(entries, covered, digest, recent, budget)

    def _current_digest(self, entries: list[str]) -> tuple[int, str]:
        """``(entries covered, digest text)`` of the persisted digest, if valid."""
        if not self._digest_path.exists():
            return 0, ""
        text = self._digest_path.read_text(encoding="utf-8")
        match = _DIGEST_HEADER_RE.match(text)
        if not match:
            return 0, ""
        covered = int(match.group(1))
        if covered > len(entries) or _entries_hash(entries[:covered]) != match.group(2):
            return 0, ""
        return covered, text[match.end():].strip()

    @staticmethod
    def _fold_point(entries: list[str], budget: int) -> int:
        """Fold the oldest entries until the rest fit the post-fold share.

        The newest entry always stays in full text — it is what the last
        iteration established.
        """
        limit = budget * _RECENT_AFTER_FOLD
        covered = 0
        while covered < len(entries) - 1 and (
            estimate_tokens("\n\n".join(entries[covered:])) > limit
        ):
            covered += 1
        return covered

    def _write_digest(self, entries: list[str], covered: int) -> str:
        """Extend the persisted digest to cover the oldest *covered* entries."""
        old_covered, old_digest = self._current_digest(entries)
        if old_covered > covered:
            old_covered, old_digest = 0, ""
        new_entries = entries[old_covered:covered]
        target_tokens = max(int(self._budget_tokens * _DIGEST_SHARE), 1)
        max_chars = target_tokens * CHARS_PER_TOKEN

        digest = ""
        if self._condense is not None and new_entries:
            source = (
                (f"<existing_digest>\n{old_digest}\n</existing_digest>\n\n" if old_digest else "")
                + "<entries_to_add>\n"
                + "\n\n".join(new_entries)
                + "\n</entries_to_add>"
            )
            try:
                digest = (self._condense(source, target_tokens) or "").strip()
            except Exception:  # noqa: BLE001 — the excerpt digest is the fallback
                digest = ""
            if len(digest) > max_chars * 1.25:
                digest = ""
        if not digest:
            digest = _excerpt_digest(old_digest, new_entries, max_chars)
        digest = clip_middle(digest, max_chars, "full text in PERMANENT_MEMORY.md")

        header = (
            f"<!-- digest covers={covered} sha256={_entries_hash(entries[:covered])} -->\n"
        )
        self._digest_path.write_text(header + digest + "\n", encoding="utf-8")
        return digest

    def _render_view(
        self,
        entries: list[str],
        covered: int,
        digest: str,
        recent: list[str],
        budget: int,
    ) -> str:
        first = _entry_title(entries[0]) if covered else ""
        last = _entry_title(entries[covered - 1]) if covered else ""
        recent_text = "\n\n".join(recent)
        recent_max = int(budget * _RECENT_SHARE) * CHARS_PER_TOKEN
        recent_text = clip_middle(
            recent_text, recent_max, "full text in PERMANENT_MEMORY.md"
        )
        parts = [
            "# Permanent Memory (bounded view)",
            "",
            "Permanent memory has outgrown its share of the context window. "
            f"The oldest {covered} of {len(entries)} entries are condensed "
            "below; the newest are shown in full. The complete, authoritative "
            f"text is in {self.FILENAME} in your workspace — read it there "
            "when you need an older entry verbatim.",
        ]
        if covered:
            parts += [
                "",
                f"## Digest of entries {first} … {last}",
                "",
                digest or "(no digest available)",
            ]
        parts += ["", "## Recent entries (full text)", "", recent_text]
        return "\n".join(parts) + "\n"


class Scratchpad:
    """Rolling append-only scratchpad. Only last N entries injected."""

    FILENAME = "SCRATCHPAD.md"

    def __init__(self, workspace_root: Path, window_size: int = 5):
        self._path = workspace_root / self.FILENAME
        self._window_size = window_size
        self._entry_count = 0
        if not self._path.exists():
            self._path.write_text("# Scratchpad\n\n")
        else:
            # Count existing entries for resume
            text = self._path.read_text()
            self._entry_count = text.count("\n## Iteration ")

    def append(self, content: str, iteration: int) -> str:
        """Append an iteration-tagged entry. Return confirmation message."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        entry = f"\n## Iteration {iteration} — {timestamp}\n\n{content.strip()}\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(entry)
        self._entry_count += 1
        visible = min(self._entry_count, self._window_size)
        return (
            f"Written to scratchpad (iteration {iteration}, {len(content)} chars). "
            f"{visible} of {self._entry_count} entries visible next iteration."
        )

    def read_window(self) -> str:
        """Read the last N entries for context injection."""
        text = self._path.read_text() if self._path.exists() else ""
        if not text.strip():
            return ""
        return tail_entries(text, self._window_size)

    def read_full(self) -> str:
        """Read entire scratchpad (for logging/debug)."""
        return self._path.read_text() if self._path.exists() else ""

    @property
    def entry_count(self) -> int:
        return self._entry_count
