"""Shared helpers for the gateway: phone redaction and fence-aware
markdown chunking."""

import logging
import re


logger = logging.getLogger(__name__)


# ─── Phone Number Redaction ──────────────────────────────────────────────────


def redact_phone(phone: str) -> str:
    """Redact a phone number for logging, preserving country code and last 4.

    Replaces the identical ``_redact_phone()`` functions in signal.py,
    the plain-text adapters.
    """
    if not phone:
        return "<none>"
    if len(phone) <= 8:
        return phone[:2] + "****" + phone[-2:] if len(phone) > 4 else "****"
    return phone[:4] + "****" + phone[-4:]


# ─── Fence-Aware Markdown Chunking ───────────────────────────────────────────
# Shared core for the fence-aware markdown chunkers that previously lived as
# near-duplicates in gateway/stream_consumer.py
# (MarkdownProcessor — the richest version, which this core is derived from),
# Each caller keeps its own knobs:
#
#   * stream_consumer: newline-preferred splitting + close/reopen fence
#     balancing (``prefer_paragraphs=False, balance_fences=True``)
#   * Some callers: atomic-block extraction + paragraph-boundary splitting, fences
#     kept intact as atoms (``prefer_paragraphs=True, balance_fences=False``)
#
# The typing helpers below use ``Optional``/``Callable`` from ``typing`` to
# match the module's existing import style.


def text_has_unclosed_fence(text: str) -> bool:
    """Return True when *text* ends inside an unclosed ``` code fence.

    Scans line by line, toggling in/out state on lines starting with ```.
    An odd number of toggles means the trailing fence is unclosed.
    """
    in_fence = False
    for line in text.split('\n'):
        if line.startswith('```'):
            in_fence = not in_fence
    return in_fence


def is_fence_atom(text: str) -> bool:
    """True when an atomic block is a code block (starts with ```)."""
    return text.lstrip().startswith('```')


def is_table_atom(text: str) -> bool:
    """True when an atomic block is a table (first line is ``|...|``)."""
    first_line = text.split('\n')[0].strip()
    return first_line.startswith('|') and first_line.endswith('|')


_SENTENCE_END_NEWLINE_RE = re.compile(r'[。！？.!?]\n')


def split_at_paragraph_boundary(text, max_chars, len_fn=None):
    """Find the nearest paragraph boundary within *max_chars*; return (head, tail).

    Split priority:
      1. Blank line (paragraph boundary)
      2. Newline after sentence-ending punctuation (CJK and ASCII)
      3. Last newline
      4. Force split at the *max_chars* window boundary

    ``head + tail == text`` always holds.  *len_fn* allows measuring in
    custom units (e.g. UTF-16 code units); a binary search finds the largest
    prefix that fits when it is provided.
    """
    _len = len_fn or len
    if _len(text) <= max_chars:
        return text, ''

    if _len is len:
        window = text[:max_chars]
    else:
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _len(text[:mid]) <= max_chars:
                lo = mid
            else:
                hi = mid - 1
        window = text[:lo]

    # 1. Prefer the last blank line (\n\n) as paragraph boundary
    pos = window.rfind('\n\n')
    if pos > 0:
        return text[:pos + 2], text[pos + 2:]

    # 2. Then the last newline following sentence-ending punctuation
    best_pos = -1
    for m in _SENTENCE_END_NEWLINE_RE.finditer(window):
        best_pos = m.end()
    if best_pos > 0:
        return text[:best_pos], text[best_pos:]

    # 3. Fallback: last newline
    pos = window.rfind('\n')
    if pos > 0:
        return text[:pos + 1], text[pos + 1:]

    # 4. No valid split point: force split at the window boundary
    cut = len(window)
    return text[:cut], text[cut:]


