"""Attachments when signal-cli runs on another host.

``send`` hands signal-cli an ``attachments`` list, and signal-cli opens those
paths on ITS OWN filesystem. With ``SIGNAL_HTTP_URL`` pointing off-box, every
path the gateway produces is unreadable there:

    Failed to send message: /srv/household/notes.md:
    /srv/household/notes.md (No such file or directory)
    (AttachmentInvalidException)

— which reads like "the file is missing" even though the gateway stat()ed it
successfully a line earlier. signal-cli accepts an RFC 2397 data URI in place
of a path (its own ``--attachment`` help documents
``data:<MIME>;filename=<NAME>;base64,<DATA>``), so the bytes can go inline.

The retry is gated on that specific error. A transient failure must never be
retried: the first send may have gone through, and the recipient would get the
file twice.
"""

from __future__ import annotations

import asyncio
import base64
from urllib.parse import unquote

import pytest

from gateway.platforms.signal import (
    SIGNAL_MAX_INLINE_ATTACHMENT_SIZE,
    SignalAdapter,
)

UNREADABLE = {
    "code": -32603,
    "message": (
        "Failed to send message: /srv/household/notes.md: "
        "/srv/household/notes.md (No such file or directory) "
        "(AttachmentInvalidException) (UnexpectedErrorException)"
    ),
}


# ── the error detector ────────────────────────────────────────────────────

def test_detects_the_daemon_cannot_read_this_path() -> None:
    assert SignalAdapter._is_unreadable_attachment_error(UNREADABLE)
    assert SignalAdapter._is_unreadable_attachment_error(
        {"message": "boom (AttachmentInvalidException)"}
    )


@pytest.mark.parametrize(
    "err",
    [
        pytest.param(None, id="no-error"),
        pytest.param({}, id="empty"),
        pytest.param({"message": "Rate limit exceeded"}, id="rate-limit"),
        pytest.param({"message": "Connection reset by peer"}, id="transient"),
        pytest.param({"message": "Untrusted identity key"}, id="identity"),
    ],
)
def test_other_failures_are_not_treated_as_unreadable(err) -> None:
    """These must not trigger the retry — the first send may have landed."""
    assert not SignalAdapter._is_unreadable_attachment_error(err)


# ── the data URI ──────────────────────────────────────────────────────────

def test_inline_uri_shape_and_payload(tmp_path) -> None:
    f = tmp_path / "routine.md"
    f.write_bytes(b"# hi\n")
    uri = SignalAdapter._inline_attachment_uri(SignalAdapter, str(f))

    assert uri.startswith("data:text/markdown;filename=routine.md;base64,")
    payload = uri.split(";base64,", 1)[1]
    assert base64.b64decode(payload) == b"# hi\n"


def test_unknown_extension_gets_a_generic_mime(tmp_path) -> None:
    f = tmp_path / "blob.zzz"
    f.write_bytes(b"x")
    uri = SignalAdapter._inline_attachment_uri(SignalAdapter, str(f))
    assert uri.startswith("data:application/octet-stream;")


def test_pdf_mime_is_detected(tmp_path) -> None:
    """The point of the exercise: a rendered PDF must arrive as a PDF."""
    f = tmp_path / "routine.pdf"
    f.write_bytes(b"%PDF-1.4\n")
    uri = SignalAdapter._inline_attachment_uri(SignalAdapter, str(f))
    assert uri.startswith("data:application/pdf;filename=routine.pdf;base64,")


def test_filename_separators_are_escaped(tmp_path) -> None:
    """``;`` and ``,`` delimit fields in the URI.

    An unescaped one would truncate the payload and signal-cli would receive a
    corrupt attachment rather than an error.
    """
    f = tmp_path / "a;b,c.md"
    f.write_bytes(b"z")
    uri = SignalAdapter._inline_attachment_uri(SignalAdapter, str(f))

    head, payload = uri.split(";base64,", 1)
    assert base64.b64decode(payload) == b"z"
    name = head.split(";filename=", 1)[1]
    assert ";" not in name and "," not in name
    assert unquote(name) == "a;b,c.md"


