"""The Textual TUI's backend: a client of the soa-d daemon.

``DaemonBackend`` owns one attached session. It turns the daemon's events
(daemon/phases/02a-server.md) into the typed events the app renders
(``SonOfAntonTUIApp._ev_*``), and the app's actions into protocol calls.
Slash commands are handled here; none reaches the Python CLI.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from son_of_anton_tui import daemon as _daemon

# The cycle shift+tab walks. Same set and spelling as ``/perm``.
PERMISSION_MODES = ("default", "ask", "lockdown", "yolo")

INTERRUPTED_TEXT = "[response interrupted]"
FAILED_TEXT = "[response failed]"
NOT_IN_SOAD = "session {id} is not in soa-d (Python sessions are imported in phase 8)"

# Canonical names of the commands this client handles (the rest say so).
SUPPORTED = (
    "model", "reasoning", "perm", "new", "clear", "resume", "sessions", "commit",
    "stop", "sendnow", "sendall", "cancel", "status", "help", "skin", "quit", "compact", "pickup",
)


class Sink:
    """Where backend events go.  The Textual app implements ``emit``."""

    def emit(self, kind: str, **payload: Any) -> None:  # pragma: no cover - protocol
        raise NotImplementedError

    def run_with_terminal(self, fn: Callable[[], Any]) -> Any:  # pragma: no cover - protocol
        raise NotImplementedError


def looks_like_command(text: str) -> bool:
    """A slash command, not a pasted path (``/usr/bin/x`` has more slashes)."""
    if not text or text[0] not in "/:":
        return False
    words = text.split()
    if not words:
        return False
    if text[0] == ":":
        from son_of_anton_cli.commands import resolve_command

        return resolve_command(words[0]) is not None
    return "/" not in words[0][1:]


def tool_summary(name: str, arguments: Any) -> str:
    """One line for a tool call, as the daemon's ``summary`` (history rows)."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
    except (ValueError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    pick = {
        "terminal": "command", "read_file": "path", "write_file": "path", "patch": "path",
        "search_files": "pattern", "clarify": "question",
    }.get(name)
    text = str(args.get(pick, "")) if pick else ""
    if name == "task":
        text = _task_label(args)
    if not text:
        raw = arguments if isinstance(arguments, str) else json.dumps(args)
        text = raw or name
    text = " ".join(text.split())
    return text[:120]


def titlecase(text: str) -> str:
    """opencode's ``Locale.titlecase``: each letter that starts a word, upper-cased."""
    return re.sub(r"\b\w", lambda m: m.group().upper(), text)


def _task_label(args: dict) -> str:
    """A task row's label, as the daemon's summary has it."""
    agent = titlecase(str(args.get("subagent_type") or "general"))
    background = " (background)" if args.get("background") is True else ""
    return f"{agent} Task{background} — {args.get('description') or ''}"


def subagent_duration(ms: Any) -> str:
    """opencode's ``Locale.duration``."""
    ms = int(ms or 0)
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    if ms < 3_600_000:
        return f"{ms // 60_000}m {ms % 60_000 // 1000}s"
    if ms < 86_400_000:
        return f"{ms // 3_600_000}h {ms % 3_600_000 // 60_000}m"
    return f"{ms // 86_400_000}d {ms % 86_400_000 // 3_600_000}h"


def _done_detail(tool_calls: Any, duration_ms: Any) -> str:
    """The line under a finished task row (opencode's formatCompletedSubagentDetail)."""
    n = int(tool_calls or 0)
    duration = subagent_duration(duration_ms)
    return f"↳ {duration}" if n == 0 else f"↳ {n} toolcall{'' if n == 1 else 's'} · {duration}"


_TASK_RESULT = re.compile(r'<task id="([^"]+)" state="([^"]+)"')
_CHILD_TITLE = re.compile(r"(.*) \(@(.+) subagent\)")


def _parse_time(value: Any) -> Optional[float]:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _percent(tokens: int, length: Optional[int]) -> Optional[int]:
    if not length:
        return None
    return round(100 * int(tokens or 0) / length)


def _compact_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _preview(text: str, room: int = 100) -> str:
    preview = " ".join(str(text).split())
    return preview if len(preview) <= room else preview[: room - 1] + "…"


def _result_is_error(content: Any) -> bool:
    try:
        obj = json.loads(content) if isinstance(content, str) else content
    except ValueError:
        return False
    return isinstance(obj, dict) and bool(obj.get("error"))


class _Stream:
    """What turns one attached session's daemon events into typed ones.

    The parent (the driver) and a viewed child each have their own: the open
    block, the open tool calls, the last row seen and the events held while
    an attach is in flight are per session, not per connection.
    """

    def __init__(self, sid: Optional[str], emit: Callable[..., None]) -> None:
        self.sid = sid
        self.emit = emit
        self.open: Optional[str] = None  # "assistant" | "reasoning"
        self.calls: dict = {}  # call_id -> (name, summary)
        self.action = ""
        self.last_seq = 0
        self._lock = threading.Lock()  # guards attaching/held
        self.attaching = False
        self.held: list = []

    def note(self, text: str, muted: bool = True) -> None:
        self.emit("ansi", text=text, stderr=muted)

    def reset(self) -> None:
        self.open = None
        self.calls.clear()
        self.action = ""

    # ---- events arriving while an attach is in flight are held ----
    def begin(self) -> None:
        with self._lock:
            self.attaching = True
            self.held = []

    def deliver(self, p: dict, handle: Callable[[dict], None]) -> None:
        with self._lock:
            if self.attaching:
                self.held.append(p)
                return
        handle(p)

    def end(self, handle: Callable[[dict], None]) -> None:
        """Handle what was held, in order, then deliver directly again."""
        while True:
            with self._lock:
                if not self.held:
                    self.attaching = False
                    return
                p = self.held.pop(0)
            handle(p)

    # ---- history ----
    def history(self, messages: list, *, more_before: bool = False, resumed: bool = False,
                compactions: Optional[list] = None) -> list:
        """Draw stored rows. Returns the task results among them as
        ``(call_id, child_id, state)``."""
        results: list = []
        if not messages:
            return results
        # A compaction is drawn after the row it summarizes up to.
        after_seq = {int(c.get("through_seq") or 0): c for c in compactions or []}
        if resumed:
            hidden = " · earlier messages not shown" if more_before else ""
            self.note(f"resumed session {self.sid}{hidden}")
        open_calls: dict = {}
        hint = False  # the message just drawn called task: its hint follows its tool rows
        for m in messages:
            self.last_seq = max(self.last_seq, int(m.get("seq") or 0))
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if hint and role != "tool":
                self.emit("hint")
                hint = False
            if role == "user":
                if content.startswith("[Background process"):
                    self.note(content.splitlines()[0][:200])
                elif content:
                    self.emit("user", text=content)
            elif role == "assistant":
                if content in (INTERRUPTED_TEXT, FAILED_TEXT):
                    self.note(content)
                else:
                    self._render_assistant(m, content, open_calls)
                    hint = _calls_task(m)
            elif role == "tool":
                cid = m.get("tool_call_id", "")
                name, label = open_calls.pop(cid, (m.get("tool_name", ""), ""))
                found = _TASK_RESULT.match(m.get("content") or "") if name == "task" else None
                if found:
                    results.append((cid, found.group(1), found.group(2)))
                self.emit("tool_done", name=name, label=label, duration=0.0,
                          is_error=_result_is_error(m.get("content")) or bool(found and found.group(2) == "error"),
                          call_id=cid)
            done = after_seq.get(int(m.get("seq") or 0))
            if done is not None:
                self.emit("compaction", text=done.get("summary", ""), stats={})
        if hint:
            self.emit("hint")
        for cid, (name, label) in open_calls.items():
            self.emit("tool_done", name=name, label=label, duration=0.0, is_error=False, call_id=cid)
        return results

    def _render_assistant(self, m: dict, content: str, open_calls: dict) -> None:
        if (m.get("reasoning") or "").strip():
            self.emit("reasoning_block", text=m["reasoning"])
        if content:
            self.emit("markdown", text=content)
        for call in m.get("tool_calls") or []:
            name = call.get("name", "")
            label = tool_summary(name, call.get("arguments"))
            open_calls[call.get("id", "")] = (name, label)
            self.emit("tool_start", name=name, label=label, call_id=call.get("id", ""))

    # ---- live events ----
    def close_open(self) -> None:
        if self.open == "assistant":
            self.emit("assistant_end")
        elif self.open == "reasoning":
            self.emit("reasoning_end")
        self.open = None

    def event(self, p: dict) -> bool:
        """Translate a transcript event; False when ``p`` is not one."""
        kind = p.get("kind")
        if kind == "turn.start":
            text = p.get("text") or ""
            self.close_open()
            if text.startswith("[Background process"):
                self.note(text.splitlines()[0][:200])
            else:
                self.emit("user", text=text)
        elif kind == "reasoning.delta":
            if self.open != "reasoning":
                self.close_open()
                self.open = "reasoning"
                self.emit("reasoning_start")
            self.emit("reasoning_delta", text=p.get("text", ""))
        elif kind == "assistant.delta":
            if self.open != "assistant":
                self.close_open()
                self.open = "assistant"
                self.emit("assistant_start")
            self.emit("assistant_delta", text=p.get("text", ""))
        elif kind == "tool.start":
            self.close_open()
            cid, name, summary = p.get("call_id", ""), p.get("name", ""), p.get("summary", "")
            self.calls[cid] = (name, summary)
            self.action = summary or name
            self.emit("tool_start", name=name, label=summary, call_id=cid)
        elif kind == "tool.end":
            cid = p.get("call_id", "")
            name, summary = self.calls.pop(cid, (p.get("name", ""), ""))
            self.action = ""
            name = name or p.get("name", "")
            # A task row's own time is its child's: the row shows none.
            self.emit("tool_done", name=name, label=summary,
                      duration=0.0 if name == "task" else float(p.get("duration_ms") or 0) / 1000.0,
                      is_error=bool(p.get("is_error")), call_id=cid)
        elif kind == "message":
            self.close_open()
            m = p.get("message") or {}
            self.last_seq = max(self.last_seq, int(m.get("seq") or 0))
            # Its tool rows are all drawn by now: the step is stored after the tools ran.
            if m.get("role") == "assistant" and _calls_task(m):
                self.emit("hint")
        elif kind == "turn.end":
            self.close_open()
            self.calls.clear()
            self.action = ""
            self.emit("turn_end")
            status = p.get("status")
            if status == "interrupted":
                self.note("interrupted")
            elif status == "error":
                self.note(f"error: {p.get('error') or 'the turn failed'}", muted=False)
        elif kind == "compaction":
            self.close_open()
            self.emit("compaction", text=p.get("summary", ""), stats={"before_tokens": p.get("before_tokens")})
        elif kind == "notice":
            self.note(p.get("text", ""), muted=p.get("level", "info") == "info")
        else:
            return False
        return True


def _calls_task(message: dict) -> bool:
    return any(c.get("name") == "task" for c in message.get("tool_calls") or [])


class DaemonBackend:
    def __init__(self, home: str, cwd: str, *, resume: Optional[str] = None,
                 continue_last: bool = False, model: Optional[str] = None, yolo: bool = False,
                 reasoning: Optional[str] = None) -> None:
        self.home = str(home)
        self.cwd = str(cwd)
        self._resume = resume
        self._continue_last = continue_last
        self._model = model
        self._yolo = yolo
        self._reasoning = reasoning
        self.session_id: Optional[str] = None
        self.session: dict = {}
        self.role = "driver"
        self.provider = ""
        self._history_file = Path(self.home) / ".son_of_anton_history"
        self._client: Optional[_daemon.Client] = None
        self._sink: Optional[Sink] = None
        self._pending: list = []  # (kind, payload) emitted before the app mounted
        self._elock = threading.RLock()
        self._main = _Stream(None, self.emit)
        self._queue: list = []  # [{id, kind, text}]
        # The subagent view: the child attached as a viewer, beside the driven
        # session, on the same connection. ``view_id`` is None when none is shown.
        self.view_id: Optional[str] = None
        self._view: Optional[_Stream] = None
        self._kids: list = []  # the session's children (session objects), in creation order
        self._task_rows: dict = {}  # child id -> the call id of its task row
        self._running_kids: set = set()
        self._closing = False
        self._reconnecting = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> bool:
        try:
            self._client = _daemon.connect(self.home)
            self._client.on_event(self._on_event)
            sid = self._resume
            if sid == "latest" or (not sid and self._continue_last):
                # -c continues this workspace's newest session (the daemon
                # resolves the git root); --resume latest takes the newest anywhere.
                query: dict = {"kinds": ["interactive"], "limit": 1}
                if sid != "latest":
                    query["cwd"] = self.cwd
                rows = self._client.call("session.list", query).get("sessions") or []
                sid = rows[0]["id"] if rows else None
                if sid is None and self._continue_last:
                    print(f"son-of-anton: no session to continue in {self.cwd}; starting a new one", file=sys.stderr)
            resumed = bool(sid)
            if not sid:
                sid = self._client.call("session.create", {"cwd": self.cwd})["session"]["id"]
            self._attach(sid, resumed=resumed)
            params: dict = {}
            if self._model:
                params["model"] = self._model
            if self._reasoning:
                params["reasoning_effort"] = self._reasoning
            if self._yolo:
                params["mode"] = "yolo"
            if params:
                self._set(**params)
            return True
        except _daemon.DaemonError as exc:
            if exc.code == -32011 and self._resume:
                print(NOT_IN_SOAD.format(id=self._resume), file=sys.stderr)
            else:
                print(f"son-of-anton: {exc.message}", file=sys.stderr)
            self._closing = True
            client, self._client = self._client, None
            if client is not None:
                client.close()
            return False

    def finish(self) -> None:
        """Detach and close. Never stops the daemon or a running turn."""
        self._closing = True
        client, self._client = self._client, None
        if client is None:
            return
        if self.session_id:
            try:
                client.call("session.detach", {"session": self.session_id}, timeout=3.0)
            except _daemon.DaemonError:
                pass
        client.close()

    # ------------------------------------------------------------------
    # Sink wiring
    # ------------------------------------------------------------------
    def attach(self, sink: Sink, feed_width: int = 0) -> None:
        with self._elock:
            self._sink = sink
            pending, self._pending = self._pending, []
            for kind, payload in pending:
                self._send_to_sink(kind, payload)

    def detach(self) -> None:
        with self._elock:
            self._sink = None

    def set_feed_width(self, width: int) -> None:
        pass

    def run_with_terminal(self, fn: Callable[[], Any]) -> Any:
        runner = getattr(self._sink, "run_with_terminal", None)
        return runner(fn) if runner else fn()

    def _send_to_sink(self, kind: str, payload: dict) -> None:
        try:
            self._sink.emit(kind, **payload)  # type: ignore[union-attr]
        except Exception:
            pass

    def emit(self, kind: str, **payload: Any) -> None:
        with self._elock:
            if self._sink is None:
                if len(self._pending) < 5000:
                    self._pending.append((kind, payload))
                return
            self._send_to_sink(kind, payload)

    def note(self, text: str, muted: bool = True) -> None:
        self.emit("ansi", text=text, stderr=muted)

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------
    def _call(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        client = self._client
        if client is None:
            raise _daemon.DaemonError(0, "not connected to soa-d")
        return client.call(method, params, timeout)

    def _sess(self, **extra: Any) -> dict:
        return {"session": self.session_id, **extra}

    def _set(self, **fields: Any) -> dict:
        res = self._call("session.set", self._sess(**fields))
        if isinstance(res.get("session"), dict):
            self._update_session(res["session"])
        return self.session

    def _update_session(self, state: dict) -> None:
        self.session = state
        self.emit("status", running=state.get("status") == "running")

    def _attach(self, sid: str, *, after_seq: Optional[int] = None, resumed: bool = True,
                clear: bool = False) -> None:
        params: dict = {"session": sid, "as": "driver"}
        if after_seq:
            params["after_seq"] = after_seq
        st = self._main
        st.begin()
        previous = self.session_id
        self.session_id = sid
        try:
            try:
                res = self._call("session.attach", params)
            except BaseException:
                self.session_id = previous
                raise
            st.reset()
            st.sid = sid
            if sid != previous:
                self._kids, self._task_rows = [], {}
                self._set_running(set())
            self.role = res.get("role", "driver")
            self._queue = list(res.get("queue") or [])
            if res.get("gap") or clear:
                self.emit("clear")
            self._update_session(res.get("session") or {})
            self.emit("queue", items=[q.get("text", "") for q in self._queue])
            results = st.history(res.get("messages") or [], more_before=bool(res.get("more_before")),
                                 resumed=resumed, compactions=res.get("compactions") or [])
            self._fill_task_details(results)
        finally:
            st.end(lambda p: self._handle(p) if self.session_id == sid else None)

    def _fill_task_details(self, results: list) -> None:
        """Map the history's task rows to their children, and write what each did."""
        kids = {k.get("id"): k for k in self.children(refresh=True)}
        for call_id, child_id, state in results:
            self._task_rows[child_id] = call_id
            kid = kids.get(child_id)
            if state in ("completed", "running") and kid is not None and kid.get("status") != "running":
                self.emit("tool_detail", call_id=call_id,
                          detail=_done_detail(kid.get("tool_calls"), kid.get("duration_ms")))

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def _on_event(self, p: dict) -> None:
        if p.get("kind") == "connection.lost":
            self._lost()
            return
        sid = p.get("session")
        if sid == self.session_id:
            self._main.deliver(p, self._handle)
        elif sid is not None and sid == self.view_id:
            view = self._view
            if view is not None:
                view.deliver(p, lambda q: self._handle_view(view, q))

    def _handle(self, p: dict) -> None:
        kind = p.get("kind")
        if kind == "tool.start" and p.get("name") == "task":
            # A call that continues a child has no child.created: its row is this one.
            try:
                resumed = json.loads(p.get("arguments") or "{}").get("task_id")
            except (ValueError, AttributeError):
                resumed = None
            if resumed:
                self._task_rows[str(resumed)] = p.get("call_id", "")
        if self._main.event(p):
            return
        if kind == "queue.updated":
            self._queue = list(p.get("items") or [])
            self.emit("queue", items=[q.get("text", "") for q in self._queue])
        elif kind == "session.updated":
            if isinstance(p.get("state"), dict):
                self._update_session(p["state"])
        elif kind == "role":
            self.role = p.get("as", self.role)
            if self.role != "driver":
                self.note("another client is now the driver of this session")
        elif kind == "approval.request":
            child = p.get("child_session") or ""
            self.emit("approval", approval_id=p.get("approval_id", ""), approval_kind=p.get("approval_kind", ""), subject=p.get("subject", ""),
                      reasons=p.get("reasons") or [], choices=p.get("choices") or [],
                      subagent=self._task_title(child) if child else "")
        elif kind == "approval.resolved":
            self.emit("approval_resolved", approval_id=p.get("approval_id", ""))
        elif kind == "question.request":
            self.emit("question", question_id=p.get("question_id", ""), text=p.get("text", ""),
                      choices=p.get("choices") or [], multi_select=bool(p.get("multi_select")))
        elif kind == "question.resolved":
            self.emit("question_resolved", question_id=p.get("question_id", ""))
        elif kind == "child.created":
            child = p.get("child") or {}
            if child.get("id"):
                self._task_rows[child["id"]] = p.get("call_id", "")
                if not any(k.get("id") == child["id"] for k in self._kids):
                    self._kids.append(child)
                self._set_running(self._running_kids | {child["id"]})
        elif kind == "child.progress":
            call_id = self._task_rows.get(p.get("child_id", ""))
            if call_id:
                self.emit("tool_detail", call_id=call_id, detail=f"↳ {p.get('name', '')} {p.get('summary', '')}".rstrip())
        elif kind == "child.done":
            child_id = p.get("child_id", "")
            self._set_running(self._running_kids - {child_id})
            call_id = self._task_rows.get(child_id)
            if call_id:
                detail = _done_detail(p.get("tool_calls"), p.get("duration_ms")) if p.get("status") == "done" else ""
                self.emit("tool_detail", call_id=call_id, detail=detail)

    def _set_running(self, ids: set) -> None:
        if ids != self._running_kids:
            self._running_kids = ids
            self.emit("subagents", running=len(ids))

    def _task_title(self, child_id: str) -> str:
        """``<Agent> Task — <description>`` for a child, read from its title."""
        for k in self._kids:
            if k.get("id") == child_id:
                found = _CHILD_TITLE.fullmatch(k.get("title") or "")
                if found:
                    return f"{titlecase(found.group(2))} Task — {found.group(1)}"
        return "Subagent"

    # ------------------------------------------------------------------
    # Connection loss
    # ------------------------------------------------------------------
    def _lost(self) -> None:
        if self._closing or self._reconnecting:
            return
        self._reconnecting = True
        self._main.open = None
        if self.view_id is not None:
            self.view_id, self._view = None, None
            self.emit("view_close")
        self.emit("prompts_clear")
        self.emit("status", running=False)
        self.note("lost the connection to soa-d; reconnecting…", muted=False)
        threading.Thread(target=self._reconnect, name="soa-d-reconnect", daemon=True).start()

    def _reconnect(self) -> None:
        delay = 0.5
        try:
            while not self._closing:
                try:
                    old, self._client = self._client, None
                    if old is not None:
                        old.close()
                    client = _daemon.connect(self.home)
                    client.on_event(self._on_event)
                    self._client = client
                    self._attach(self.session_id or "", after_seq=self._main.last_seq, resumed=False)
                    self.note("reconnected to soa-d")
                    return
                except _daemon.DaemonError as exc:
                    if exc.code == -32011:
                        self.note(NOT_IN_SOAD.format(id=self.session_id), muted=False)
                        return
                    time.sleep(delay)
                    delay = min(delay * 2, 10.0)
        finally:
            self._reconnecting = False

    # ------------------------------------------------------------------
    # Subagents
    # ------------------------------------------------------------------
    def children(self, refresh: bool = False) -> list:
        """The session's children in creation order (session objects)."""
        if refresh and self.session_id:
            try:
                kids = self._call("session.children", self._sess()).get("children") or []
            except _daemon.DaemonError:
                return list(self._kids)
            self._kids = list(kids)
            self._set_running({k["id"] for k in kids if k.get("status") == "running"})
        return list(self._kids)

    def _view_emit(self, cid: str) -> Callable[..., None]:
        return lambda kind, **payload: self.emit("view", sid=cid, event=kind, args=payload)

    def view(self, cid: str) -> None:
        """Show a child's transcript: attach it as a viewer, beside the driven session."""
        old, self.view_id = self.view_id, cid
        st = self._view = _Stream(cid, self._view_emit(cid))
        if old and old != cid:
            self.detach_session(old)
        st.begin()
        try:
            res = self._call("session.attach", {"session": cid, "as": "viewer"})
            if self.view_id != cid:  # released while the call was in flight
                return
            st.reset()
            self.emit("view", sid=cid, event="state", args={"state": res.get("session") or {}})
            st.history(res.get("messages") or [], compactions=res.get("compactions") or [])
        except _daemon.DaemonError as exc:
            self.note(f"could not open the subagent: {exc.message}", muted=False)
        finally:
            st.end(lambda p: self._handle_view(st, p) if self.view_id == cid else None)

    def _handle_view(self, st: _Stream, p: dict) -> None:
        if p.get("kind") == "session.updated":
            if isinstance(p.get("state"), dict):
                st.emit("state", state=p["state"])
        else:
            st.event(p)

    def release_view(self) -> Optional[str]:
        """Stop showing the child now; returns its id, to detach with ``detach_session``."""
        old, self.view_id, self._view = self.view_id, None, None
        return old

    def detach_session(self, sid: str) -> None:
        try:
            self._call("session.detach", {"session": sid}, timeout=5.0)
        except _daemon.DaemonError:
            pass

    # ------------------------------------------------------------------
    # What the app asks for
    # ------------------------------------------------------------------
    def send(self, text: str, mode: str = "queue") -> dict:
        try:
            return self._call("turn.send", self._sess(text=text, mode=mode))
        except _daemon.DaemonError as exc:
            self.note(f"could not send: {exc.message}", muted=False)
            return {}

    def interrupt_turn(self) -> bool:
        if self.session.get("status") != "running" or self._client is None:
            return False

        def _go() -> None:
            try:
                self._call("turn.interrupt", self._sess(), timeout=10.0)
            except _daemon.DaemonError:
                pass

        threading.Thread(target=_go, name="soa-d-interrupt", daemon=True).start()
        return True

    def answer_approval(self, approval_id: str, choice: str) -> None:
        try:
            self._call("approval.answer", self._sess(approval_id=approval_id, choice=choice))
        except _daemon.DaemonError as exc:
            if exc.code not in (-32011, -32012):
                self.note(f"could not answer the approval: {exc.message}", muted=False)

    def answer_question(self, question_id: str, answers: list) -> None:
        try:
            self._call("question.answer", self._sess(question_id=question_id, answers=list(answers)))
        except _daemon.DaemonError as exc:
            if exc.code not in (-32011, -32012):
                self.note(f"could not answer the question: {exc.message}", muted=False)

    def idle_tick(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def permission_mode(self) -> str:
        return self.session.get("mode") or "default"

    def cycle_permission_mode(self) -> str:
        order = PERMISSION_MODES
        cur = self.permission_mode()
        target = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
        try:
            self._set(mode=target)
        except _daemon.DaemonError as exc:
            self.note(f"could not switch permission mode: {exc.message}", muted=False)
        return self.permission_mode()

    def status_action(self) -> str:
        return self._main.action

    def prompt_start_time(self) -> Optional[float]:
        if self.session.get("status") != "running":
            return None
        return _parse_time(self.session.get("turn_started"))

    def status_snapshot(self) -> dict:
        s = self.session or {}
        model = s.get("model") or "unknown"
        # Under auto, maitred names the model that actually answered.
        served = s.get("served_model") or model

        def _short(name: str) -> str:
            name = name.split("/")[-1]
            return name[:-5] if name.endswith(".gguf") else name

        short = _short(served) if served == model else f"{_short(model)} → {_short(served)}"
        if len(short) > 26:
            short = f"{short[:23]}..."
        if served != model:
            model = f"{model} → {served}"
        usage = s.get("usage") or {}
        created = _parse_time(s.get("created"))
        started = self.prompt_start_time()
        title = (s.get("title") or s.get("preview") or "").strip()
        return {
            "model_name": model,
            "model_short": short,
            "session_title": title,
            "reasoning_label": s.get("reasoning_effort") or "default",
            "context_tokens": s.get("context_tokens", 0),
            "context_length": s.get("context_length"),
            "context_percent": _percent(s.get("context_tokens", 0), s.get("context_length")),
            "session_input_tokens": usage.get("input_tokens", 0),
            "session_output_tokens": usage.get("output_tokens", 0),
            "session_api_calls": usage.get("api_calls", 0),
            "active_background_processes": s.get("background_processes", 0),
            "duration": _compact_duration(time.time() - created) if created else "",
            "prompt_elapsed": _compact_duration(time.time() - started) if started else "",
            "compactions": s.get("compactions", 0),
        }

    def palette_entries(self) -> list:
        from son_of_anton_cli.commands import COMMANDS_BY_CATEGORY, resolve_command

        out = []
        for category, commands in COMMANDS_BY_CATEGORY.items():
            for cmd, desc in commands.items():
                c = resolve_command(cmd)
                if c is not None and c.name in SUPPORTED:
                    out.append((cmd, category, desc))
        return out

    # ------------------------------------------------------------------
    # Slash commands and ! lines
    # ------------------------------------------------------------------
    def run_slash(self, command: str) -> bool:
        """Run a slash command; returns False when the app should exit."""
        from son_of_anton_cli.commands import resolve_command

        parts = command.strip().split(None, 1)
        head = parts[0] if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        cmd = resolve_command(head)
        if cmd is None:
            self.note(f"unknown command {head} (/help lists them)", muted=False)
            return True
        if cmd.name not in SUPPORTED:
            self.note(f"/{head.lstrip('/:')} is not available in soa-d yet", muted=False)
            return True
        try:
            return self._run_supported(cmd.name, arg)
        except _daemon.DaemonError as exc:
            self.note(f"/{cmd.name}: {exc.message}", muted=False)
            return True

    def _run_supported(self, name: str, arg: str) -> bool:
        if name == "quit":
            return False
        if name == "model":
            if arg:
                self._set(model=arg)
                self.note(f"model: {self.session.get('model')} (from the next turn)")
            else:
                res = self._call("model.list", self._sess())
                cur = res.get("current") or self.session.get("model", "")
                rows = []
                for m in res.get("models") or []:
                    eff = ", ".join(m.get("reasoning_efforts") or [])
                    label = f"{m['name']}  ●" if m["name"] == cur else m["name"]
                    rows.append((m["name"], label, f"reasoning: {eff}" if eff else ""))
                self.emit("pick", purpose="model", title=f"Switch model  ·  now {cur}", rows=rows,
                          default=next((i for i, r in enumerate(rows) if r[0] == cur), 0), filterable=True)
        elif name == "reasoning":
            if arg:
                self._set(reasoning_effort="" if arg.lower() == "default" else arg.lower())
                self.note(f"reasoning: {self.session.get('reasoning_effort') or 'default'} (from the next turn)")
            else:
                res = self._call("model.list", self._sess())
                cur = self.session.get("model", "")
                efforts = next((m.get("reasoning_efforts") or [] for m in res.get("models") or [] if m["name"] == cur), [])
                if not efforts:
                    self.note(f"{cur} has no configured reasoning levels")
                else:
                    now = self.session.get("reasoning_effort") or ""
                    rows = [("", "default", "the configured default")] + [(e, e, "") for e in efforts]
                    self.emit("pick", purpose="reasoning", title=f"Reasoning  ·  now {now or 'default'}",
                              rows=rows, default=0, filterable=False)
        elif name == "perm":
            if arg:
                self._set(mode=arg.lower())
                self.note(f"permission mode: {self.permission_mode()}")
            else:
                self.note(f"permission mode: {self.permission_mode()}  (default | ask | lockdown | yolo)")
        elif name in ("new", "clear"):
            self.new_session()
        elif name in ("resume", "sessions"):
            if arg:
                self.resume(arg)
            else:
                rows = []
                for s in self.list_sessions():
                    title = (s.get("title") or s.get("preview") or "").strip() or "untitled"
                    rows.append((s["id"], title[:80], f"{s.get('cwd', '')} · {str(s.get('updated', ''))[:16]}"))
                if not rows:
                    self.note("no sessions to resume")
                else:
                    self.emit("pick", purpose="resume", title="Resume session", rows=rows, default=0, filterable=True)
        elif name == "pickup":
            self._pickup(arg)
        elif name == "compact":
            self._compact(arg)
        elif name == "commit":
            self._call("command.run", self._sess(name="commit", args=arg))
        elif name == "stop":
            res = self._call("command.run", self._sess(name="stop", args=""))
            n = int(res.get("killed") or 0)
            self.note(f"killed {n} background process{'es' if n != 1 else ''}")
        elif name == "sendnow":
            if arg:
                self._call("turn.send", self._sess(text=arg, mode="now"))
            else:
                self._steer(False)
        elif name == "sendall":
            self._steer(True)
        elif name == "cancel":
            if not self._queue:
                self.note("nothing queued")
            else:
                rows = [(q["id"], _preview(q.get("text", ""))) for q in self._queue]
                self.emit("pick_many", purpose="cancel", title="Drop queued messages", rows=rows)
        elif name == "status":
            s = self.session
            u = s.get("usage") or {}
            self.note(f"session {self.session_id} · {s.get('model')} · mode {self.permission_mode()} · "
                      f"in {u.get('input_tokens', 0)} out {u.get('output_tokens', 0)} · "
                      f"{u.get('api_calls', 0)} calls")
        elif name == "help":
            from son_of_anton_cli.commands import COMMAND_REGISTRY

            self.note("commands (everything else is not available in soa-d yet):")
            for c in COMMAND_REGISTRY:
                if c.name in SUPPORTED:
                    self.note(f"  /{c.name} {c.args_hint}".rstrip() + f" — {c.description}")
            self.note("  !cmd runs a shell command here · :q quits")
        elif name == "skin":
            self._skin(arg)
        return True

    def _pickup(self, arg: str) -> None:
        """Attach a worker as driver, like /resume; with no argument, pick one."""
        workers = self.list_workers()
        if arg:
            want = arg.strip().lower()
            hit = next((w for w in workers if w["id"] == arg.strip()), None) or next(
                (w for w in workers if (w.get("title") or "").strip().lower() == want), None)
            if hit is None:
                self.note(f"no worker {arg}", muted=False)
            else:
                self.resume(hit["id"])
            return
        if not workers:
            self.note("no workers to pick up")
            return
        rows = []
        for w in workers:
            title = (w.get("title") or w.get("preview") or "").strip() or "untitled"
            rows.append((w["id"], title[:80], f"{w.get('cwd', '')} · {str(w.get('updated', ''))[:16]}"))
        self.emit("pick", purpose="pickup", title="Pick up a worker", rows=rows, default=0, filterable=True)

    def _compact(self, arg: str) -> None:
        if arg in ("--preview", "--dry-run"):
            p = self._call("command.run", self._sess(name="compact", args="--preview")).get("preview") or {}
            self.note(f"would summarize {p.get('rows', 0)} rows (through seq {p.get('through_seq', 0)}, "
                      f"about {p.get('tokens', 0):,} tokens) and keep the rest verbatim")
            return
        self.note("compacting…")
        self._call("command.run", self._sess(name="compact", args=arg), timeout=600.0)

    def _steer(self, everything: bool) -> None:
        res = self._call("queue.steer", self._sess(all=everything))
        if not (res.get("started") or res.get("steered")):
            self.note("nothing queued to send")

    def _skin(self, arg: str) -> None:
        from son_of_anton_cli.skin_engine import get_active_skin_name, list_skins, set_active_skin

        if not arg:
            cur = get_active_skin_name()
            self.note(f"skin: {cur}")
            for s in list_skins():
                self.note(f"  {'●' if s['name'] == cur else ' '} {s['name']} — {s['description']}")
            return
        name = arg.lower()
        if name not in {s["name"] for s in list_skins()}:
            self.note(f"unknown skin: {name}", muted=False)
            return
        set_active_skin(name)
        self.emit("restyle")
        self.note(f"skin: {name}")

    def picked(self, purpose: str, key: str) -> None:
        try:
            if purpose == "model":
                self._set(model=key)
                self.note(f"model: {key} (from the next turn)")
            elif purpose == "reasoning":
                self._set(reasoning_effort=key)
                self.note(f"reasoning: {key or 'default'} (from the next turn)")
            elif purpose in ("resume", "pickup"):
                self.resume(key)
        except _daemon.DaemonError as exc:
            self.note(exc.message, muted=False)

    def picked_many(self, purpose: str, keys: list) -> None:
        if purpose == "cancel" and keys:
            try:
                self._call("queue.remove", self._sess(items=list(keys)))
                self.note(f"dropped {len(keys)} queued message{'s' if len(keys) != 1 else ''}")
            except _daemon.DaemonError as exc:
                self.note(exc.message, muted=False)

    def list_sessions(self) -> list:
        return self._call("session.list", {"kinds": ["interactive"], "limit": 500}).get("sessions") or []

    def list_workers(self) -> list:
        return self._call("session.list", {"kinds": ["worker"], "limit": 500}).get("sessions") or []

    def new_session(self) -> None:
        sid = self._call("session.create", {"cwd": self.cwd})["session"]["id"]
        self._switch(sid, resumed=False)

    def resume(self, sid: str) -> None:
        try:
            self._switch(sid, resumed=True)
        except _daemon.DaemonError as exc:
            if exc.code != -32011:
                raise
            self.note(NOT_IN_SOAD.format(id=sid), muted=False)

    def _switch(self, sid: str, *, resumed: bool) -> None:
        old = self.session_id
        saved_seq = self._main.last_seq
        self._main.last_seq = 0
        gone = self.release_view()
        if gone:
            self.emit("view_close")
        try:
            self._attach(sid, resumed=resumed, clear=True)
        except _daemon.DaemonError:
            self._main.last_seq = saved_seq
            raise
        if gone:
            self.detach_session(gone)
        if old and old != sid:
            try:
                self._call("session.detach", {"session": old}, timeout=5.0)
            except _daemon.DaemonError:
                pass

    def run_bang(self, line: str) -> None:
        from son_of_anton_cli.bang_shell import USAGE_HINT, parse_bang_command, run_bang_command

        command = parse_bang_command(line)
        if not command:
            self.note(USAGE_HINT)
            return
        code = run_bang_command(command, cwd=self.cwd,
                                writer=lambda text: self.emit("ansi", text=text.rstrip("\n")))
        if code:
            self.note(f"! exited {code}")

    def _compose_in_editor(self, initial_text: str = "") -> str:
        """Compose in ``$VISUAL``/``$EDITOR`` with the terminal handed over."""
        return self.run_with_terminal(lambda: _edit(initial_text))


def _edit(initial_text: str) -> str:
    import shlex
    import subprocess
    import tempfile

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    header = (
        "#! Compose your prompt below. Lines starting with '#!' are ignored.\n"
        "#! Save and quit to send; leave empty to cancel.\n\n"
    )
    fd, path = tempfile.mkstemp(suffix=".md", prefix="son_of_anton_prompt_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(header + (initial_text or ""))
        try:
            subprocess.call([*shlex.split(editor), path])
        except Exception:
            subprocess.call(f"{editor} {shlex.quote(path)}", shell=True)
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return "\n".join(ln for ln in raw.splitlines() if not ln.startswith("#!")).strip()
