"""The soa-d protocol client (daemon/phases/02a-server.md).

Newline-delimited JSON-RPC 2.0 over ``<home>/run/soa-d.sock``. One reader
thread demultiplexes responses (to the waiting ``call``) and ``event``
notifications (to the callbacks registered with ``on_event``).
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from typing import Callable, Optional

PROTOCOL = 1
CLIENT_NAME = "tui"


class DaemonError(Exception):
    """An error response (``code`` is the JSON-RPC code), or ``code`` 0 when
    there is no daemon or the connection broke."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


def socket_path(home: str) -> str:
    return os.path.join(str(home), "run", "soa-d.sock")


class Client:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._file = sock.makefile("rb")
        self._wlock = threading.Lock()
        self._plock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[int, list] = {}  # id -> [Event, response|None]
        self._callbacks: list[Callable[[dict], None]] = []
        self._closed = False
        self._lost_sent = False
        self.hello: dict = {}
        self._reader = threading.Thread(target=self._read_loop, name="soa-d-reader", daemon=True)
        self._reader.start()

    # ------------------------------------------------------------------
    def on_event(self, fn: Callable[[dict], None]) -> None:
        """``fn(params)`` runs on the reader thread for every event. It must not
        call the daemon itself (the reader is what delivers the answer)."""
        self._callbacks.append(fn)

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        rid = next(self._ids)
        slot: list = [threading.Event(), None]
        with self._plock:
            if self._closed:
                raise DaemonError(0, "connection to soa-d is closed")
            self._pending[rid] = slot
        line = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}) + "\n"
        try:
            with self._wlock:
                self._sock.sendall(line.encode("utf-8"))
        except OSError as exc:
            with self._plock:
                self._pending.pop(rid, None)
            raise DaemonError(0, f"connection to soa-d broke: {exc}") from exc
        if not slot[0].wait(timeout):
            with self._plock:
                self._pending.pop(rid, None)
            raise DaemonError(0, f"soa-d did not answer {method} within {timeout:g}s")
        resp = slot[1]
        if resp is None:
            raise DaemonError(0, "connection to soa-d lost")
        if "error" in resp and resp["error"] is not None:
            err = resp["error"]
            raise DaemonError(int(err.get("code", 0)), str(err.get("message", "")))
        return resp.get("result") or {}

    def close(self) -> None:
        with self._plock:
            self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._fail_pending()

    # ------------------------------------------------------------------
    def _fail_pending(self) -> None:
        with self._plock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot[0].set()

    def _read_loop(self) -> None:
        try:
            for raw in self._file:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("method") == "event":
                    params = msg.get("params")
                    if isinstance(params, dict):
                        for fn in list(self._callbacks):
                            try:
                                fn(params)
                            except Exception:
                                pass
                    continue
                rid = msg.get("id")
                with self._plock:
                    slot = self._pending.pop(rid, None) if isinstance(rid, int) else None
                if slot is not None:
                    slot[1] = msg
                    slot[0].set()
        except (OSError, ValueError):
            pass
        finally:
            self._fail_pending()
            with self._plock:
                already = self._closed or self._lost_sent
                self._lost_sent = True
                self._closed = True
            if not already:
                for fn in list(self._callbacks):
                    try:
                        fn({"kind": "connection.lost"})
                    except Exception:
                        pass


# ----------------------------------------------------------------------
def _open(path: str) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(5.0)
        s.connect(path)
        s.settimeout(None)
    except OSError:
        s.close()
        raise
    return s


def _say_hello(sock: socket.socket) -> Client:
    client = Client(sock)
    try:
        client.hello = client.call("hello", {"protocol": PROTOCOL, "client": CLIENT_NAME}, timeout=10.0)
    except DaemonError:
        client.close()
        raise
    return client


def _start_daemon(home: str) -> None:
    binary = os.environ.get("SOA_D_BIN") or shutil.which("soa-d")
    if not binary:
        raise DaemonError(0, "soa-d is not installed (not on PATH, and SOA_D_BIN is unset)")
    logs = os.path.join(home, "logs")
    os.makedirs(logs, exist_ok=True)
    with open(os.path.join(logs, "soa-d.log"), "ab") as log:
        try:
            proc = subprocess.Popen(
                [binary, "serve", "--home", home],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
        except OSError as exc:
            raise DaemonError(0, f"could not start soa-d: {exc}") from exc
    # A second starter's serve exits "already running"; reap whoever exits.
    threading.Thread(target=proc.wait, name="soa-d-reap", daemon=True).start()


def connect(home: str, *, autostart: bool = True, timeout: float = 10.0) -> Client:
    home = str(home)
    path = socket_path(home)
    try:
        return _say_hello(_open(path))
    except OSError:
        pass
    if not autostart:
        raise DaemonError(0, f"soa-d is not running (no answer on {path})")
    _start_daemon(home)
    deadline = time.monotonic() + timeout
    while True:
        try:
            return _say_hello(_open(path))
        except OSError:
            pass
        except DaemonError as exc:
            if exc.code != 0:
                raise
        if time.monotonic() >= deadline:
            raise DaemonError(0, f"soa-d did not start within {timeout:g}s (see {os.path.join(home, 'logs', 'soa-d.log')})")
        time.sleep(0.05)