def test_unreadable_file_returns_none(tmp_path) -> None:
    missing = tmp_path / "gone.md"
    assert SignalAdapter._inline_attachment_uri(SignalAdapter, str(missing)) is None


# ── the send path ─────────────────────────────────────────────────────────

def _adapter(rpc_results: list):
    """A SignalAdapter with only what ``_send_attachment`` touches.

    object.__new__ per the AGENTS.md pitfall: adapters are routinely built
    without BasePlatformAdapter.__init__ in tests.
    """
    a = object.__new__(SignalAdapter)
    a.account = "+15550000000"
    a.calls = []

    async def _rpc(method, params, *args, error_out=None, **kwargs):
        # Snapshot: the send path reuses one params dict across the retry, so
        # holding a reference would show every call the final attachment.
        a.calls.append({**params, "attachments": list(params["attachments"])})
        outcome = rpc_results[len(a.calls) - 1]
        if isinstance(outcome, dict) and "error" in outcome:
            if error_out is not None:
                error_out["error"] = outcome["error"]
            return None
        return outcome

    async def _noop(*args, **kwargs):
        return None

    a._rpc = _rpc
    a._stop_typing_indicator = _noop
    a._track_sent_timestamp = lambda result: None
    return a


def _send(adapter, path, **kw):
    return asyncio.run(
        adapter._send_attachment("group:g1", str(path), "File", None, **kw)
    )


def test_path_send_succeeds_without_inlining(tmp_path) -> None:
    """A local daemon reads the path; nothing is base64-encoded."""
    f = tmp_path / "ok.pdf"
    f.write_bytes(b"%PDF-1.4\n")
    a = _adapter([{"timestamp": 1}])

    assert _send(a, f).success
    assert len(a.calls) == 1
    assert a.calls[0]["attachments"] == [str(f)]


def test_unreadable_path_retries_inline(tmp_path) -> None:
    f = tmp_path / "routine.pdf"
    f.write_bytes(b"%PDF-1.4\n")
    a = _adapter([{"error": UNREADABLE}, {"timestamp": 2}])

    assert _send(a, f).success
    assert len(a.calls) == 2
    assert a.calls[0]["attachments"] == [str(f)]
    assert a.calls[1]["attachments"][0].startswith("data:application/pdf;")
    # The retry must target the same conversation, not fall back to a default.
    assert a.calls[1]["groupId"] == a.calls[0]["groupId"] == "g1"


def test_transient_failure_is_not_retried(tmp_path) -> None:
    """The double-send guard.

    A timeout or reset may mean the message DID go out and only the response
    was lost. Retrying would deliver the attachment twice.
    """
    f = tmp_path / "notes.pdf"
    f.write_bytes(b"%PDF-1.4\n")
    a = _adapter([{"error": {"message": "Connection reset by peer"}}])

    assert not _send(a, f).success
    assert len(a.calls) == 1


def test_oversize_file_reports_the_real_cause(tmp_path) -> None:
    """Refuse rather than build a 100 MB JSON body — and say why.

    The operator needs to know the daemon is remote; "too large" alone would
    send them looking at the wrong thing.
    """
    f = tmp_path / "big.pdf"
    f.write_bytes(b"\0" * (SIGNAL_MAX_INLINE_ATTACHMENT_SIZE + 1))
    a = _adapter([{"error": UNREADABLE}])

    result = _send(a, f)
    assert not result.success
    assert len(a.calls) == 1
    assert "signal-cli cannot read" in result.error
    assert "SIGNAL_HTTP_URL" in result.error


def test_missing_file_never_reaches_the_rpc(tmp_path) -> None:
    a = _adapter([])
    result = _send(a, tmp_path / "nope.pdf")
    assert not result.success
    assert not a.calls
