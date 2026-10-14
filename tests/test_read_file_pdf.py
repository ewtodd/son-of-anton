"""read_file turns PDFs into text from what the Nix build ships, with no pip.

PDF conversion hung on anydoc, a pip package lazy-installed on first use,
which the sealed Nix venv never receives: every PDF read fell through to the
binary-file error and the agent spent a turn rediscovering pdftotext through
its terminal. The package now ships poppler and read_file converts through it.

These drive the real ``read_file_tool`` (temp ``SON_OF_ANTON_HOME`` from
conftest) on PDFs built here, without anydoc, and check nothing reaches for
an installer.
"""

from __future__ import annotations

import json
import shutil
import sys

import pytest

needs_pdftotext = pytest.mark.skipif(
    shutil.which("pdftotext") is None, reason="poppler's pdftotext is not on PATH"
)


def _pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF with one Helvetica text line per page ('' = blank)."""
    n = len(pages)
    page_ids = [4 + 2 * i for i in range(n)]
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for page_id, text in zip(page_ids, pages):
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


@pytest.fixture
def read_pdf(tmp_path, monkeypatch):
    import tools.read_extract as read_extract
    from tools.file_tools import read_file_tool
    from tools.terminal_tool import cleanup_vm

    # A build without anydoc, loaded fresh through the real _anydoc().
    monkeypatch.setitem(sys.modules, "anydoc", None)
    monkeypatch.setattr(read_extract, "_anydoc_module", read_extract._ANYDOC_UNSET)
    task_id = "read-file-pdf"

    def read(pages: list[str]) -> dict:
        path = tmp_path / "doc.pdf"
        path.write_bytes(_pdf(pages))
        return json.loads(read_file_tool(str(path), task_id=task_id))

    try:
        yield read
    finally:
        cleanup_vm(task_id)


@needs_pdftotext
def test_pdf_reads_as_text_page_by_page(read_pdf) -> None:
    pages = ["Alpha decay of 37Cl", "Rutherford scattering cross section"]
    result = read_pdf(pages)
    assert result.get("extracted_document") is True, result
    content = result["content"]
    assert "%PDF" not in content and "endstream" not in content, "raw PDF syntax leaked"
    positions = []
    for number, text in enumerate(pages, 1):
        marker = content.index(f"--- page {number} ---")
        assert content.index(text) > marker, f"page {number} text is not under its marker"
        positions.append(marker)
    assert positions == sorted(positions), "pages out of order"


@needs_pdftotext
def test_pages_without_text_are_flagged_not_silently_dropped(read_pdf) -> None:
    result = read_pdf(["Introduction", "", "", "Conclusions"])
    content = result["content"]
    assert "EXTRACTION COVERAGE WARNING" in content
    assert content.index("EXTRACTION COVERAGE WARNING") < content.index("Introduction"), (
        "the warning must lead: read_file paginates, so a footer can go unread"
    )
    assert "pdftoppm" in content


@needs_pdftotext
def test_pdf_without_a_text_layer_says_how_to_read_it(read_pdf) -> None:
    result = read_pdf(["", ""])
    assert not result.get("content"), result
    assert "no text layer" in result["error"]
    assert "pdftoppm" in result["error"]
