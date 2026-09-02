"""The Textual backend: ``SonOfAntonCLI`` with every output hook redirected.

The prompt_toolkit CLI (``cli.SonOfAntonCLI``) owns the whole agent lifecycle —
credentials, routing, session persistence, slash commands, interrupts, the
approval / clarify / sudo / secret prompts.  None of that is UI, so the Textual
front-end reuses it wholesale instead of re-deriving it.

``TextualBackend`` is that class with the *rendering* seams overridden:

* streamed tokens, reasoning, tool lifecycle → typed events for the app;
* ``_cprint`` / ``ChatConsole`` / ``self.console`` / bare ``print`` → the feed;
* modal prompts keep their queue-based contract (the agent thread blocks on a
  ``response_queue``) — the app watches the ``_*_state`` dicts and answers them
  through the same queues the prompt_toolkit key bindings used;
* the few stdin fallbacks (``_prompt_text_input``, curses pickers, the secret
  prompt) become queue-based too, so nothing ever reads the raw terminal while
  Textual owns it.

Everything model-facing (system prompt, toolsets, history) is untouched — the
backend is a pure view layer, so per-conversation prompt caching is preserved.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

import cli as _cli
from cli import SonOfAntonCLI

_MODAL_TIMEOUT = 120.0

# The cycle shift+tab walks. Same set and spelling as ``/perm``.
PERMISSION_MODES = ("default", "ask", "lockdown", "yolo")


class Sink:
    """Where backend events go.  The Textual app implements ``emit``."""

    def emit(self, kind: str, **payload: Any) -> None:  # pragma: no cover - protocol
        raise NotImplementedError

    def run_with_terminal(self, fn: Callable[[], Any]) -> Any:  # pragma: no cover - protocol
        """Run ``fn`` with the raw terminal handed back to it."""
        raise NotImplementedError


class _SinkStream:
    """A line-buffered file-like that turns Rich console output into events."""

    def __init__(self, backend: "TextualBackend", *, stderr: bool = False) -> None:
        self._backend = backend
        self._stderr = stderr
        self._buf = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._backend.emit("ansi", text=line, stderr=self._stderr)
        return len(text)

    def flush(self) -> None:
        if self._buf:
            self._backend.emit("ansi", text=self._buf, stderr=self._stderr)
            self._buf = ""

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise OSError("sink stream has no fileno")


def _looks_like_rule(text: str) -> bool:
    """True for the ``────`` separator the CLI prints around each turn."""
    stripped = text.strip()
    return bool(stripped) and set(stripped) <= {"─", "━", "-"}


class TextualBackend(SonOfAntonCLI):
    """``SonOfAntonCLI`` whose output seams feed a Textual app."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._sink: Optional[Sink] = None
        # Startup work (credential checks, the tirith warning) emits before the
        # app has mounted. Hold those instead of dropping them.
        self._pending_events: list = []
        self._tui_feed_width: int = 0
        self._tui_text_prompt_state: Optional[dict] = None
        self._tui_picker_state: Optional[dict] = None
        self._tui_prepared = False
        self._perm_label: Optional[str] = None
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------
    def emit(self, kind: str, **payload: Any) -> None:
        sink = self._sink
        if sink is None:
            # Only buffer content; repaint requests are meaningless once late.
            if kind in ("ansi", "rich") and len(self._pending_events) < 200:
                self._pending_events.append((kind, payload))
            return
        try:
            sink.emit(kind, **payload)
        except Exception:
            pass

    def attach(self, sink: Sink, feed_width: int = 0) -> None:
        """Bind the app, replay anything said before it mounted, and reroute output."""
        self._sink = sink
        if feed_width:
            self._tui_feed_width = feed_width
        self._patch_output_funnels()
        pending, self._pending_events = self._pending_events, []
        for kind, payload in pending:
            self.emit(kind, **payload)

    def detach(self) -> None:
        self._sink = None
        _cli._CPRINT_ROUTED = False
        try:
            from son_of_anton_constants import set_frontend_active

            set_frontend_active(False)
        except Exception:
            pass

    def _patch_output_funnels(self) -> None:
        backend = self

        def _tui_cprint(text: str, strip: bool = True) -> None:
            if strip:
                text = _cli.strip_decorative_glyphs(text or "")
            try:
                _cli._record_output_history(text)
            except Exception:
                pass
            backend.emit("ansi", text=text)

        _cli._cprint = _tui_cprint  # type: ignore[assignment]
        _cli._CPRINT_ROUTED = True
        # Tell the low-level layers (approval, lazy installs) that a
        # full-screen app owns stdin, so nothing tries to prompt on the tty.
        from son_of_anton_constants import set_frontend_active

        set_frontend_active(True)

        try:
            from son_of_anton_cli import banner as _banner

            _banner.cprint = lambda text: _tui_cprint(text)  # type: ignore[assignment]
        except Exception:
            pass
        try:
            from son_of_anton_cli import callbacks as _callbacks

            _callbacks.cprint = lambda text: _tui_cprint(text)  # type: ignore[assignment]
        except Exception:
            pass

        class _TuiChatConsole:
            """Drop-in for ``cli.ChatConsole``: renderables go straight to the feed."""

            def __init__(self) -> None:
                pass

            def print(self, *objects: Any, **kwargs: Any) -> None:
                if not objects:
                    backend.emit("ansi", text="")
                    return
                from rich.text import Text

                for obj in objects:
                    if isinstance(obj, str):
                        if _looks_like_rule(obj.split("]")[-1] if obj.startswith("[") else obj):
                            continue
                        try:
                            renderable: Any = Text.from_markup(obj)
                        except Exception:
                            renderable = Text(obj)
                        backend.emit("rich", renderable=renderable)
                    else:
                        backend.emit("rich", renderable=obj)

        _cli.ChatConsole = _TuiChatConsole  # type: ignore[assignment,misc]

        from rich.console import Console

        self.console = Console(
            file=_SinkStream(self),
            force_terminal=True,
            color_system="standard",
            highlight=False,
            width=max(40, self._tui_feed_width or 100),
        )

    def run_with_terminal(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` while the front-end lets go of the terminal.

        Anything that draws its own screen — an editor, a pager — cannot share
        the tty with a running full-screen app: both write to it and both read
        stdin, so the child's output and its escape-sequence replies land in the
        app as stray characters. The app suspends around ``fn`` instead.
        """
        runner = getattr(self._sink, "run_with_terminal", None)
        if runner is None:
            return fn()
        return runner(fn)

    def _compose_in_editor(self, initial_text: str = "") -> str:
        """Compose in ``$EDITOR`` with the terminal handed over first.

        The inherited implementation spawns the editor on this process's tty;
        doing that under a running front-end is what garbles the screen.
        """
        return self.run_with_terminal(lambda: super(TextualBackend, self)._compose_in_editor(initial_text))

    def _apply_tui_skin_style(self) -> bool:
        """Re-resolve the app's palette after ``/skin`` changes the accents."""
        if self._sink is None:
            return False
        self.emit("restyle")
        return True

    def set_feed_width(self, width: int) -> None:
        """Keep the Rich console and box helpers in step with the feed column."""
        self._tui_feed_width = max(20, int(width or 0))
        try:
            self.console.width = max(40, self._tui_feed_width)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # The run() prologue, minus prompt_toolkit
    # ------------------------------------------------------------------
    def prepare_interactive_state(self) -> bool:
        """Do what ``run()`` does before it builds the prompt_toolkit app."""
        if self._tui_prepared:
            return True
        if not self._claim_active_session("cli"):
            return False
        try:
            _cli._detect_light_mode()
        except Exception:
            pass

        self._agent_running = False
        self._pending_input = queue.Queue()
        self._interrupt_queue = queue.Queue()
        self._last_turn_interrupted = False
        self._should_exit = False
        self._last_ctrl_c_time = 0

        try:
            from son_of_anton_cli.plugins import get_plugin_manager

            get_plugin_manager()._cli_ref = self
        except Exception:
            pass

        try:
            from son_of_anton_cli.config import get_config_path as _get_config_path

            _cfg_path = _get_config_path()
            self._config_mtime = _cfg_path.stat().st_mtime if _cfg_path.exists() else 0.0
        except Exception:
            self._config_mtime = 0.0
        self._config_mcp_servers = self.config.get("mcp_servers") or {}
        self._last_config_check = 0.0

        self._clarify_state = None
        self._clarify_freetext = False
        self._clarify_deadline = 0
        self._clarify_multi_base = None
        self._clarify_prefill = ""
        self._sudo_state = None
        self._sudo_deadline = 0
        self._modal_input_snapshot = None
        self._approval_state = None
        self._approval_deadline = 0
        self._approval_lock = threading.Lock()
        self._slash_confirm_state = None
        self._slash_confirm_deadline = 0
        self._command_running = False
        self._command_blocks_input = False
        self._command_status = ""
        self._secret_state = None
        self._secret_deadline = 0
        self._attached_images = []
        self._image_counter = 0
        self._model_picker_state = getattr(self, "_model_picker_state", None)
        self._command_palette_state = None

        import os

        # Startup prewarms all fetch or import in the background and write under
        # SON_OF_ANTON_HOME, so they answer to the same defer flag the classic
        # run() honours — including the /model picker cache, which fetches every
        # authed provider's model list.
        if os.environ.get("SON_OF_ANTON_DEFER_AGENT_STARTUP") != "1":
            self._install_tool_callbacks()
            self._ensure_tirith_security()
            try:
                from son_of_anton_cli.model_switch import prewarm_picker_cache_async

                prewarm_picker_cache_async()
            except Exception:
                pass

            def _prewarm_agent_runtime() -> None:
                try:
                    import run_agent  # noqa: F401
                    import openai  # noqa: F401
                except Exception:
                    pass

            threading.Thread(
                target=_prewarm_agent_runtime, name="agent-runtime-prewarm", daemon=True
            ).start()
        self._tui_prepared = True
        return True

    def finish(self) -> None:
        """Shut the session down cleanly after the app exits.

        This is the only exit path now, so it carries everything the old
        prompt_toolkit ``run()`` did in its ``finally``: stop the agent, drop
        callbacks, flush the in-memory transcript, close/prune the session row,
        fire the interrupted-session hook, then the shared cleanup and the
        resume summary. Every step is independently guarded — a failure late in
        teardown must not skip the steps after it.
        """
        self.detach()
        self._should_exit = True
        try:
            print("Shutting down… (finalizing session)", flush=True)
        except Exception:
            pass

        agent = getattr(self, "agent", None)
        was_running = bool(agent and getattr(self, "_agent_running", False))

        # Stop the agent's daemon thread making more API calls on the way out.
        if was_running:
            try:
                _cli.request_hard_interrupt(agent)
            except Exception:
                pass

        # Drop callbacks so a reused thread can't reach a disposed instance.
        for setter in (
            "set_sudo_password_callback",
            "set_approval_callback",
            "set_secret_capture_callback",
        ):
            try:
                getattr(_cli, setter)(None)
            except Exception:
                pass

        # A terminal close can reap the agent thread before run_conversation()
        # persists the turn, so flush before marking the session closed.
        try:
            self._persist_active_session_before_close()
        except Exception:
            pass

        session_db = getattr(self, "_session_db", None)
        if session_db and agent:
            try:
                session_db.end_session(agent.session_id, "cli_close")
            except (Exception, KeyboardInterrupt):
                pass
            if getattr(self, "_delete_session_on_exit", False):
                # /exit --delete: drop the transcripts and the SQLite history.
                try:
                    from son_of_anton_constants import get_son_of_anton_home

                    deleted = session_db.delete_session(
                        agent.session_id, sessions_dir=get_son_of_anton_home() / "sessions"
                    )
                    print(
                        f"  {'✓' if deleted else '✗'} Session {agent.session_id}"
                        f"{' deleted' if deleted else ' not found for deletion'}"
                    )
                except (Exception, KeyboardInterrupt):
                    pass
            else:
                # Quit-immediately sessions never gained content; keep /resume
                # and `sessions list` clean by dropping the empty row.
                try:
                    self._discard_session_if_empty(agent.session_id)
                except (Exception, KeyboardInterrupt):
                    pass

        # on_session_end already fired per-turn on normal completion, so this is
        # only the safety net for quitting mid-turn.
        if was_running:
            try:
                from son_of_anton_cli.lifecycle import invoke_hook

                invoke_hook(
                    "on_session_end",
                    session_id=agent.session_id,
                    completed=False,
                    interrupted=True,
                    model=getattr(agent, "model", None),
                    platform=getattr(agent, "platform", None) or "cli",
                    reason="shutdown",
                )
            except Exception:
                pass

        try:
            _cli._run_cleanup()
        except Exception:
            pass
        try:
            self._print_exit_summary(clear_screen=False)
        except Exception:
            pass
        try:
            self._release_active_session()
        except Exception:
            pass

        # /update parks a relaunch here so the exec happens after the app has
        # exited and the terminal is restored, never from a worker thread.
        if getattr(self, "_pending_relaunch", None):
            try:
                from son_of_anton_cli.relaunch import relaunch

                relaunch(self._pending_relaunch, preserve_inherited=False)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Turn helpers used by the app
    # ------------------------------------------------------------------
    def run_turn(self, message: Any, images: Optional[list] = None) -> Optional[str]:
        """Run one chat turn on the calling (worker) thread."""
        self._agent_running = True
        self._interactive_turn = True
        try:
            self._turn_summary_begin()
        except Exception:
            pass
        self.emit("refresh")
        try:
            return self.chat(message, images=images)
        finally:
            self._agent_running = False
            self._interactive_turn = False
            self._spinner_text = ""
            self._tool_start_time = 0.0
            self._last_scrollback_tool = ""
            try:
                self._pending_tool_info.clear()
            except Exception:
                pass
            self._post_turn_hooks()
            self.emit("turn_end")

    def _post_turn_hooks(self) -> None:
        """The end-of-turn work the old input loop did after ``chat()``.

        Each is independent and non-fatal: the accounting footer, re-queueing
        messages that raced the interrupt path, the standing-goal judge, the
        ``/loop`` tick evaluation, and background process notifications that
        arrived while the agent held the floor.
        """
        for hook in (
            self._turn_summary_emit,
            self._drain_interrupt_queue_to_pending_input,
            self._maybe_continue_goal_after_turn,
            self._maybe_complete_loop_tick_after_turn,
            lambda: self._drain_process_notifications("cli-post-turn"),
        ):
            try:
                hook()
            except Exception:
                logging.debug("post-turn hook failed", exc_info=True)

    def idle_tick(self) -> None:
        """Periodic work the old input loop did while waiting on input.

        Watches config for mcp_servers changes, surfaces finished background
        processes, and fires a due ``/loop`` wakeup. Skipped while a turn holds
        the floor, matching the loop this replaces.
        """
        if getattr(self, "_agent_running", False):
            return
        for hook in (
            self._check_config_mcp_changes,
            lambda: self._drain_process_notifications("cli-idle"),
            self._maybe_fire_loop_tick,
        ):
            try:
                hook()
            except Exception:
                logging.debug("idle hook failed", exc_info=True)

    def consume_resume_selection(self, text: str) -> bool:
        """True when ``text`` picked a session from a bare ``/resume`` listing."""
        if not getattr(self, "_pending_resume_sessions", None):
            return False
        try:
            return bool(self._consume_pending_resume_selection(text))
        except Exception:
            return False

    def new_session(self, *args: Any, **kwargs: Any):
        """Start a fresh session, dropping this session's permission override."""
        previous = self.session_id
        result = super().new_session(*args, **kwargs)
        if previous and previous != self.session_id:
            try:
                from tools.approval import set_session_permission_mode

                set_session_permission_mode(previous, None)
            except Exception:
                pass
        self._perm_label = None
        return result

    def run_slash(self, command: str) -> bool:
        """Run a slash command; returns False when the app should exit."""
        self._command_running = True
        self.emit("refresh")
        if command.lstrip("/:").split()[:1] == ["perm"]:
            self._perm_label = None
        try:
            return self.process_command(command)
        except KeyboardInterrupt:
            self.emit("ansi", text="Command interrupted.")
            return True
        finally:
            self._command_running = False
            self._perm_label = None if command.lstrip("/:").startswith("perm") else self._perm_label
            self.emit("refresh")

    def interrupt_turn(self) -> bool:
        """Ctrl+C while the agent runs: hard-interrupt the current turn."""
        agent = getattr(self, "agent", None)
        if not (self._agent_running and agent):
            return False
        try:
            _cli.request_hard_interrupt(agent)
        except Exception:
            try:
                agent.interrupt()
            except Exception:
                return False
        try:
            self._clear_active_overlays_for_interrupt()
        except Exception:
            pass
        return True

    def cancel_pending_prompts(self) -> None:
        """Unblock every prompt still waiting on a queue (app exit / interrupt).

        Worker threads block on ``response_queue.get()``; if the app goes away
        without answering, a non-daemon worker would keep the process alive.
        """
        try:
            self._clear_active_overlays_for_interrupt()
        except Exception:
            pass
        for attr, answer in (
            ("_slash_confirm_state", "cancel"),
            ("_tui_text_prompt_state", None),
            ("_tui_picker_state", None),
        ):
            state = getattr(self, attr, None)
            if state:
                try:
                    state["response_queue"].put(answer)
                except Exception:
                    pass
                setattr(self, attr, None)
        try:
            self._close_model_picker()
        except Exception:
            pass

    def drain_pending_input(self) -> list:
        """Messages re-queued by an interrupt or a leftover ``/steer``."""
        items = []
        q = getattr(self, "_pending_input", None)
        if q is None:
            return items
        while True:
            try:
                items.append(q.get_nowait())
            except queue.Empty:
                break
        return items

    # ------------------------------------------------------------------
    # Permission mode
    # ------------------------------------------------------------------
    def permission_mode(self) -> str:
        """This session's mode: default | ask | lockdown | yolo.

        A session override (set by the front-end's cycle) wins; otherwise this
        reports what config says, which is what ``/perm`` writes. Read through
        the approval guard's own resolvers rather than the CLI's cached config,
        because ``/perm`` persists to disk without refreshing that copy.
        Cached between changes so a once-a-second status repaint costs nothing.
        """
        if self._perm_label is not None:
            return self._perm_label
        label = "default"
        try:
            from tools.approval import (
                _get_approval_config,
                _normalize_approval_mode,
                get_session_permission_mode,
            )

            override = get_session_permission_mode(self.session_id or "")
            if override is not None:
                label = override
            else:
                mode = _normalize_approval_mode(_get_approval_config().get("mode", "manual"))
                lockdown = False
                try:
                    from son_of_anton_cli.config import load_config_readonly

                    lockdown = bool((load_config_readonly() or {}).get("security", {}).get("lockdown", False))
                except Exception:
                    pass
                label = "lockdown" if lockdown else {"off": "yolo", "manual": "ask"}.get(mode, "default")
        except Exception:
            pass
        self._perm_label = label
        return label

    def cycle_permission_mode(self) -> str:
        """Advance this session's permission mode and report the new one.

        Session-scoped on purpose: the cycle never writes config, so it cannot
        outlive the session that chose it, and a new session (``/new``, or a new
        process) starts from the configured default again. ``/perm`` remains the
        way to change the persistent setting.
        """
        order = PERMISSION_MODES
        try:
            index = order.index(self.permission_mode())
        except ValueError:
            index = 0
        target = order[(index + 1) % len(order)]
        try:
            from tools.approval import set_session_permission_mode

            set_session_permission_mode(self.session_id or "", target)
        except Exception as exc:
            self.emit("ansi", text=f"  ✗ Could not switch permission mode: {exc}")
            return self.permission_mode()
        self._perm_label = target
        detail = {
            "default": "smart approvals",
            "ask": "every dangerous command asks",
            "lockdown": "every command asks",
            "yolo": "approvals skipped",
        }[target]
        self.emit("ansi", text=f"  Permission mode: {target} — {detail} (this session)")
        self.emit("refresh")
        return target

    def status_snapshot(self) -> dict:
        try:
            return self._get_status_bar_snapshot()
        except Exception:
            return {}

    def palette_entries(self) -> list:
        try:
            return self._build_command_palette_entries()
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Streaming seams
    # ------------------------------------------------------------------
    def _emit_stream_text(self, text: str) -> None:
        if not text:
            return
        self._close_reasoning_box()
        if not self._stream_box_opened:
            text = text.lstrip("\n")
            if not text:
                return
            self._stream_box_opened = True
            self.emit("assistant_start")
        self.emit("assistant_delta", text=text)

    def _flush_stream(self) -> None:
        if getattr(self, "_in_reasoning_block", False) and getattr(self, "_stream_prefilt", ""):
            # A tag mentioned in prose, never closed: recover the text.
            self._in_reasoning_block = False
            self._emit_stream_text(self._stream_prefilt)
            self._stream_prefilt = ""
        self._close_reasoning_box()
        if self._stream_buf:
            self.emit("assistant_delta", text=self._stream_buf)
            self._stream_buf = ""
        if self._stream_box_opened:
            self.emit("assistant_end")

    def _stream_reasoning_delta(self, text: str) -> None:
        if not text:
            return
        self._reasoning_shown_this_turn = True
        if getattr(self, "_stream_box_opened", False):
            return
        if not getattr(self, "_reasoning_box_opened", False):
            self._reasoning_box_opened = True
            self.emit("reasoning_start")
        self.emit("reasoning_delta", text=text)

    def _close_reasoning_box(self) -> None:
        if getattr(self, "_reasoning_box_opened", False):
            self._reasoning_box_opened = False
            self.emit("reasoning_end")
        deferred = getattr(self, "_deferred_content", "")
        if deferred:
            self._deferred_content = ""
            self._emit_stream_text(deferred)

    def _emit_reasoning_preview(self, reasoning_text: str) -> None:
        """Verbose (non-streaming) reasoning preview → the same reasoning block."""
        preview = (reasoning_text or "").strip()
        if not preview:
            return
        if not getattr(self, "_reasoning_box_opened", False):
            self._reasoning_box_opened = True
            self.emit("reasoning_start")
        self.emit("reasoning_delta", text=preview + "\n")

    # ------------------------------------------------------------------
    # Tool lifecycle seams
    # ------------------------------------------------------------------
    def _on_tool_gen_start(self, tool_name: str) -> None:
        if getattr(self, "_stream_box_opened", False):
            self._flush_stream()
            self._stream_box_opened = False
        self._close_reasoning_box()
        self.emit("tool_gen", name=tool_name)

    def _on_tool_progress(
        self,
        event_type: str,
        function_name: str = None,
        preview: str = None,
        function_args: dict = None,
        **kwargs: Any,
    ) -> None:
        if event_type == "tool.started":
            if function_name and not function_name.startswith("_"):
                label = preview or function_name
                try:
                    from agent.display import get_tool_preview_max_len

                    _pl = get_tool_preview_max_len()
                    if _pl > 0 and len(label) > _pl:
                        label = label[: _pl - 3] + "..."
                except Exception:
                    pass
                self._spinner_text = label
                self._tool_start_time = time.monotonic()
                self._pending_tool_info.setdefault(function_name, []).append(
                    function_args if function_args is not None else {}
                )
                self.emit(
                    "tool_start",
                    name=function_name,
                    label=label,
                    hidden=self.tool_progress_mode == "off",
                )
            return
        if event_type != "tool.completed":
            return
        self._tool_start_time = 0.0
        try:
            self._turn_summary_record(
                function_name, kwargs.get("result"), kwargs.get("is_error", False)
            )
        except Exception:
            pass
        duration = kwargs.get("duration", 0.0) or 0.0
        stored = self._pending_tool_info.get(function_name)
        stored_args = stored.pop(0) if stored else {}
        if stored is not None and not stored:
            del self._pending_tool_info[function_name]
        # The app draws its own icon column, so send the phrase rather than the
        # REPL's pre-decorated line ("Running scripts/x.sh", "Reading foo.py").
        # ``line`` stays as the fallback for anything build_tool_label can't phrase.
        label = ""
        line = ""
        if function_name and self.tool_progress_mode != "off":
            try:
                from agent.display import build_tool_label

                label = build_tool_label(function_name, stored_args or {}) or ""
            except Exception:
                label = ""
            if not label:
                try:
                    from agent.display import get_cute_tool_message

                    line = get_cute_tool_message(
                        function_name, stored_args, duration, result=kwargs.get("result")
                    )
                except Exception:
                    line = ""
                label = label or function_name
        self.emit(
            "tool_done",
            name=function_name,
            label=label,
            line=line,
            duration=duration,
            is_error=bool(kwargs.get("is_error", False)),
            hidden=self.tool_progress_mode == "off",
        )

    def _on_thinking(self, text: str) -> None:
        if not text:
            try:
                self._flush_reasoning_preview(force=True)
            except Exception:
                pass
        self._spinner_text = text or ""
        self._tool_start_time = 0.0
        self.emit("refresh")

    # ------------------------------------------------------------------
    # Repaint seams (prompt_toolkit invalidate → app refresh)
    # ------------------------------------------------------------------
    def _paint_now(self) -> None:
        self.emit("refresh")

    def _invalidate(self, min_interval: float = 0.25) -> None:
        self.emit("refresh")

    def _force_full_redraw(self) -> None:
        self.emit("refresh")

    def _capture_modal_input_snapshot(self) -> None:
        return None

    def _restore_modal_input_snapshot(self) -> None:
        return None

    def _get_tui_terminal_width(self, default: tuple = (80, 24)) -> int:  # type: ignore[override]
        if self._tui_feed_width:
            return self._tui_feed_width
        import shutil

        return shutil.get_terminal_size(default).columns

    def _scrollback_box_width(self, width: Optional[int] = None) -> int:  # type: ignore[override]
        return SonOfAntonCLI._scrollback_box_width(width or self._tui_feed_width or None)

    def _use_minimal_tui_chrome(self, width: Optional[int] = None) -> bool:
        return False

    def _check_termios_drift(self) -> None:
        return None

    def _recover_terminal_input_modes(self, reason: str = "") -> None:
        return None

    # ------------------------------------------------------------------
    # Prompts that fell back to stdin: make them queue-based
    # ------------------------------------------------------------------
    def _wait_for_queue(
        self,
        response_queue: "queue.Queue[Any]",
        timeout: Optional[float],
        *,
        still_open: Callable[[], bool],
    ) -> tuple[bool, Any]:
        """Block the calling (worker) thread until the app answers or timeout."""
        deadline = None if not timeout or timeout <= 0 else time.monotonic() + timeout
        self.emit("refresh")
        while True:
            try:
                return True, response_queue.get(timeout=1)
            except queue.Empty:
                if not still_open():
                    return False, None
                if deadline is not None and time.monotonic() >= deadline:
                    return False, None
                self.emit("refresh")

    def _prompt_text_input_modal(
        self,
        *,
        title: str,
        detail: str,
        choices: list,
        timeout: float = _MODAL_TIMEOUT,
    ) -> Optional[str]:
        if not choices:
            return None
        response_queue: "queue.Queue[Any]" = queue.Queue()
        self._slash_confirm_state = {
            "title": title,
            "detail": detail,
            "choices": choices,
            "selected": 0,
            "response_queue": response_queue,
        }
        self._slash_confirm_deadline = time.monotonic() + timeout
        try:
            ok, result = self._wait_for_queue(
                response_queue, timeout, still_open=lambda: self._slash_confirm_state is not None
            )
        finally:
            self._slash_confirm_state = None
            self._slash_confirm_deadline = 0
            self.emit("refresh")
        return result if ok else None

    def _prompt_text_input(self, prompt_text: str) -> Optional[str]:
        return self.prompt_text(prompt_text)

    def prompt_text(
        self, prompt_text: str, *, password: bool = False, timeout: float = _MODAL_TIMEOUT
    ) -> Optional[str]:
        """Ask the user for one line of text through a Textual modal."""
        response_queue: "queue.Queue[Any]" = queue.Queue()
        self._tui_text_prompt_state = {
            "prompt": prompt_text,
            "password": password,
            "response_queue": response_queue,
        }
        try:
            ok, result = self._wait_for_queue(
                response_queue, timeout, still_open=lambda: self._tui_text_prompt_state is not None
            )
        finally:
            self._tui_text_prompt_state = None
            self.emit("refresh")
        if not ok or result is None:
            return None
        result = str(result).strip()
        return result or None

    def _run_curses_picker(
        self, title: str, items: list, default_index: int = 0
    ) -> Optional[int]:
        response_queue: "queue.Queue[Any]" = queue.Queue()
        self._tui_picker_state = {
            "title": title,
            "items": list(items),
            "default": default_index,
            "response_queue": response_queue,
        }
        try:
            ok, result = self._wait_for_queue(
                response_queue, _MODAL_TIMEOUT, still_open=lambda: self._tui_picker_state is not None
            )
        finally:
            self._tui_picker_state = None
            self.emit("refresh")
        if not ok or result is None:
            return None
        try:
            return int(result)
        except (TypeError, ValueError):
            return None

    def _secret_capture_callback(self, var_name: str, prompt: str, metadata=None) -> dict:
        from son_of_anton_cli.config import save_env_value_secure
        from son_of_anton_constants import display_son_of_anton_home

        response_queue: "queue.Queue[Any]" = queue.Queue()
        self._secret_state = {
            "var_name": var_name,
            "prompt": prompt,
            "metadata": metadata or {},
            "response_queue": response_queue,
        }
        self._secret_deadline = time.monotonic() + _MODAL_TIMEOUT
        try:
            ok, value = self._wait_for_queue(
                response_queue, _MODAL_TIMEOUT, still_open=lambda: self._secret_state is not None
            )
        finally:
            self._secret_state = None
            self._secret_deadline = 0
            self.emit("refresh")
        if not ok:
            self.emit("ansi", text="  ⏱ Timeout — secret capture cancelled")
            return {
                "success": True,
                "reason": "timeout",
                "stored_as": var_name,
                "validated": False,
                "skipped": True,
                "message": "Secret setup timed out and was skipped.",
            }
        if not value:
            self.emit("ansi", text="  ⏭ Secret entry skipped")
            return {
                "success": True,
                "reason": "cancelled",
                "stored_as": var_name,
                "validated": False,
                "skipped": True,
                "message": "Secret setup was skipped.",
            }
        stored = save_env_value_secure(var_name, value)
        self.emit("ansi", text=f"  ✓ Stored secret in {display_son_of_anton_home()}/.env as {var_name}")
        return {
            **stored,
            "skipped": False,
            "message": "Secret stored securely. The secret value was not exposed to the model.",
        }
