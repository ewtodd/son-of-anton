"""Attachments when signal-cli cannot open the gateway's paths.

``send`` hands signal-cli an ``attachments`` list, and signal-cli opens those
paths itself. With ``SIGNAL_HTTP_URL`` pointing off-box, every path the gateway
produces is missing there:

    Failed to send message: /srv/household/notes.md:
    /srv/household/notes.md (No such file or directory)
    (AttachmentInvalidException)

and with the daemon on this host as its own user (the NixOS ``signal-cli``
service), a file under a home directory is out of its reach:

    Failed to send message: /home/e-work/MUSIC/.../traces.png:
    /home/e-work/MUSIC/.../traces.png (Permission denied)
    (AttachmentInvalidException)

— either way the gateway stat()ed the file successfully a line earlier.
signal-cli accepts an RFC 2397 data URI in place of a path (its own
``--attachment`` help documents ``data:<MIME>;filename=<NAME>;base64,<DATA>``),
so the bytes can go inline. Every sender takes that route: single files, image
batches (where the 2026-10-16 plots were lost), and the send_message tool.

The retry is gated on that specific error. A transient failure must never be
retried: the first send may have gone through, and the recipient would get the
file twice.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
import pytest

import gateway.platforms.signal as signal_mod
import gateway.platforms.signal_attachments as signal_attachments
from gateway.config import PlatformConfig
from gateway.platforms.signal import SignalAdapter
from gateway.platforms.signal_attachments import (
    SIGNAL_MAX_INLINE_ATTACHMENT_SIZE,
    inline_attachment_uri,
    is_unreadable_attachment_error,
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
    assert is_unreadable_attachment_error(UNREADABLE)
    assert is_unreadable_attachment_error(
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
    assert not is_unreadable_attachment_error(err)


# ── the data URI ──────────────────────────────────────────────────────────

def test_inline_uri_shape_and_payload(tmp_path) -> None:
    f = tmp_path / "routine.md"
    f.write_bytes(b"# hi\n")
    uri = inline_attachment_uri(str(f))

    assert uri.startswith("data:text/markdown;filename=routine.md;base64,")
    payload = uri.split(";base64,", 1)[1]
    assert base64.b64decode(payload) == b"# hi\n"


def test_unknown_extension_gets_a_generic_mime(tmp_path) -> None:
    f = tmp_path / "blob.zzz"
    f.write_bytes(b"x")
    uri = inline_attachment_uri(str(f))
    assert uri.startswith("data:application/octet-stream;")


def test_pdf_mime_is_detected(tmp_path) -> None:
    """The point of the exercise: a rendered PDF must arrive as a PDF."""
    f = tmp_path / "routine.pdf"
    f.write_bytes(b"%PDF-1.4\n")
    uri = inline_attachment_uri(str(f))
    assert uri.startswith("data:application/pdf;filename=routine.pdf;base64,")


def test_filename_separators_are_escaped(tmp_path) -> None:
    """``;`` and ``,`` delimit fields in the URI.

    An unescaped one would truncate the payload and signal-cli would receive a
    corrupt attachment rather than an error.
    """
    f = tmp_path / "a;b,c.md"
    f.write_bytes(b"z")
    uri = inline_attachment_uri(str(f))

    head, payload = uri.split(";base64,", 1)
    assert base64.b64decode(payload) == b"z"
    name = head.split(";filename=", 1)[1]
    assert ";" not in name and "," not in name
    assert unquote(name) == "a;b,c.md"


def test_unreadable_file_returns_none(tmp_path) -> None:
    missing = tmp_path / "gone.md"
    assert inline_attachment_uri(str(missing)) is None


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


# ── every sender, against a daemon that cannot read our paths ─────────────

class FakeSignalCli:
    """signal-cli's JSON-RPC ``send`` as a daemon running as another user.

    A path under ``readable`` opens; any other path fails the whole send the
    way signal-cli does — nothing sent, AttachmentInvalidException naming the
    path. A data URI always works. ``fail_attachments`` makes every send that
    carries attachments fail some other way. ``delivered`` is what the chat
    receives: ``(message, [(filename, bytes)])`` per message.
    """

    def __init__(self, readable: Path | None = None, fail_attachments: str | None = None):
        self.readable = readable
        self.fail_attachments = fail_attachments
        self.sends: list[list[str]] = []
        self.delivered: list[tuple[str, list[tuple[str, bytes]]]] = []

    def _error(self, rpc_id, message):
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32603, "message": message}},
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] != "send":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        params = body["params"]
        attachments = list(params.get("attachments") or [])
        self.sends.append(attachments)
        if attachments and self.fail_attachments:
            return self._error(body["id"], self.fail_attachments)
        files = []
        for att in attachments:
            if att.startswith("data:"):
                head, payload = att.split(";base64,", 1)
                files.append((unquote(head.split(";filename=", 1)[1]), base64.b64decode(payload)))
            elif self.readable is not None and Path(att).is_relative_to(self.readable):
                files.append((Path(att).name, Path(att).read_bytes()))
            else:
                return self._error(
                    body["id"],
                    f"Failed to send message: {att}: {att} (Permission denied) "
                    f"(AttachmentInvalidException) (UnexpectedErrorException)",
                )
        self.delivered.append((params.get("message", ""), files))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"timestamp": 1_700_000_000_000 + len(self.delivered),
                           "results": [{"type": "SUCCESS"}]},
            },
        )

    def files(self) -> dict[str, bytes]:
        return {name: data for _msg, files in self.delivered for name, data in files}

    def notices(self) -> list[str]:
        return [msg for msg, files in self.delivered if not files and "⚠️" in msg]


def _live_adapter(daemon: FakeSignalCli) -> SignalAdapter:
    adapter = SignalAdapter(
        PlatformConfig(enabled=True, extra={"http_url": "http://signal.test", "account": "+15550000000"})
    )
    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(daemon.handle))
    return adapter


def _pngs(directory: Path, *sizes: int) -> list[Path]:
    paths = []
    for i, size in enumerate(sizes):
        path = directory / f"plot_{i}.png"
        path.write_bytes(bytes([i]) * size)
        paths.append(path)
    return paths


def _file_urls(paths) -> list[tuple[str, str]]:
    return [(f"file://{quote(str(p))}", "") for p in paths]


def test_images_the_daemon_cannot_read_arrive_inline(tmp_path) -> None:
    """The 2026-10-16 plots: four PNGs under a home directory signal-cli may not enter."""
    daemon = FakeSignalCli()
    pngs = _pngs(tmp_path, 300, 400, 500, 600)

    asyncio.run(_live_adapter(daemon).send_multiple_images("group:g1", _file_urls(pngs)))

    assert daemon.files() == {p.name: p.read_bytes() for p in pngs}
    assert not daemon.notices()


def test_images_the_daemon_can_read_go_as_paths(tmp_path) -> None:
    daemon = FakeSignalCli(readable=tmp_path)
    pngs = _pngs(tmp_path, 10, 20)

    asyncio.run(_live_adapter(daemon).send_multiple_images("group:g1", _file_urls(pngs)))

    assert daemon.sends == [[str(p) for p in pngs]]
    assert daemon.files() == {p.name: p.read_bytes() for p in pngs}


def test_inline_sends_keep_to_the_size_cap(tmp_path, monkeypatch) -> None:
    """Inline payloads are split by bytes; a file over the cap alone is named, not dropped silently."""
    monkeypatch.setattr(signal_attachments, "SIGNAL_MAX_INLINE_ATTACHMENT_SIZE", 1000)
    daemon = FakeSignalCli()
    small_a, small_b, small_c, big = _pngs(tmp_path, 400, 400, 400, 2000)

    asyncio.run(
        _live_adapter(daemon).send_multiple_images(
            "group:g1", _file_urls([small_a, small_b, small_c, big])
        )
    )

    inline_sends = [send for send in daemon.sends if send and send[0].startswith("data:")]
    assert all(
        sum(len(base64.b64decode(uri.split(";base64,", 1)[1])) for uri in send) <= 1000
        for send in inline_sends
    )
    assert set(daemon.files()) == {small_a.name, small_b.name, small_c.name}
    assert daemon.notices() == [f"⚠️ Couldn't deliver the image attachment ({big.name})."]


def test_a_failed_image_batch_is_named_in_the_chat(tmp_path, monkeypatch) -> None:
    """A failure the inline resend cannot fix still reaches the user, not just the log."""
    async def _no_wait(_seconds):
        return None

    monkeypatch.setattr(signal_mod.asyncio, "sleep", _no_wait)
    daemon = FakeSignalCli(fail_attachments="Untrusted identity key")
    pngs = _pngs(tmp_path, 10, 20)

    asyncio.run(_live_adapter(daemon).send_multiple_images("group:g1", _file_urls(pngs)))

    assert not daemon.files()
    assert daemon.notices() == [
        f"⚠️ Couldn't deliver 2 image attachments ({pngs[0].name}, {pngs[1].name})."
    ]


def test_send_image_from_a_path_the_daemon_cannot_read(tmp_path) -> None:
    daemon = FakeSignalCli()
    (png,) = _pngs(tmp_path, 50)

    result = asyncio.run(
        _live_adapter(daemon).send_image("group:g1", f"file://{quote(str(png))}", caption="the plot")
    )

    assert result.success
    assert daemon.delivered[-1] == ("the plot", [(png.name, png.read_bytes())])


def test_send_message_tool_resends_inline(tmp_path, monkeypatch) -> None:
    """Cron delivery and ``son-of-anton send`` talk to signal-cli without the adapter."""
    from tools.send_message_tool import _send_signal

    daemon = FakeSignalCli()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(*args, transport=httpx.MockTransport(daemon.handle), **kwargs),
    )
    pngs = _pngs(tmp_path, 30, 40)

    result = asyncio.run(
        _send_signal(
            {"http_url": "http://signal.test", "account": "+15550000000"},
            "group:g1",
            "the plots",
            media_files=[(str(p), False) for p in pngs],
        )
    )

    assert result.get("success"), result
    assert daemon.delivered == [("the plots", [(p.name, p.read_bytes()) for p in pngs])]


def test_a_failed_attachment_after_streaming_is_named_in_the_chat(tmp_path, monkeypatch) -> None:
    """The streamed reply's MEDIA: delivery used to drop failures silently."""
    from gateway.platforms.base import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from gateway.config import Platform

    monkeypatch.setattr(signal_mod, "SIGNAL_MAX_INLINE_ATTACHMENT_SIZE", 10)
    daemon = FakeSignalCli()
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4\n" + b"\0" * 100)
    runner = object.__new__(GatewayRunner)
    event = MessageEvent(
        text="send it",
        source=SessionSource(platform=Platform.SIGNAL, chat_id="group:g1", chat_type="group"),
    )

    asyncio.run(
        runner._deliver_media_from_response(
            f"Here it is.\nMEDIA:{report}", event, _live_adapter(daemon), thread_metadata={}
        )
    )

    assert not daemon.files()
    assert daemon.notices() == [f"⚠️ Couldn't deliver the file attachment ({report.name})."]
