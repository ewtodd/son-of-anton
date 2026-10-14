"""Stdlib document-to-text extraction for ``read_file``.

Supports Jupyter notebooks, DOCX, and XLSX without adding hard dependencies,
and PDF through poppler's ``pdftotext``, which the Nix package puts on PATH.
Nothing is installed at runtime. When the build includes the optional
``firecrawl-anydoc`` package (imports as ``anydoc``), coverage widens to
legacy Office (.doc/.ppt/.xls), OpenDocument, RTF, and EPUB, and PDF goes
through it too — converted to Markdown by its Rust core. The stdlib
extractors remain authoritative for their three formats so behavior is
identical whether or not anydoc is present.
Malformed documents raise :class:`ExtractionError`; callers can then fall back to
normal text/binary handling.
"""

from __future__ import annotations

import importlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET

__all__ = [
    "EXTRACTABLE_EXTENSIONS",
    "ExtractionError",
    "extract_document_bytes",
    "extract_document_text",
    "is_extractable_document",
]

EXTRACTABLE_EXTENSIONS = frozenset({".ipynb", ".docx", ".xlsx"})
# Formats handled only when the build includes the optional anydoc converter
# (PDF also converts without it, through pdftotext).
ANYDOC_EXTENSIONS = frozenset({
    ".doc", ".docm",
    ".ppt", ".pps", ".pot", ".pptx", ".pptm", ".ppsx", ".ppsm",
    ".xls", ".xlsm", ".xlsb",
    ".odt", ".ods", ".odp",
    ".rtf", ".epub", ".pdf",
})
# Refuse to convert huge documents. anydoc loads the whole file through its
# Rust core with no streaming, and the read_file char budget only applies
# after conversion, so an unbounded input can pin a tool turn and spike RAM.
MAX_ANYDOC_BYTES = 50 * 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
_MAX_XLSX_ROWS_PER_SHEET = 5000
_MAX_XLSX_COLS = 256

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


class ExtractionError(Exception):
    """Raised when a supported-looking document cannot be rendered as text."""


def _extension(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in EXTRACTABLE_EXTENSIONS:
        return ext
    if ext in ANYDOC_EXTENSIONS and (_anydoc() is not None or _pdftotext_fallback(ext)):
        return ext
    return ""


_ANYDOC_UNSET = object()
_anydoc_module: Any = _ANYDOC_UNSET


def _anydoc() -> Optional[Any]:
    """The optional anydoc converter if the build includes it, else None.

    Never installed at runtime: every dependency comes from the Nix build
    (add anydoc through the package's ``extraPythonPackages`` override).
    """
    global _anydoc_module
    if _anydoc_module is _ANYDOC_UNSET:
        try:
            _anydoc_module = importlib.import_module("anydoc")
        except Exception:  # ImportError or a broken native binding
            _anydoc_module = None
    return _anydoc_module


def is_extractable_document(path: str) -> bool:
    return bool(_extension(path))


def extract_document_text(path: str) -> str:
    ext = _extension(path)
    if ext == ".ipynb":
        return _extract_notebook(path)
    if ext == ".docx":
        return _extract_docx(path)
    if ext == ".xlsx":
        return _extract_xlsx(path)
    if ext in ANYDOC_EXTENSIONS:
        return _extract_anydoc(path)
    raise ExtractionError(f"Unsupported document type: {path!r}")


def extract_document_bytes(data: bytes, path: str) -> str:
    """Extract a document already fetched across a file backend boundary."""
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ExtractionError(
            f"Document too large to convert ({len(data):,} bytes, limit is {MAX_DOCUMENT_BYTES:,})"
        )
    ext = _extension(path)
    if ext in ANYDOC_EXTENSIONS:
        return _extract_anydoc_bytes(data, path)
    if ext not in EXTRACTABLE_EXTENSIONS:
        raise ExtractionError(f"Unsupported document type: {path!r}")

    # The stdlib extractors are path-oriented. Materialize backend bytes in a
    # private host temp file, then remove it even when parsing fails.
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as fh:
            fh.write(data)
            temp_path = fh.name
        return extract_document_text(temp_path)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _extract_anydoc(path: str) -> str:
    mod = _anydoc()
    if mod is None:
        if _pdftotext_fallback(Path(path).suffix.lower()):
            return _extract_pdftotext(path)
        raise ExtractionError(f"Unsupported document type: {path!r}")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    if size > MAX_ANYDOC_BYTES:
        raise ExtractionError(
            f"Document too large to convert ({size:,} bytes, limit is {MAX_ANYDOC_BYTES:,})"
        )
    try:
        text = mod.to_markdown(path)
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    except Exception as exc:
        # anydoc raises one ConvertError subclass per failure mode
        # (Unsupported, Malformed, Encrypted, ResourceLimit, MissingPart).
        # Any of them means "no meaningful text": fall back to the normal
        # path/binary handling rather than crash read_file.
        raise ExtractionError(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(text, str) or not text.strip():
        raise ExtractionError("Document contains no extractable text")
    text = text.rstrip("\n") + "\n"
    if Path(path).suffix.lower() == ".pdf":
        note = _pdf_coverage_note(path)
        if note:
            # Prepend: read_file paginates the extraction, so a footer on a
            # long document would sit on a page the model may never fetch.
            text = note + text
    return text


# ── Scanned-PDF coverage detection ──────────────────────────────────
#
# anydoc (like every text-layer extractor) returns nothing for scanned
# image pages and emits no image placeholders or page markers, so a
# mostly-scanned PDF converts "successfully" into a few headers with
# empty bodies — silent data loss the model cannot detect. Count per-page
# text via poppler's pdftotext (form-feed page separators) and append a
# loud footer when a meaningful share of pages yielded no text.

# A page with fewer extracted characters than this is considered empty.
PDF_EMPTY_PAGE_CHARS = 20
# Warn when at least this many pages are empty AND they exceed the ratio,
# or when the absolute count alone is overwhelming.
PDF_COVERAGE_MIN_EMPTY = 2
PDF_COVERAGE_MIN_RATIO = 0.2
PDF_COVERAGE_ABSOLUTE_EMPTY = 10
PDF_PAGE_SCAN_TIMEOUT = 20.0


def _run_pdftotext(path: str) -> list[str]:
    """Per-page text from poppler's pdftotext; raises ExtractionError."""
    try:
        proc = subprocess.run(
            ["pdftotext", path, "-"],
            capture_output=True,
            timeout=PDF_PAGE_SCAN_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExtractionError(f"pdftotext failed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ExtractionError(f"pdftotext failed: {detail or f'exit {proc.returncode}'}")
    pages = proc.stdout.decode("utf-8", errors="replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()  # trailing form-feed artifact
    return pages


def _pdf_page_texts(path: str) -> Optional[list[str]]:
    """Per-page extracted text, or None when undeterminable."""
    if shutil.which("pdftotext") is None:
        return None
    try:
        return _run_pdftotext(path) or None
    except ExtractionError:
        return None


def _group_ranges(pages: list[int]) -> list[list[int]]:
    """Group sorted 1-based page numbers into [start, end] runs."""
    ranges: list[list[int]] = []
    for p in pages:
        if ranges and p == ranges[-1][1] + 1:
            ranges[-1][1] = p
        else:
            ranges.append([p, p])
    return ranges


# Cap the per-gap breakdown so a pathological PDF (hundreds of alternating
# text/scan pages) cannot balloon the warning. Ranges beyond the cap are
# summarized in one line.
PDF_GAP_MAP_MAX_ENTRIES = 20
_GAP_CONTEXT_CHARS = 60


def _gap_map(counts: list[int], texts: list[str], empty: list[int]) -> str:
    """Per-gap breakdown: each empty range labeled with the last text seen
    before it (usually a section divider/header page), so the agent can
    decide WHICH gaps it actually needs to read instead of OCRing all of
    them."""
    ranges = _group_ranges(empty)
    lines: list[str] = []
    for a, b in ranges[:PDF_GAP_MAP_MAX_ENTRIES]:
        label = ""
        # Walk back to the nearest preceding page with text.
        for prev in range(a - 2, -1, -1):
            if counts[prev] >= PDF_EMPTY_PAGE_CHARS:
                snippet = " ".join(texts[prev].split())[:_GAP_CONTEXT_CHARS]
                label = f' — after "{snippet}" (p{prev + 1})'
                break
        span = f"page {a}" if a == b else f"pages {a}-{b}"
        n = b - a + 1
        lines.append(f"  {span} ({n} page{'s' if n != 1 else ''}){label}")
    if len(ranges) > PDF_GAP_MAP_MAX_ENTRIES:
        rest = ranges[PDF_GAP_MAP_MAX_ENTRIES:]
        rest_pages = sum(b - a + 1 for a, b in rest)
        lines.append(f"  … {len(rest)} more gaps ({rest_pages} pages)")
    return "\n".join(lines)


def _pdf_coverage_note(path: str, display_path: Optional[str] = None) -> str:
    """A warning header when many PDF pages produced no text, else ''.

    ``path`` is the file scanned with pdftotext (may be a host temp file
    for backend-transferred bytes); ``display_path`` is the path shown in
    the recovery command — the one the agent's terminal can actually see.
    """
    texts = _pdf_page_texts(path)
    return _coverage_note_for(texts, display_path or path) if texts else ""


def _coverage_note_for(texts: list[str], shown: str) -> str:
    """The coverage warning for already-extracted page texts, else ''."""
    if len(texts) < 2:
        return ""
    counts = [len(page.strip()) for page in texts]
    empty = [i + 1 for i, n in enumerate(counts) if n < PDF_EMPTY_PAGE_CHARS]
    total = len(counts)
    if len(empty) < PDF_COVERAGE_MIN_EMPTY:
        return ""
    if (
        len(empty) / total < PDF_COVERAGE_MIN_RATIO
        and len(empty) < PDF_COVERAGE_ABSOLUTE_EMPTY
    ):
        return ""
    return (
        "[EXTRACTION COVERAGE WARNING: "
        f"{len(empty)} of {total} pages in this PDF yielded no text. "
        "Those pages are likely scanned images (or blank) — their content "
        "is MISSING from the extracted text below, even where section "
        "headers appear with empty bodies. Unreadable gaps, each labeled "
        "with the last text extracted before it:\n"
        f"{_gap_map(counts, texts, empty)}\n"
        "Decide which gaps you actually need — do NOT OCR or render "
        "everything. For the gaps that matter, render just that range with "
        f"`pdftoppm -jpeg -r 150 -f <first> -l <last> '{shown}' /tmp/page` "
        "and inspect each image with the vision_analyze tool.]\n"
    )


def _extract_anydoc_bytes(data: bytes, path: str) -> str:
    mod = _anydoc()
    if mod is None:
        if _pdftotext_fallback(Path(path).suffix.lower()):
            return _extract_pdftotext_bytes(data, path)
        raise ExtractionError(f"Unsupported document type: {path!r}")
    if len(data) > MAX_ANYDOC_BYTES:
        raise ExtractionError(
            f"Document too large to convert ({len(data):,} bytes, limit is {MAX_ANYDOC_BYTES:,})"
        )
    try:
        text = mod.to_markdown_bytes(data)
    except Exception as exc:
        raise ExtractionError(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(text, str) or not text.strip():
        raise ExtractionError("Document contains no extractable text")
    text = text.rstrip("\n") + "\n"
    if Path(path).suffix.lower() == ".pdf":
        note = _pdf_coverage_note_from_bytes(data, path)
        if note:
            # Prepend: read_file paginates the extraction, so a footer on a
            # long document would sit on a page the model may never fetch.
            text = note + text
    return text


def _pdf_coverage_note_from_bytes(data: bytes, display_path: str) -> str:
    """Coverage note for backend-transferred PDF bytes.

    pdftotext is path-oriented, so materialize the bytes in a private host
    temp file for the scan; the recovery command still names
    ``display_path`` — the path the agent's terminal backend can see.
    """
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(data)
            temp_path = fh.name
        return _pdf_coverage_note(temp_path, display_path=display_path)
    except OSError:
        return ""
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


# ── PDF without anydoc ──────────────────────────────────────────────
#
# anydoc is not in the sealed Nix venv, so every PDF read fell through to the
# binary-file error and the agent spent a turn rediscovering pdftotext.
# poppler ships with the package, so PDFs convert through it instead.


def _pdftotext_fallback(ext: str) -> bool:
    return ext == ".pdf" and shutil.which("pdftotext") is not None


def _extract_pdftotext(path: str, display_path: Optional[str] = None) -> str:
    """PDF text in reading order, one marked section per page.

    Reading order (not ``-layout``) keeps a two-column paper one column at a
    time; the page markers are what the coverage warning's page numbers mean.
    """
    shown = display_path or path
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    if size > MAX_DOCUMENT_BYTES:
        raise ExtractionError(
            f"Document too large to convert ({size:,} bytes, limit is {MAX_DOCUMENT_BYTES:,})"
        )
    texts = _run_pdftotext(path)
    if not any(page.strip() for page in texts):
        raise ExtractionError(
            "the PDF has no text layer (likely scanned). Render the pages you "
            f"need with `pdftoppm -jpeg -r 150 -f <first> -l <last> '{shown}' "
            "/tmp/page` and inspect them with the vision_analyze tool"
        )
    body = "\n".join(f"--- page {i} ---\n{page.strip()}" for i, page in enumerate(texts, 1))
    # Prepended for the same reason as the anydoc path: read_file paginates.
    return _coverage_note_for(texts, shown) + body + "\n"


def _extract_pdftotext_bytes(data: bytes, display_path: str) -> str:
    """pdftotext for backend-transferred bytes, via a private host temp file."""
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(data)
            temp_path = fh.name
        return _extract_pdftotext(temp_path, display_path=display_path)
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _source_text(source) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, list):
        return "".join(item for item in source if isinstance(item, str))
    return ""


def _human_size(n_bytes: int) -> str:
    return f"{round(n_bytes / 1024)} KB" if n_bytes >= 1024 else f"{n_bytes} B"


def _base64_bytes(payload: str) -> int:
    """Approximate decoded size of a base64 payload (whitespace ignored)."""
    clean = re.sub(r"[^0-9+/=A-Za-z]", "", payload)
    padding = min(2, len(clean) - len(clean.rstrip("=")))
    return max(0, (len(clean) * 3) // 4 - padding)


def _clean_stream_text(text: str) -> str:
    """Strip ANSI escapes and collapse ``\\r`` progress-bar rewrites.

    tqdm and friends redraw the same line via carriage returns; Jupyter
    renders only the final frame, so keeping the text after the last ``\\r``
    of each line reproduces what the notebook displays without the invisible
    intermediate frames.
    """
    from tools.ansi_strip import strip_ansi

    cleaned = strip_ansi(text).replace("\r\n", "\n")
    lines = []
    for line in cleaned.split("\n"):
        frames = [frame for frame in line.split("\r") if frame]
        lines.append(frames[-1] if frames else "")
    return "\n".join(lines)


# Notebook outputs longer than this are tail-truncated per output block so a
# single runaway training log cannot flood the extracted text.
_MAX_OUTPUT_CHARS = 20_000


def _notebook_output_text(output: Any) -> str:
    """Render one notebook output as compact text.

    Keeps stream text, error tracebacks, and textual results; replaces
    token-heavy payloads (base64 images, HTML, widget state) with short
    sized placeholders. Handles both nbformat v4 output shapes and the
    legacy v3 ones (``pyout``/``pyerr``; data flat on the output dict).
    """
    if not isinstance(output, dict):
        return ""
    otype = output.get("output_type")

    if otype == "stream":
        body = _clean_stream_text(_source_text(output.get("text", "")))
        return body if body.strip() else ""

    if otype in {"error", "pyerr"}:
        traceback = output.get("traceback")
        tb_text = ""
        if isinstance(traceback, list):
            tb_text = _clean_stream_text(
                "\n".join(line for line in traceback if isinstance(line, str))
            )
        header = f"Error: {output.get('ename', '')}: {output.get('evalue', '')}".rstrip(": ")
        return f"{header}\n{tb_text}".rstrip()

    if otype in {"execute_result", "display_data", "pyout"}:
        data = output.get("data")
        if not isinstance(data, dict):
            # nbformat v3 stores mime data flat on the output dict.
            data = {}
            if isinstance(output.get("text"), (str, list)):
                data["text/plain"] = output["text"]
            for v3_key, mime in (("png", "image/png"), ("jpeg", "image/jpeg"),
                                 ("svg", "image/svg+xml"), ("html", "text/html")):
                if v3_key in output:
                    data[mime] = output[v3_key]

        if "application/vnd.jupyter.widget-view+json" in data:
            return "[interactive widget — omitted]"

        # Prefer readable text: models consume text/plain (e.g. the pandas
        # twin of an HTML table) far better than markup.
        for mime in ("text/plain", "text/markdown"):
            if mime in data:
                body = _clean_stream_text(_source_text(data[mime]))
                if body.strip():
                    return body

        for mime, value in data.items():
            if isinstance(mime, str) and mime.startswith("image/"):
                size = _base64_bytes(_source_text(value))
                return f"[{mime} output — {_human_size(size)}, omitted]"

        if "text/html" in data:
            html = _source_text(data["text/html"])
            return f"[text/html output — {len(html):,} chars, omitted]"

        mimes = ", ".join(str(m) for m in data) or "unknown"
        return f"[{mimes} output — omitted]"

    return ""


def _notebook_outputs(cell: dict, jq_pointer: str = "", filename: str = "") -> str:
    outputs = cell.get("outputs")
    if not isinstance(outputs, list):
        return ""
    blocks = [text for text in (_notebook_output_text(o) for o in outputs) if text]
    if not blocks:
        return ""
    joined = "\n".join(blocks)
    if len(joined) > _MAX_OUTPUT_CHARS:
        omitted = len(joined) - _MAX_OUTPUT_CHARS
        hint = ""
        if jq_pointer and filename:
            hint = f" — full output: jq -r '{jq_pointer}' {filename}"
        joined = joined[:_MAX_OUTPUT_CHARS] + f"\n… [{omitted:,} output chars truncated{hint}]"
    return joined


def _extract_notebook(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            nb = json.load(fh)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"Not a valid notebook: {exc}") from exc
    if not isinstance(nb, dict):
        raise ExtractionError("Notebook root is not an object")

    raw_cells = nb.get("cells")
    if isinstance(raw_cells, list):
        cells = [(f".cells[{i}].outputs", cell) for i, cell in enumerate(raw_cells)]
    else:
        cells = [
            (f".worksheets[{wi}].cells[{ci}].outputs", cell)
            for wi, ws in enumerate(nb.get("worksheets", []))
            if isinstance(ws, dict)
            for ci, cell in enumerate(ws.get("cells", []))
        ]
    if not cells:
        raise ExtractionError("Notebook contains no cells")

    nb_name = os.path.basename(path)
    counts = {"markdown": 0, "code": 0, "raw": 0}
    labels = {"markdown": "Markdown", "code": "Code", "raw": "Raw"}
    out: list[str] = []
    for jq_pointer, cell in cells:
        if not isinstance(cell, dict):
            continue
        typ = cell.get("cell_type")
        if typ not in labels:
            continue
        counts[typ] += 1
        suffix = f" {counts[typ]}" if typ != "raw" else ""
        out.extend((f"# ── {labels[typ]} cell{suffix} ──", _source_text(cell.get("source", "")).rstrip("\n"), ""))
        if typ == "code":
            rendered = _notebook_outputs(cell, jq_pointer, nb_name)
            if rendered:
                out.extend((f"# ── Output (cell {counts[typ]}) ──", rendered.rstrip("\n"), ""))
    if not out:
        raise ExtractionError("Notebook contains no readable cells")
    return "\n".join(out).rstrip("\n") + "\n"


def _zip_xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(zf.read(name))
    except KeyError as exc:
        raise ExtractionError(f"Missing {name}") from exc
    except ET.ParseError as exc:
        raise ExtractionError(f"Malformed XML in {name}: {exc}") from exc


def _extract_docx(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            root = _zip_xml(zf, "word/document.xml")
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f"Not a valid DOCX: {exc}") from exc
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc

    w = f"{{{_NS_W}}}"
    lines: list[str] = []
    for para in root.iter(f"{w}p"):
        buf: list[str] = []
        for node in para.iter():
            if node.tag == f"{w}t":
                buf.append(node.text or "")
            elif node.tag == f"{w}tab":
                buf.append("\t")
            elif node.tag in {f"{w}br", f"{w}cr"}:
                buf.append("\n")
        lines.extend("".join(buf).split("\n"))
    if not any(line.strip() for line in lines):
        raise ExtractionError("DOCX contains no extractable text")
    return "\n".join(lines).rstrip("\n") + "\n"


def _extract_xlsx(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            shared = _shared_strings(zf, names)
            sheets = _workbook_sheets(zf)
            rels = _workbook_rels(zf, names)
            out: list[str] = []
            for name, state, rid in sheets:
                if state in {"hidden", "veryHidden"}:
                    continue
                part = _sheet_part(rels.get(rid, ""))
                if part not in names:
                    continue
                try:
                    rows = _sheet_rows(zf.read(part), shared)
                except ET.ParseError:
                    continue
                out.append(f"# ── Sheet: {name} ──")
                out.extend("\t".join(row) for row in rows)
                if not rows:
                    out.append("(empty)")
                out.append("")
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f"Not a valid XLSX: {exc}") from exc
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc

    if not out:
        raise ExtractionError("XLSX has no visible sheets with content")
    return "\n".join(out).rstrip("\n") + "\n"


def _shared_strings(zf: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except ET.ParseError:
        return []
    s = f"{{{_NS_S}}}"
    return ["".join(t.text or "" for t in item.iter(f"{s}t")) for item in root.iter(f"{s}si")]


def _workbook_sheets(zf: zipfile.ZipFile) -> list[tuple[str, str, str]]:
    root = _zip_xml(zf, "xl/workbook.xml")
    s, r = f"{{{_NS_S}}}", f"{{{_NS_REL}}}"
    return [
        (sheet.get("name", "Sheet"), sheet.get("state", "visible"), sheet.get(f"{r}id", ""))
        for sheet in root.iter(f"{s}sheet")
    ]


def _workbook_rels(zf: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
    rels_path = "xl/_rels/workbook.xml.rels"
    if rels_path not in names:
        return {}
    try:
        root = ET.fromstring(zf.read(rels_path))
    except ET.ParseError:
        return {}
    rel_tag = f"{{{_NS_PKG_REL}}}Relationship"
    return {rel.get("Id", ""): rel.get("Target", "") for rel in root.iter(rel_tag) if rel.get("Id")}


def _sheet_part(target: str) -> str:
    target = target.lstrip("/")
    return posixpath.normpath(target if target.startswith("xl/") else f"xl/{target}")


def _col_index(ref: str) -> int:
    idx = 0
    for ch in ref:
        if not ch.isalpha():
            break
        idx = idx * 26 + ord(ch.upper()) - ord("A") + 1
    return max(idx - 1, 0)


def _sheet_rows(xml_bytes: bytes, shared: list[str]) -> list[list[str]]:
    root = ET.fromstring(xml_bytes)
    s = f"{{{_NS_S}}}"
    rows: list[list[str]] = []
    for row in root.iter(f"{s}row"):
        if len(rows) >= _MAX_XLSX_ROWS_PER_SHEET:
            break
        cells: dict[int, str] = {}
        max_col = -1
        for cell in row.iter(f"{s}c"):
            col = _col_index(cell.get("r", "")) if cell.get("r") else max_col + 1
            if col >= _MAX_XLSX_COLS:
                continue
            cells[col] = _cell_value(cell, shared, s)
            max_col = max(max_col, col)
        rows.append([cells.get(i, "") for i in range(max_col + 1)] if max_col >= 0 else [])
    while rows and not any(value.strip() for value in rows[-1]):
        rows.pop()
    return rows


def _cell_value(cell: ET.Element, shared: list[str], s: str) -> str:
    value = cell.findtext(f"{s}v") or ""
    typ = cell.get("t", "")
    if typ == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    if typ == "inlineStr":
        inline = cell.find(f"{s}is")
        return "" if inline is None else "".join(t.text or "" for t in inline.iter(f"{s}t"))
    if typ == "b":
        return "TRUE" if value.strip() in {"1", "true", "TRUE"} else "FALSE"
    if typ == "e":
        return value or "#ERROR"
    return value