def split_markdown_atoms(text: str) -> "list[str]":
    """Split markdown into indivisible "atomic blocks".

    Atoms are: fenced code blocks (``` ... ``` inclusive), tables
    (consecutive ``|...|`` lines), and plain paragraphs separated by blank
    lines.  Blank lines are separators and belong to no atom.
    """
    lines = text.split('\n')
    atoms: "list[str]" = []

    current_lines: "list[str]" = []
    in_fence = False

    def _is_table_line(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith('|') and stripped.endswith('|')

    def _flush_current() -> None:
        if current_lines:
            atom = '\n'.join(current_lines)
            if atom.strip():
                atoms.append(atom)
            current_lines.clear()

    for line in lines:
        if in_fence:
            current_lines.append(line)
            if line.startswith('```') and len(current_lines) > 1:
                in_fence = False
                _flush_current()
        elif line.startswith('```'):
            _flush_current()
            in_fence = True
            current_lines.append(line)
        elif _is_table_line(line):
            if current_lines and not _is_table_line(current_lines[-1]):
                _flush_current()
            current_lines.append(line)
        elif line.strip() == '':
            _flush_current()
        else:
            if current_lines and _is_table_line(current_lines[-1]):
                _flush_current()
            current_lines.append(line)

    _flush_current()

    return atoms


def balance_fences_across_chunks(chunks: "list[str]") -> "list[str]":
    """Close orphaned ``` fences at each chunk boundary and reopen on the next.

    When a split lands inside a triple-backtick code block, close the fence
    at the end of the head chunk and reopen it (with the original language
    tag) at the start of the next, so every delivered chunk is
    fence-balanced on its own.
    """
    if len(chunks) <= 1:
        return chunks
    out: "list[str]" = []
    carry_lang = None
    for chunk in chunks:
        prefix = f"```{carry_lang}\n" if carry_lang is not None else ""
        in_code = carry_lang is not None
        lang = carry_lang or ""
        for line in chunk.split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_code:
                    in_code = False
                    lang = ""
                else:
                    in_code = True
                    tag = stripped[3:].strip()
                    lang = tag.split()[0] if tag else ""
        body = prefix + chunk
        if in_code:
            body += "\n```"
            carry_lang = lang
        else:
            carry_lang = None
        out.append(body)
    return out


def split_text_fence_aware(
    text,
    limit,
    len_fn=None,
    *,
    prefer_paragraphs=True,
    balance_fences=False,
):
    """Split markdown text into chunks of at most *limit*, respecting fences.

    Two strategies, selected by ``prefer_paragraphs``:

    ``prefer_paragraphs=True`` (the richest):
      Extract atomic blocks (code fences, tables, paragraphs), greedily merge
      them up to *limit*, split still-oversized non-atomic chunks at
      paragraph boundaries, then re-merge small neighbours.  Code blocks and
      tables are never split in the middle; a single atom larger than
      *limit* is emitted oversize rather than broken.

    ``prefer_paragraphs=False`` (stream_consumer-derived):
      Newline-preferred hard splitting with headroom reserved for fence
      markers when the text contains ```.

    ``balance_fences=True`` post-processes the chunks so a split inside a
    code block closes the fence on the head chunk and reopens it (with the
    language tag) on the tail — required by callers whose chunks are
    delivered as independent messages that each must render standalone.
    """
    _len = len_fn or len

    if not text:
        return []

    if prefer_paragraphs:
        chunks = _chunk_markdown_paragraphs(text, limit, len_fn)
    else:
        chunks = _chunk_newline_preferred(text, limit, _len)

    if balance_fences:
        chunks = balance_fences_across_chunks(chunks)
    return chunks


def _chunk_markdown_paragraphs(text, max_chars, len_fn=None):
    """Paragraph/atom chunking pipeline (see module docs)."""
    _len = len_fn or len

    if _len(text) <= max_chars:
        return [text]

    # Phase 1: Extract atomic blocks
    atoms = split_markdown_atoms(text)

    # Phase 2: Greedy merge
    chunks: "list[str]" = []
    indivisible_set: "set[int]" = set()
    current_parts: "list[str]" = []
    current_len = 0

    def _flush_parts() -> None:
        if current_parts:
            chunks.append('\n\n'.join(current_parts))

    for atom in atoms:
        atom_len = _len(atom)
        sep_len = 2 if current_parts else 0
        projected_len = current_len + sep_len + atom_len

        if projected_len > max_chars and current_parts:
            _flush_parts()
            current_parts = []
            current_len = 0
            sep_len = 0

        if (not current_parts
                and atom_len > max_chars
                and (is_fence_atom(atom) or is_table_atom(atom))):
            indivisible_set.add(len(chunks))
            chunks.append(atom)
            continue

        current_parts.append(atom)
        current_len += sep_len + atom_len

    _flush_parts()

    # Phase 3: Split still-oversized chunks at paragraph boundaries
    result: "list[str]" = []
    for idx, chunk in enumerate(chunks):
        if _len(chunk) <= max_chars:
            result.append(chunk)
            continue

        if idx in indivisible_set:
            result.append(chunk)
            continue

        if text_has_unclosed_fence(chunk):
            result.append(chunk)
            continue

        remaining = chunk
        while _len(remaining) > max_chars:
            head, remaining = split_at_paragraph_boundary(
                remaining, max_chars, len_fn=len_fn,
            )
            if not head:
                head, remaining = remaining[:max_chars], remaining[max_chars:]
            if head:
                result.append(head)
        if remaining:
            result.append(remaining)

    # Phase 4: Merge small trailing/leading chunks with neighbours
    if len(result) > 1:
        merged: "list[str]" = [result[0]]
        for chunk in result[1:]:
            prev = merged[-1]
            combined = prev + '\n\n' + chunk
            if _len(combined) <= max_chars:
                merged[-1] = combined
            else:
                merged.append(chunk)
        result = merged

    return [c for c in result if c]


def _chunk_newline_preferred(text, limit, len_fn):
    """Stream-consumer-derived newline-preferred splitting (no balancing)."""
    if len_fn(text) <= limit:
        return [text]
    # Reserve headroom for the close/reopen fence markers a balancing pass
    # may add, so balanced chunks stay within the platform limit.
    split_limit = limit
    if "```" in text:
        split_limit = max(limit - 16, limit // 2, 1)
    # Local import: gateway.platforms.base is heavyweight and pulls config;
    # helpers must stay import-light for adapters that import it first.
    from gateway.platforms.base import _custom_unit_to_cp

    chunks: "list[str]" = []
    remaining = text
    while len_fn(remaining) > split_limit:
        _cp_budget = _custom_unit_to_cp(remaining, split_limit, len_fn)
        split_at = remaining.rfind("\n", 0, _cp_budget)
        if split_at < _cp_budget // 2:
            split_at = _cp_budget
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks
