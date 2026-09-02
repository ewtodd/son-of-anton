"""Slash-command handlers for the interactive CLI (god-file decomposition Phase 4).

This module hosts the ``_handle_*_command`` slash-command handlers lifted out of
``cli.py``'s ``SonOfAntonCLI`` class. ``SonOfAntonCLI`` inherits ``CLICommandsMixin`` so
every ``self.<handler>`` call resolves unchanged via the MRO — behavior-neutral.

Import discipline (mirrors gateway/slash_commands.py, PR #41886):
  * Neutral, non-cyclic deps are imported at module top-level below.
  * cli.py-internal symbols (the ``_cprint``/``_ACCENT``/``save_config_value``…
    module-level helpers and constants) are imported LAZILY inside each handler
    via ``from cli import ...`` — that resolves at call time when ``cli`` is fully
    loaded, so the mixin module never imports ``cli`` at top level (no cycle).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime

from rich import box as rich_box
from rich.markup import escape as _escape
from rich.panel import Panel

from son_of_anton_constants import display_son_of_anton_home
from agent.turn_context import extract_api_content_sidecar


class CLICommandsMixin:
    """Mixin holding the interactive-CLI slash-command handlers.

    All methods use only ``self`` state plus the imports above and per-method
    lazy ``from cli import ...`` lines, so they compose cleanly onto
    ``SonOfAntonCLI`` via the MRO.
    """

    def _handle_rollback_command(self, command: str):
        """Handle /rollback — list, diff, or restore filesystem checkpoints.

        Syntax:
            /rollback                 — list checkpoints
            /rollback <N>             — restore checkpoint N, preserving user
                                        hand-edits (also undoes last chat turn)
            /rollback <N> --all       — classic full restore (may overwrite
                                        files you edited after Son of Anton did)
            /rollback diff <N>        — preview changes since checkpoint N
            /rollback <N> <file>      — restore a single file from checkpoint N
        """
        from tools.checkpoint_manager import format_checkpoint_list

        if not hasattr(self, 'agent') or not self.agent:
            print("  No active agent session.")
            return

        mgr = self.agent._checkpoint_mgr
        if not mgr.enabled:
            print("  Checkpoints are not enabled.")
            print("  Enable with: son-of-anton --checkpoints")
            print("  Or in config.yaml: checkpoints: { enabled: true }")
            return

        cwd = os.getenv("TERMINAL_CWD", os.getcwd())
        parts = command.split()
        args = parts[1:] if len(parts) > 1 else []

        # --all / --force: classic full restore, overwriting user edits too.
        restore_all = False
        filtered = []
        for a in args:
            if a.lower() in ("--all", "--force"):
                restore_all = True
            else:
                filtered.append(a)
        args = filtered

        if not args:
            # List checkpoints — fall back to the cross-project view when the
            # current directory has none (#10505, reapply of PR #10633 by
            # @nightq). The Aug 2026 QA sweep hit this live: writes landed
            # checkpoints under the session cwd (/tmp/qa-repo) while bare
            # /rollback searched only TERMINAL_CWD's project and reported
            # "No checkpoints found" despite fresh checkpoints existing.
            checkpoints = mgr.list_checkpoints(cwd)
            if not checkpoints:
                all_checkpoints = mgr.list_all_checkpoints()
                if all_checkpoints:
                    print(f"  No checkpoints for {cwd} — showing all directories.")
                    print(format_checkpoint_list(all_checkpoints, "all directories"))
                    return
            print(format_checkpoint_list(checkpoints, cwd))
            return

        # Handle /rollback diff <N>
        if args[0].lower() == "diff":
            if len(args) < 2:
                print("  Usage: /rollback diff <N>")
                return
            checkpoints = mgr.list_checkpoints(cwd)
            if not checkpoints:
                print(f"  No checkpoints found for {cwd}")
                return
            target_hash = self._resolve_checkpoint_ref(args[1], checkpoints)
            if not target_hash:
                return
            result = mgr.diff(cwd, target_hash)
            if result["success"]:
                stat = result.get("stat", "")
                diff = result.get("diff", "")
                if not stat and not diff:
                    print("  No changes since this checkpoint.")
                else:
                    if stat:
                        print(f"\n{stat}")
                    if diff:
                        # Limit diff output to avoid terminal flood
                        diff_lines = diff.splitlines()
                        if len(diff_lines) > 80:
                            print("\n".join(diff_lines[:80]))
                            print(f"\n  ... ({len(diff_lines) - 80} more lines, showing first 80)")
                        else:
                            print(f"\n{diff}")
            else:
                print(f"  ❌ {result['error']}")
            return

        # Resolve checkpoint reference (number or hash)
        checkpoints = mgr.list_checkpoints(cwd)
        if not checkpoints:
            print(f"  No checkpoints found for {cwd}")
            return

        target_hash = self._resolve_checkpoint_ref(args[0], checkpoints)
        if not target_hash:
            return

        # Check for file-level restore: /rollback <N> <file>
        file_path = args[1] if len(args) > 1 else None

        result = mgr.restore(
            cwd, target_hash, file_path=file_path,
            safe=not restore_all and not file_path,
        )
        if result["success"]:
            if file_path:
                print(f"  ✅ Restored {file_path} from checkpoint {result['restored_to']}: {result['reason']}")
            else:
                print(f"  ✅ Restored to checkpoint {result['restored_to']}: {result['reason']}")
            skipped = result.get("skipped_user_edits") or []
            if skipped:
                shown = ", ".join(skipped[:5])
                more = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
                print(f"  ↷ Kept your hand-edits: {shown}{more}")
                print("  Use /rollback <N> --all to restore those too.")
            print("  A pre-rollback snapshot was saved automatically.")

            # Also undo the last conversation turn so the agent's context
            # matches the restored filesystem state
            if self.conversation_history:
                self.undo_last(prefill=False)
                print("  Chat turn undone to match restored file state.")
        else:
            print(f"  ❌ {result['error']}")

    def _handle_diff_command(self, command: str):
        """Handle /diff — show git changes in the working directory.

        Syntax:
            /diff                  — unstaged changes + untracked files
            /diff staged           — staged changes (git diff --cached)
            /diff all              — staged + unstaged + untracked (vs HEAD)
            /diff session          — everything Son of Anton changed (checkpoint baseline)
            /diff [mode] --stat    — summary only (changed files + counts)
            /diff [mode] <path...> — restrict to specific paths
        """
        import shlex

        try:
            parts = shlex.split(command)[1:]  # preserves quoted paths
        except ValueError:
            parts = command.split()[1:]

        stat_only = False
        mode = "working"
        paths: list[str] = []
        for arg in parts:
            low = arg.lower()
            if low in ("--stat", "stat"):
                stat_only = True
            elif low in ("staged", "--staged", "cached", "--cached"):
                mode = "staged"
            elif low in ("all", "--all", "head"):
                mode = "all"
            elif low == "session":
                mode = "session"
            else:
                paths.append(arg)

        cwd = os.getenv("TERMINAL_CWD", os.getcwd())

        if mode == "session":
            self._print_session_diff(cwd, stat_only)
            return

        from tools.working_diff import collect_working_diff

        result = collect_working_diff(cwd, mode=mode, paths=paths or None)
        if not result.get("success"):
            print(f"  {result.get('error', 'Could not generate diff')}")
            return

        stat = result.get("stat", "")
        diff = result.get("diff", "")
        untracked = result.get("untracked", [])
        if result.get("empty") or (not stat and not diff and not untracked):
            print("  No changes.")
            return

        label = {"working": "Unstaged", "staged": "Staged", "all": "All (vs HEAD)"}[mode]
        if stat:
            print(f"\n  {label}:")
            self._print_diff_text(stat)
        if untracked and mode in ("working", "all"):
            print("\n  Untracked:")
            for rel in untracked[:20]:
                print(f"    + {rel}")
            if len(untracked) > 20:
                print(f"    ... and {len(untracked) - 20} more")
        if stat_only or not diff:
            return

        diff_lines = diff.splitlines()
        print("")
        if len(diff_lines) > 400:
            self._print_diff_text("\n".join(diff_lines[:400]))
            print(
                f"\n  ... ({len(diff_lines) - 400} more lines — "
                "run /diff --stat for a summary)"
            )
        else:
            self._print_diff_text(diff)

    def _print_session_diff(self, cwd: str, stat_only: bool):
        """Print the cumulative checkpoint-baseline diff (/diff session)."""
        if not hasattr(self, 'agent') or not self.agent:
            print("  No active agent session.")
            return

        mgr = self.agent._checkpoint_mgr
        if not mgr.enabled:
            print("  Checkpoints are not enabled, so there's no session baseline.")
            print("  Enable with: son-of-anton --checkpoints")
            print("  Or in config.yaml: checkpoints: { enabled: true }")
            print("  (Plain /diff still works — it uses git directly.)")
            return

        result = mgr.session_diff(cwd)
        if not result.get("success"):
            print(f"  {result.get('error', 'Could not generate diff')}")
            return

        stat = result.get("stat", "")
        diff = result.get("diff", "")
        if result.get("empty") or (not stat and not diff):
            print("  No changes — Son of Anton hasn't edited any files here yet.")
            return

        if stat:
            self._print_diff_text(f"\n{stat}")
        if stat_only or not diff:
            return
        diff_lines = diff.splitlines()
        print("")
        if len(diff_lines) > 400:
            self._print_diff_text("\n".join(diff_lines[:400]))
            print(
                f"\n  ... ({len(diff_lines) - 400} more lines — "
                "run /diff session --stat for a summary)"
            )
        else:
            self._print_diff_text(diff)

    def _print_diff_text(self, text: str) -> None:
        """Render diff/stat text with color when a rich console is present.

        Falls back to plain print when the console isn't available (e.g. unit
        tests instantiating the mixin standalone).
        """
        console = getattr(self, "console", None)
        if console is not None:
            try:
                from cli import _rich_text_from_ansi
                console.print(_rich_text_from_ansi(text))
                return
            except Exception:
                pass
        print(text)




    def _handle_stop_command(self):
        """Handle /stop — kill all running background processes and
        background (async) delegations.

        Inspired by OpenAI Codex's separation of interrupt (stop current turn)
        from /stop (clean up background processes). See openai/codex#14602.
        """
        from tools.process_registry import process_registry

        processes = process_registry.list_sessions()
        running = [p for p in processes if p.get("status") == "running"]

        # Background subagents dispatched via delegate_task(background=true)
        # live in their own registry, not the process registry.
        try:
            from tools.async_delegation import active_count, interrupt_all
            n_async = active_count()
        except Exception:
            n_async = 0
            interrupt_all = None

        if not running and not n_async:
            print("  No running background processes.")
            return

        if running:
            print(f"  Stopping {len(running)} background process(es)...")
            killed = process_registry.kill_all()
            print(f"  ✅ Stopped {killed} process(es).")
        if n_async and interrupt_all is not None:
            stopped = interrupt_all(reason="/stop")
            print(f"  ✅ Interrupted {stopped} background delegation(s).")

    def _handle_agents_command(self):
        """Handle /agents — show background processes and agent status."""
        from cli import _cprint
        from tools.process_registry import format_uptime_short, process_registry

        processes = process_registry.list_sessions()
        running = [p for p in processes if p.get("status") == "running"]
        finished = [p for p in processes if p.get("status") != "running"]

        _cprint(f"  Running processes: {len(running)}")
        for p in running:
            cmd = p.get("command", "")[:80]
            up = format_uptime_short(p.get("uptime_seconds", 0))
            _cprint(f"    {p.get('session_id', '?')} · {up} · {cmd}")

        if finished:
            _cprint(f"  Recently finished: {len(finished)}")

        # Background (async) delegations — delegate_task(background=true)
        try:
            from tools.async_delegation import list_async_delegations
            delegations = list_async_delegations()
        except Exception:
            delegations = []
        running_d = [
            d for d in delegations
            if d.get("status") in ("running", "stalling")
        ]
        if delegations:
            _cprint(f"  Background delegations: {len(running_d)} running")
            for d in delegations:
                goal = (d.get("goal") or "")[:60]
                status = d.get("status", "?")
                line = (
                    f"    {d.get('delegation_id', '?')} · "
                    f"{status} · {goal}"
                )
                # Live-status detail for in-flight delegations (#51690).
                if status == "stalling":
                    quiet = d.get("stalled_after_quiet_seconds")
                    if quiet is not None:
                        line += (
                            f" · no progress {quiet:.0f}s — interrupting"
                        )
                elif status in ("running",):
                    quiet = d.get("seconds_since_progress")
                    if quiet is not None and quiet >= 60:
                        line += f" · quiet {quiet:.0f}s"
                _cprint(line)
                for i, child in enumerate(d.get("children_activity") or []):
                    if not isinstance(child, dict):
                        continue
                    tool = child.get("current_tool")
                    doing = f"in {tool}" if tool else "between turns"
                    part = (
                        f"      └ child {i + 1}: "
                        f"{child.get('api_calls', '?')} api calls · {doing}"
                    )
                    idle = child.get("seconds_since_activity")
                    if idle is not None:
                        part += f" · last activity {idle:.0f}s ago"
                    _cprint(part)

        agent_running = getattr(self, "_agent_running", False)
        _cprint(f"  Agent: {'running' if agent_running else 'idle'}")


    def _handle_paste_command(self):
        """Handle /paste — explicitly check clipboard for an image.

        This is the reliable fallback for terminals where BracketedPaste
        doesn't fire for image-only clipboard content (e.g., VSCode terminal).
        """
        from cli import _DIM, _RST, _cprint
        from son_of_anton_cli.clipboard import has_clipboard_image
        if has_clipboard_image():
            if self._try_attach_clipboard_image():
                n = len(self._attached_images)
                _cprint(f"  📎 Image #{n} attached from clipboard")
            else:
                _cprint(f"  {_DIM}(>_<) Clipboard has an image but extraction failed{_RST}")
        else:
            _cprint(f"  {_DIM}(._.) No image found in clipboard{_RST}")

    def _handle_copy_command(self, cmd_original: str) -> None:
        """Handle /copy [number] — copy assistant output to clipboard."""
        from cli import _assistant_copy_text, _cprint
        parts = cmd_original.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        assistant = [m for m in self.conversation_history if m.get("role") == "assistant"]
        if not assistant:
            _cprint("  Nothing to copy yet.")
            return

        if arg:
            try:
                idx = int(arg) - 1
            except ValueError:
                _cprint("  Usage: /copy [number]")
                return
            if idx < 0 or idx >= len(assistant):
                _cprint(f"  Invalid response number. Use 1-{len(assistant)}.")
                return
        else:
            idx = len(assistant) - 1
            while idx >= 0 and not _assistant_copy_text(assistant[idx].get("content")):
                idx -= 1
            if idx < 0:
                _cprint("  Nothing to copy in assistant responses yet.")
                return

        text = _assistant_copy_text(assistant[idx].get("content"))
        if not text:
            _cprint("  Nothing to copy in that assistant response.")
            return

        try:
            from son_of_anton_cli.clipboard import (
                is_remote_shell_session,
                write_clipboard_text,
            )
            if is_remote_shell_session():
                # Over SSH, native tools would write the REMOTE clipboard
                # (or an X-forwarded one) — OSC 52 reaches the terminal
                # the user is actually sitting at. Fixes #31528.
                self._write_osc52_clipboard(text)
                _cprint(
                    f"  Copied assistant response #{idx + 1} via OSC 52 "
                    "(terminal support required)"
                )
                return
            if write_clipboard_text(text):
                _cprint(f"  Copied assistant response #{idx + 1} to clipboard")
                return
            # Native tools unavailable/failed — fall back to OSC 52 so
            # SSH/tmux sessions can still copy via the terminal emulator.
            self._write_osc52_clipboard(text)
            _cprint(
                f"  Copied assistant response #{idx + 1} via OSC 52 "
                "(terminal support required)"
            )
        except Exception as e:
            _cprint(f"  Clipboard copy failed: {e}")

    def _handle_image_command(self, cmd_original: str):
        """Handle /image <path> — attach a local image file for the next prompt."""
        from cli import _DIM, _IMAGE_EXTENSIONS, _RST, _cprint, _resolve_attachment_path, _split_path_input
        raw_args = (cmd_original.split(None, 1)[1].strip() if " " in cmd_original else "")
        if not raw_args:
            hint = "/path/to/image.png"
            _cprint(f"  {_DIM}Usage: /image <path>  e.g. /image {hint}{_RST}")
            return

        path_token, _remainder = _split_path_input(raw_args)
        image_path = _resolve_attachment_path(path_token)
        if image_path is None:
            _cprint(f"  {_DIM}(>_<) File not found: {path_token}{_RST}")
            return
        if image_path.suffix.lower() not in _IMAGE_EXTENSIONS:
            _cprint(f"  {_DIM}(._.) Not a supported image file: {image_path.name}{_RST}")
            return

        self._attached_images.append(image_path)
        _cprint(f"  📎 Attached image: {image_path.name}")
        if _remainder:
            _cprint(f"  {_DIM}Now type your prompt (or use --image in single-query mode): {_remainder}{_RST}")

    def _handle_tools_command(self, cmd: str):
        """Handle /tools [list|disable|enable] slash commands.

        /tools (no args) shows the tool list.
        /tools list shows enabled/disabled status per toolset.
        /tools disable/enable saves the change to config and resets
        the session so the new tool set takes effect cleanly (no
        prompt-cache breakage mid-conversation).
        """
        from cli import _ACCENT, _DIM, _RST, _cprint
        import shlex
        from argparse import Namespace
        from contextlib import redirect_stdout
        from io import StringIO
        from son_of_anton_cli.tools_config import tools_disable_enable_command

        def _run_capture(ns: Namespace) -> None:
            """Run tools_disable_enable_command, routing its ANSI-colored
            print() output through _cprint when inside the interactive TUI
            so escapes aren't mangled by patch_stdout's StdoutProxy into
            garbled '?[32m...?[0m' text.

            Outside the TUI (standalone mode, tests), call straight through
            so real stdout / pytest capture works as expected.
            """
            # Standalone/tests, run as usual
            if getattr(self, "_app", None) is None:
                tools_disable_enable_command(ns)
                return

            # Buffer reports isatty()=True so color() in son_of_anton_cli/colors.py
            # still emits ANSI escapes. StringIO.isatty() is False, which
            # would otherwise strip all colors before we re-render them.
            class _TTYBuf(StringIO):
                def isatty(self) -> bool:
                    return True

            buf = _TTYBuf()
            with redirect_stdout(buf):
                tools_disable_enable_command(ns)
            for line in buf.getvalue().splitlines():
                _cprint(line)

        try:
            parts = shlex.split(cmd)
        except ValueError:
            parts = cmd.split()

        subcommand = parts[1] if len(parts) > 1 else ""
        if subcommand not in {"list", "disable", "enable"}:
            self.show_tools()
            return

        if subcommand == "list":
            _run_capture(Namespace(tools_action="list", platform="cli"))
            return

        names = parts[2:]
        if not names:
            print(f"(._.) Usage: /tools {subcommand} <name> [name ...]")
            print(f"  Built-in toolset:  /tools {subcommand} web")
            print(f"  MCP tool:          /tools {subcommand} github:create_issue")
            return

        # Apply the change directly — the user typing the command is implicit
        # consent.  Do NOT use input() here; it hangs inside prompt_toolkit's
        # TUI event loop (known pitfall).
        verb = "Disabling" if subcommand == "disable" else "Enabling"
        label = ", ".join(names)
        _cprint(f"{_ACCENT}{verb} {label}...{_RST}")

        _run_capture(Namespace(tools_action=subcommand, names=names, platform="cli"))

        # Reset session so the new tool config is picked up from a clean state
        from son_of_anton_cli.tools_config import _get_platform_tools
        from son_of_anton_cli.config import load_config
        self.enabled_toolsets = _get_platform_tools(load_config(), "cli")
        self.new_session()
        _cprint(f"{_DIM}Session reset. New tool configuration is active.{_RST}")

    def _handle_resume_command(self, cmd_original: str) -> None:
        """Handle /resume <session_id_or_title> — switch to a previous session mid-conversation."""
        from cli import _cprint, _sync_process_session_id
        parts = cmd_original.split(None, 1)
        target = parts[1].strip() if len(parts) > 1 else ""

        # Strip common outer brackets/quotes users may type literally from the
        # usage hint (e.g. ``/resume <abc123>`` or ``/resume [abc123]``).  The
        # `/resume` help text shows angle brackets as a placeholder and a few
        # users copy them through verbatim.  Stripping them keeps the lookup
        # working without changing the help string.
        if len(target) >= 2 and (
            (target[0] == "<" and target[-1] == ">")
            or (target[0] == "[" and target[-1] == "]")
            or (target[0] == '"' and target[-1] == '"')
            or (target[0] == "'" and target[-1] == "'")
        ):
            target = target[1:-1].strip()

        if not target:
            _cprint("  Usage: /resume <number|session_id_or_title>")
            if self._show_recent_sessions(reason="resume"):
                # Arm a one-shot pending-resume selection so the user can type
                # just the number (`3`) on the next line instead of having to
                # retype `/resume 3`. The list here must match the one shown by
                # _show_recent_sessions and used for index resolution below —
                # all three go through _list_recent_sessions(limit=10). See
                # #34584.
                self._pending_resume_sessions = self._list_recent_sessions(limit=10)
                return
            _cprint("  Tip:   Use /history or `son-of-anton sessions list` to find sessions.")
            return

        # Any explicit /resume <target> supersedes a previously-armed bare
        # numbered prompt.
        self._pending_resume_sessions = None

        if not self._session_db:
            from son_of_anton_state import format_session_db_unavailable
            _cprint(f"  {format_session_db_unavailable()}")
            return

        # Resolve numbered selection, title, or ID
        if target.isdigit():
            sessions = self._list_recent_sessions(limit=10)
            index = int(target)
            if index < 1 or index > len(sessions):
                _cprint(f"  Resume index {index} is out of range.")
                _cprint("  Use /resume with no arguments to see available sessions.")
                return
            selected = sessions[index - 1]
            target_id = selected["id"]
        else:
            from son_of_anton_cli.main import _resolve_session_by_name_or_id
            resolved = _resolve_session_by_name_or_id(target)
            target_id = resolved or target

        session_meta = self._session_db.get_session(target_id)
        if not session_meta:
            _cprint(f"  Session not found: {target}")
            _cprint("  Use /sessions or `son-of-anton sessions list` to see available sessions.")
            return

        # If the target is the empty head of a compression chain, redirect to
        # the descendant that actually holds the transcript. See #15000.
        try:
            resolved_id = self._session_db.resolve_resume_session_id(target_id)
        except Exception:
            resolved_id = target_id
        if resolved_id and resolved_id != target_id:
            _cprint(
                f"  Session {target_id} was compressed into {resolved_id}; "
                f"resuming the descendant with your transcript."
            )
            target_id = resolved_id
            resolved_meta = self._session_db.get_session(target_id)
            if resolved_meta:
                session_meta = resolved_meta

        if target_id == self.session_id:
            _cprint("  Already on that session.")
            return

        old_session_id = self.session_id
        # Flush un-persisted messages before ending the old session (#47202).
        if self.agent:
            try:
                self.agent._flush_messages_to_session_db(
                    self.conversation_history,
                    conversation_history=self.conversation_history,
                )
            except Exception:
                pass
        # End current session
        try:
            self._session_db.end_session(self.session_id, "resumed_other")
        except Exception:
            pass

        # Switch to the target session
        self.session_id = target_id
        self._resumed = True
        self._pending_title = None
        _sync_process_session_id(target_id)

        # Load conversation history (strip transcript-only metadata entries).
        # repair_alternation: this /resume feeds LIVE REPLAY — ``restored``
        # becomes ``self.conversation_history`` for subsequent turns. Heal a
        # durable ``user;user`` violation once here instead of re-firing the
        # pre-request repair on every request for the rest of the session.
        #
        # Both projections come from one lineage SELECT: model_history is
        # alternation-repaired for live replay; display_history is the full
        # lineage verbatim, used by _display_resumed_history() so timeline
        # events and ancestor rows render correctly (matching the startup
        # --resume path in _preload_resumed_session).
        model_history, display_history = self._session_db.get_resume_conversations(
            target_id
        )
        restored = [m for m in (model_history or []) if m.get("role") != "session_meta"]
        self.conversation_history = restored
        self._resume_display_history = [
            m for m in (display_history or []) if m.get("role") != "session_meta"
        ]

        # Re-open the target session so it's not marked as ended
        try:
            self._session_db.reopen_session(target_id)
        except Exception:
            pass

        # Sync the agent if already initialised
        if self.agent:
            self.agent.session_id = target_id
            self.agent.reset_session_state()
            if hasattr(self.agent, "_last_flushed_db_idx"):
                self.agent._last_flushed_db_idx = len(self.conversation_history)
            if hasattr(self.agent, "_todo_store"):
                try:
                    from tools.todo_tool import TodoStore
                    self.agent._todo_store = TodoStore()
                except Exception:
                    pass
            if hasattr(self.agent, "_invalidate_system_prompt"):
                self.agent._invalidate_system_prompt()

            # Notify memory providers that session_id rotated to a resumed
            # session. reset=False — the provider's accumulated state is
            # still valid; it just needs to target the new session_id for
            # subsequent writes. See #6672.
            try:
                _mm = getattr(self.agent, "_memory_manager", None)
                if _mm is not None:
                    _mm.on_session_switch(
                        target_id,
                        parent_session_id=old_session_id or "",
                        reset=False,
                        reason="resume",
                    )
            except Exception:
                pass

        title_part = f" \"{session_meta['title']}\"" if session_meta.get("title") else ""
        from agent.context_compressor import is_user_originated_turn

        # Count only user-originated turns (#80622): legacy compaction
        # handoffs are durable role=user rows without display_kind.
        msg_count = len([m for m in self._resume_display_history if is_user_originated_turn(m)])
        if self.conversation_history:
            _cprint(
                f"  ↻ Resumed session {target_id}{title_part}"
                f" ({msg_count} user message{'s' if msg_count != 1 else ''},"
                f" {len(self.conversation_history)} total)"
            )
            self._display_resumed_history()
        else:
            _cprint(f"  ↻ Resumed session {target_id}{title_part} — no messages, starting fresh.")

        # Retarget the process + tool cwd to where the session was started, so a
        # mid-chat /resume (and /sessions <id>, which delegates here) lands in the
        # same directory as a startup `son-of-anton -c`/`--resume`. The startup resume
        # paths already call this; without it, the terminal/code-exec tools and
        # relative-path resolution keep operating in the wrong repo. Idempotent
        # and a no-op when the session recorded no cwd. See #38562.
        self._restore_session_cwd(session_meta)

        # Restore the target session's persisted YOLO bypass. Any bypass the
        # PREVIOUS session had toggled on stops applying automatically because
        # the approval session key just changed. Same contract as a startup
        # --resume.
        self._restore_session_yolo(session_meta)

        # Restore the target session's model/provider so a mid-chat /resume
        # doesn't silently revert to the config default. Same contract as a
        # startup --resume (_preload_resumed_session / _init_agent path).
        self._restore_session_model(session_meta)

    def _handle_sessions_command(self, cmd_original: str) -> None:
        """Handle /sessions [list|<id_or_title>] — browse or resume previous sessions.

        Without arguments, prints the same recent-sessions table that /resume
        shows when called without a target, and tells the user how to resume.
        With an explicit subcommand or target, delegates to the resume flow so
        ``/sessions <id>`` and ``/resume <id>`` behave identically.

        The TUI ships an interactive picker overlay for this command; the
        classic CLI prints an inline list because there is no equivalent
        overlay primitive here. Without this handler the canonical name
        ``sessions`` falls through ``process_command``'s elif chain and
        prints ``Unknown command: sessions`` even though the command is
        registered in the central COMMAND_REGISTRY.
        """
        from cli import _cprint
        parts = cmd_original.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        sub = arg.lower()

        # Bare /sessions or /sessions list — show recent sessions inline.
        if not arg or sub in {"list", "ls", "browse"}:
            if not self._session_db:
                from son_of_anton_state import format_session_db_unavailable
                _cprint(f"  {format_session_db_unavailable()}")
                return
            if not self._show_recent_sessions(reason="sessions"):
                _cprint("  (._.) No previous sessions yet.")
            return

        # /sessions <id_or_title> behaves the same as /resume <id_or_title>.
        self._handle_resume_command(f"/resume {arg}")

    def _handle_worktree_command(self, cmd_original: str) -> None:
        """Handle /worktree — inspect, create, or reclaim isolated git worktrees.

        Syntax:
            /worktree                  — show the active worktree (if any)
            /worktree new [name]       — create a worktree and move this session into it
            /worktree list             — list worktrees under the repo's .worktrees/
            /worktree prune [--dry-run] — reclaim safe trees + merged branches

        Inspired by Copilot CLI's ``/worktree new``: start isolated work in a
        fresh worktree without leaving the session. Creating one retargets the
        terminal/file tools (``TERMINAL_CWD`` + process cwd) at the new tree;
        the launcher's exit cleanup applies (kept only when it has unpushed
        commits, same as ``son-of-anton -w``).

        ``prune`` is the same attended reclaim as ``son-of-anton worktree prune``
        (son_of_anton_cli/worktree_gc.py): never deletes tracked changes, unique
        unpushed commits, or in-use trees; archives untracked-only scratch.
        """
        import subprocess

        import cli as _cli

        parts = cmd_original.split(None, 2)
        sub = parts[1].lower() if len(parts) > 1 else ""

        repo_root = _cli._git_repo_root()

        if not sub or sub in {"status", "show"}:
            active = _cli._active_worktree
            if active:
                print(f"  Active worktree: {active['path']}")
                print(f"  Branch: {active['branch']}")
            else:
                print("  No active worktree for this session.")
            if repo_root:
                print("  /worktree new [name] — create one and move this session into it")
                print("  /worktree prune      — reclaim stale trees and merged branches")
            else:
                print("  (not inside a git repository)")
            return

        if sub in {"prune", "gc", "clean"}:
            if not repo_root:
                print("  Not inside a git repository.")
                return
            rest = parts[2].strip().lower() if len(parts) > 2 else ""
            dry_run = "--dry-run" in rest or "-n" in rest.split()
            from son_of_anton_cli import worktree_gc

            active = _cli._active_worktree
            tree_records = worktree_gc.audit_worktrees(repo_root, with_sizes=False)
            if active:
                # Never reap the tree this very session is sitting in, even
                # if a concurrent audit would judge it clean+merged.
                active_path = str(active.get("path") or "")
                tree_records = [
                    record for record in tree_records
                    if record.path != active_path
                ]
            actions = worktree_gc.reclaim_worktrees(
                repo_root, dry_run=dry_run, records=tree_records
            )
            actions += worktree_gc.reclaim_branches(repo_root, dry_run=dry_run)
            if actions:
                for line in actions:
                    print(f"  {line}")
                print(f"  {len(actions)} action(s) {'planned' if dry_run else 'done'}.")
            else:
                print("  Nothing to reclaim — remaining trees/branches carry real work.")
            kept = [
                record for record in tree_records
                if record.verdict == "keep"
                and "in use" not in record.reason
            ]
            if kept:
                print(f"  Preserved {len(kept)} tree(s) with real work:")
                for record in kept:
                    print(f"    {record.name}: {record.reason}")
            return

        if sub in {"list", "ls"}:
            if not repo_root:
                print("  Not inside a git repository.")
                return
            try:
                result = subprocess.run(
                    ["git", "worktree", "list"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=10, cwd=repo_root,
                )
                out = result.stdout.strip() if result.returncode == 0 else ""
            except Exception:
                out = ""
            if out:
                for line in out.splitlines():
                    print(f"  {line}")
            else:
                print("  Could not list worktrees.")
            return

        if sub in {"new", "add", "create"}:
            if not repo_root:
                print("  ❌ /worktree new requires being inside a git repository.")
                return
            name = parts[2].strip() if len(parts) > 2 else None
            from son_of_anton_cli.config import load_config
            try:
                sync_base = bool(load_config().get("worktree_sync", True))
            except Exception:
                sync_base = True
            wt_info = _cli._setup_worktree(
                repo_root=repo_root, sync_base=sync_base, name=name,
            )
            if not wt_info:
                return  # _setup_worktree already printed the failure
            # Retarget the session's terminal/file tools at the new tree, the
            # same way `son-of-anton -w` and session-resume cwd restore do.
            try:
                os.chdir(wt_info["path"])
            except OSError as e:
                print(f"  ⚠ Created worktree but could not enter it: {e}")
            os.environ["TERMINAL_CWD"] = wt_info["path"]
            # Register for the same keep-if-unpushed cleanup as `son-of-anton -w`.
            # Only one worktree is tracked as "active" per process; an earlier
            # one keeps its own atexit registration (explicit info arg).
            import atexit
            _cli._active_worktree = wt_info
            atexit.register(_cli._cleanup_worktree, wt_info)
            print(f"  ✅ Worktree ready: {wt_info['path']}")
            print(f"  Branch: {wt_info['branch']}")
            print("  Terminal and file tools now operate in the worktree.")
            return

        print(f"  Unknown /worktree subcommand: {sub}")
        print("  Usage: /worktree [new [name] | list]")



    def _handle_cron_command(self, cmd: str):
        """Handle the /cron command to manage scheduled tasks."""
        from cli import get_job
        import shlex
        from tools.cronjob_tools import cronjob as cronjob_tool

        def _cron_api(**kwargs):
            return json.loads(cronjob_tool(**kwargs))

        def _normalize_skills(values):
            normalized = []
            for value in values:
                text = str(value or "").strip()
                if text and text not in normalized:
                    normalized.append(text)
            return normalized

        def _parse_flags(tokens):
            opts = {
                "name": None,
                "deliver": None,
                "repeat": None,
                "skills": [],
                "add_skills": [],
                "remove_skills": [],
                "clear_skills": False,
                "all": False,
                "prompt": None,
                "schedule": None,
                "positionals": [],
            }
            i = 0
            while i < len(tokens):
                token = tokens[i]
                if token == "--name" and i + 1 < len(tokens):
                    opts["name"] = tokens[i + 1]
                    i += 2
                elif token == "--deliver" and i + 1 < len(tokens):
                    opts["deliver"] = tokens[i + 1]
                    i += 2
                elif token == "--repeat" and i + 1 < len(tokens):
                    try:
                        opts["repeat"] = int(tokens[i + 1])
                    except ValueError:
                        print("(._.) --repeat must be an integer")
                        return None
                    i += 2
                elif token == "--skill" and i + 1 < len(tokens):
                    opts["skills"].append(tokens[i + 1])
                    i += 2
                elif token == "--add-skill" and i + 1 < len(tokens):
                    opts["add_skills"].append(tokens[i + 1])
                    i += 2
                elif token == "--remove-skill" and i + 1 < len(tokens):
                    opts["remove_skills"].append(tokens[i + 1])
                    i += 2
                elif token == "--clear-skills":
                    opts["clear_skills"] = True
                    i += 1
                elif token == "--all":
                    opts["all"] = True
                    i += 1
                elif token == "--prompt" and i + 1 < len(tokens):
                    opts["prompt"] = tokens[i + 1]
                    i += 2
                elif token == "--schedule" and i + 1 < len(tokens):
                    opts["schedule"] = tokens[i + 1]
                    i += 2
                else:
                    opts["positionals"].append(token)
                    i += 1
            return opts

        tokens = shlex.split(cmd)

        if len(tokens) == 1:
            print()
            print("+" + "-" * 68 + "+")
            print("|" + " " * 22 + "(^_^) Scheduled Tasks" + " " * 23 + "|")
            print("+" + "-" * 68 + "+")
            print()
            print("  Commands:")
            print("    /cron list")
            print('    /cron add "every 2h" "Check server status" [--skill blogwatcher]')
            print('    /cron edit <job_id> --schedule "every 4h" --prompt "New task"')
            print("    /cron edit <job_id> --skill blogwatcher --skill maps")
            print("    /cron edit <job_id> --remove-skill blogwatcher")
            print("    /cron edit <job_id> --clear-skills")
            print("    /cron pause <job_id>")
            print("    /cron resume <job_id>")
            print("    /cron run <job_id>")
            print("    /cron remove <job_id>")
            print()
            result = _cron_api(action="list")
            jobs = result.get("jobs", []) if result.get("success") else []
            if jobs:
                print("  Current Jobs:")
                print("  " + "-" * 63)
                for job in jobs:
                    repeat_str = job.get("repeat", "?")
                    print(f"    {job['job_id'][:12]:<12} | {job['schedule']:<15} | {repeat_str:<8}")
                    if job.get("skills"):
                        print(f"      Skills: {', '.join(job['skills'])}")
                    print(f"      {job.get('prompt_preview', '')}")
                    if job.get("next_run_at"):
                        print(f"      Next: {job['next_run_at']}")
                    print()
            else:
                print("  No scheduled jobs. Use '/cron add' to create one.")
            print()
            return

        subcommand = tokens[1].lower()
        opts = _parse_flags(tokens[2:])
        if opts is None:
            return

        if subcommand == "list":
            result = _cron_api(action="list", include_disabled=opts["all"])
            jobs = result.get("jobs", []) if result.get("success") else []
            if not jobs:
                print("(._.) No scheduled jobs.")
                return

            print()
            print("Scheduled Jobs:")
            print("-" * 80)
            for job in jobs:
                print(f"  ID: {job['job_id']}")
                print(f"  Name: {job['name']}")
                print(f"  State: {job.get('state', '?')}")
                print(f"  Schedule: {job['schedule']} ({job.get('repeat', '?')})")
                print(f"  Next run: {job.get('next_run_at', 'N/A')}")
                if job.get("skills"):
                    print(f"  Skills: {', '.join(job['skills'])}")
                print(f"  Prompt: {job.get('prompt_preview', '')}")
                if job.get("last_run_at"):
                    print(f"  Last run: {job['last_run_at']} ({job.get('last_status', '?')})")
                print()
            return

        if subcommand in {"add", "create"}:
            positionals = opts["positionals"]
            if not positionals:
                print("(._.) Usage: /cron add <schedule> <prompt>")
                return
            schedule = opts["schedule"] or positionals[0]
            prompt = opts["prompt"] or " ".join(positionals[1:])
            skills = _normalize_skills(opts["skills"])
            if not prompt and not skills:
                print("(._.) Please provide a prompt or at least one skill")
                return
            result = _cron_api(
                action="create",
                schedule=schedule,
                prompt=prompt or None,
                name=opts["name"],
                deliver=opts["deliver"],
                repeat=opts["repeat"],
                skills=skills or None,
            )
            if result.get("success"):
                print(f"(^_^)b Created job: {result['job_id']}")
                print(f"  Schedule: {result['schedule']}")
                if result.get("skills"):
                    print(f"  Skills: {', '.join(result['skills'])}")
                print(f"  Next run: {result['next_run_at']}")
            else:
                print(f"(x_x) Failed to create job: {result.get('error')}")
            return

        if subcommand == "edit":
            positionals = opts["positionals"]
            if not positionals:
                print("(._.) Usage: /cron edit <job_id> [--schedule ...] [--prompt ...] [--skill ...]")
                return
            job_id = positionals[0]
            existing = get_job(job_id)
            if not existing:
                print(f"(._.) Job not found: {job_id}")
                return

            final_skills = None
            replacement_skills = _normalize_skills(opts["skills"])
            add_skills = _normalize_skills(opts["add_skills"])
            remove_skills = set(_normalize_skills(opts["remove_skills"]))
            existing_skills = list(existing.get("skills") or ([] if not existing.get("skill") else [existing.get("skill")]))
            if opts["clear_skills"]:
                final_skills = []
            elif replacement_skills:
                final_skills = replacement_skills
            elif add_skills or remove_skills:
                final_skills = [skill for skill in existing_skills if skill not in remove_skills]
                for skill in add_skills:
                    if skill not in final_skills:
                        final_skills.append(skill)

            result = _cron_api(
                action="update",
                job_id=job_id,
                schedule=opts["schedule"],
                prompt=opts["prompt"],
                name=opts["name"],
                deliver=opts["deliver"],
                repeat=opts["repeat"],
                skills=final_skills,
            )
            if result.get("success"):
                job = result["job"]
                print(f"(^_^)b Updated job: {job['job_id']}")
                print(f"  Schedule: {job['schedule']}")
                if job.get("skills"):
                    print(f"  Skills: {', '.join(job['skills'])}")
                else:
                    print("  Skills: none")
            else:
                print(f"(x_x) Failed to update job: {result.get('error')}")
            return

        if subcommand in {"pause", "resume", "run", "remove", "rm", "delete"}:
            positionals = opts["positionals"]
            if not positionals:
                print(f"(._.) Usage: /cron {subcommand} <job_id>")
                return
            job_id = positionals[0]
            action = "remove" if subcommand in {"remove", "rm", "delete"} else subcommand
            result = _cron_api(action=action, job_id=job_id, reason="paused from /cron" if action == "pause" else None)
            if not result.get("success"):
                print(f"(x_x) Failed to {action} job: {result.get('error')}")
                return
            if action == "pause":
                print(f"(^_^)b Paused job: {result['job']['name']} ({job_id})")
            elif action == "resume":
                print(f"(^_^)b Resumed job: {result['job']['name']} ({job_id})")
                print(f"  Next run: {result['job'].get('next_run_at')}")
            elif action == "run":
                print(f"(^_^)b Triggered job: {result['job']['name']} ({job_id})")
                print("  It will run on the next scheduler tick.")
            else:
                removed = result.get("removed_job", {})
                print(f"(^_^)b Removed job: {removed.get('name', job_id)} ({job_id})")
            return

        print(f"(._.) Unknown cron command: {subcommand}")
        print("  Available: list, add, edit, pause, resume, run, remove")



    def _handle_curator_command(self, cmd: str):
        """Handle /curator slash command.

        Delegates to son_of_anton_cli.curator so the CLI and the `son-of-anton curator`
        subcommand share the same handler set.
        """
        import shlex

        tokens = shlex.split(cmd)[1:] if cmd else []
        if not tokens:
            tokens = ["status"]

        try:
            from son_of_anton_cli.curator import cli_main
            cli_main(tokens)
        except SystemExit:
            # argparse calls sys.exit() on --help or errors; swallow so we
            # don't kill the interactive session.
            pass
        except Exception as exc:
            print(f"(._.) curator: {exc}")

    def _handle_skills_command(self, cmd: str):
        """Handle /skills slash command — delegates to son_of_anton_cli.skills_hub."""
        from cli import ChatConsole
        # Intercept write-approval review subcommands first (pending/approve/
        # reject/diff/mode); everything else goes to the skills hub.
        parts = cmd.strip().split()
        args = parts[1:] if len(parts) > 1 else []
        if args and args[0].lower() in {"pending", "approve", "apply", "reject",
                                        "deny", "drop", "diff", "approval", "mode"}:
            from son_of_anton_cli.write_approval_commands import handle_pending_subcommand
            from tools import write_approval as wa
            out = handle_pending_subcommand(
                wa.SKILLS, args,
                set_mode_fn=lambda enabled: self._save_write_approval("skills", enabled),
            )
            if out is not None:
                print(out)
                return
        from son_of_anton_cli.skills_hub import handle_skills_slash
        handle_skills_slash(cmd, ChatConsole())

    def _handle_learn_command(self, cmd: str):
        """Handle /learn — distill a reusable skill from anything the user describes.

        Open-ended: the argument is free text describing the source(s) — a
        directory, a URL, "what we just did", pasted notes. We build a
        standards-guided prompt and inject it onto the agent's input queue; the
        live agent gathers the material with the tools it already has and
        authors the skill via ``skill_manage``. No engine, no model-tool
        footprint, works on any terminal backend.
        """
        from agent.learn_prompt import build_learn_prompt

        # Everything after the command word is the open-ended request.
        parts = cmd.strip().split(None, 1)
        user_request = parts[1].strip() if len(parts) > 1 else ""

        msg = build_learn_prompt(user_request)
        if user_request:
            print("\n⚡ Learning a skill from what you described...")
        else:
            print("\n⚡ Learning a skill from this conversation...")
        if hasattr(self, "_pending_input"):
            self._pending_input.put(msg)
        else:  # pragma: no cover - defensive (no live input loop)
            print("  /learn needs an active chat session to run.")

    def _handle_init_command(self, cmd: str):
        """Handle /init — generate or update AGENTS.md from a project scan.

        Mirrors /learn: build a guidance-laden prompt and inject it onto the
        agent's input queue as a normal user turn. The live agent scans the
        project with its own read-only tools and writes/updates AGENTS.md via
        ``write_file``. No engine, no model-tool footprint, works on any
        terminal backend, and preserves prompt-cache invariants (no system
        prompt or history mutation).
        """
        from son_of_anton_cli.init_command import build_init_prompt_for_cwd

        # Everything after the command word is optional user emphasis.
        parts = cmd.strip().split(None, 1)
        extra = parts[1].strip() if len(parts) > 1 else ""

        msg = build_init_prompt_for_cwd(extra=extra)
        if "UPDATE the existing AGENTS.md" in msg:
            print("\n⚡ Updating AGENTS.md from a project scan...")
        else:
            print("\n⚡ Generating AGENTS.md from a project scan...")
        if hasattr(self, "_pending_input"):
            self._pending_input.put(msg)
        else:  # pragma: no cover - defensive (no live input loop)
            print("  /init needs an active chat session to run.")

    def _handle_memory_command(self, cmd: str):
        """Handle /memory slash command — pending review + approval-gate toggle."""
        from son_of_anton_cli.write_approval_commands import handle_pending_subcommand
        from tools import write_approval as wa
        parts = cmd.strip().split()
        args = parts[1:] if len(parts) > 1 else []
        store = getattr(self.agent, "_memory_store", None) if getattr(self, "agent", None) else None
        if store is None:
            # No live agent store (e.g. /memory approve invoked from the Desktop
            # GUI, or any context without an active agent). Apply against a freshly
            # loaded on-disk store, mirroring the gateway path
            # (gateway/slash_commands.py): it persists to the same MEMORY/USER.md
            # and creates MEMORY.md on the first approved write. Without this the
            # shared handler returns "memory store unavailable". See #46783.
            # load_on_disk_store() honors the user's configured char limits, so
            # an approval here enforces the same caps as the live agent would.
            from tools.memory_tool import load_on_disk_store
            store = load_on_disk_store()
        out = handle_pending_subcommand(
            wa.MEMORY, args,
            memory_store=store,
            set_mode_fn=lambda enabled: self._save_write_approval("memory", enabled),
        )
        if out is None:
            out = ("Unknown /memory subcommand. "
                   "Use: pending, approve <id>, reject <id>, approval <on|off>.")
        print(out)

    def _save_write_approval(self, subsystem: str, enabled: bool):
        """Persist <subsystem>.write_approval to config (for /memory|/skills approval)."""
        from cli import save_config_value
        save_config_value(f"{subsystem}.write_approval", bool(enabled))

    def _handle_background_command(self, cmd: str):
        """Handle /background <prompt> — run a prompt in a separate background session.

        Spawns a new AIAgent in a background thread with its own session.
        When it completes, prints the result to the CLI without modifying
        the active session's conversation history.
        """
        from cli import AIAgent, ChatConsole, _accent_hex, _cprint, _maybe_remap_for_light_mode, _render_final_assistant_content, set_approval_callback, set_secret_capture_callback, set_sudo_password_callback
        parts = cmd.strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            _cprint("  Usage: /background <prompt>")
            _cprint("  Example: /background Summarize the top HN stories today")
            _cprint("  The task runs in a separate session and results display here when done.")
            return

        prompt = parts[1].strip()
        self._background_task_counter += 1
        task_num = self._background_task_counter
        task_id = f"bg_{datetime.now().strftime('%H%M%S')}_{uuid.uuid4().hex[:6]}"

        # Make sure we have valid credentials
        if not self._ensure_runtime_credentials():
            _cprint("  (>_<) Cannot start background task: no valid credentials.")
            return

        _cprint(f"  🔄 Background task #{task_num} started: \"{prompt[:60]}{'...' if len(prompt) > 60 else ''}\"")
        _cprint(f"  Task ID: {task_id}")
        _cprint("  You can continue chatting — results will appear when done.\n")

        turn_route = self._resolve_turn_agent_config(prompt)

        def run_background():
            set_sudo_password_callback(self._sudo_password_callback)
            set_approval_callback(self._approval_callback)
            try:
                set_secret_capture_callback(self._secret_capture_callback)
            except Exception:
                pass
            try:
                bg_agent = AIAgent(
                    model=turn_route["model"],
                    api_key=turn_route["runtime"].get("api_key"),
                    base_url=turn_route["runtime"].get("base_url"),
                    provider=turn_route["runtime"].get("provider"),
                    api_mode=turn_route["runtime"].get("api_mode"),
                    acp_command=turn_route["runtime"].get("command"),
                    acp_args=turn_route["runtime"].get("args"),
                    max_tokens=turn_route["runtime"].get("max_tokens"),
                    max_iterations=self.max_turns,
                    enabled_toolsets=self.enabled_toolsets,
                    quiet_mode=True,
                    verbose_logging=False,
                    session_id=task_id,
                    platform="cli",
                    session_db=self._session_db,
                    reasoning_config=self.reasoning_config,
                    service_tier=self.service_tier,
                    request_overrides=turn_route.get("request_overrides"),
                    providers_allowed=self._providers_only,
                    providers_ignored=self._providers_ignore,
                    providers_order=self._providers_order,
                    provider_sort=self._provider_sort,
                    provider_require_parameters=self._provider_require_params,
                    provider_data_collection=self._provider_data_collection,
                    openrouter_min_coding_score=self._openrouter_min_coding_score,
                    fallback_model=self._fallback_model,
                )
                # Silence raw spinner; route thinking through TUI widget when no foreground agent is active.
                bg_agent._print_fn = lambda *_a, **_kw: None

                def _bg_thinking(text: str) -> None:
                    # Concurrent bg tasks may race on _spinner_text; acceptable for best-effort UI.
                    if not self._agent_running:
                        self._spinner_text = text
                        if self._app:
                            self._app.invalidate()

                bg_agent.thinking_callback = _bg_thinking

                result = bg_agent.run_conversation(
                    user_message=prompt,
                    task_id=task_id,
                )

                response = result.get("final_response", "") if result else ""
                if not response and result and result.get("error"):
                    response = f"Error: {result['error']}"

                # Display result in the CLI (thread-safe via patch_stdout).
                # Force a TUI refresh first so spinner/status bar don't overlap
                # with the output (fixes #2718).
                if self._app:
                    self._app.invalidate()
                    time.sleep(0.05)  # brief pause for refresh
                print()
                ChatConsole().print(f"[{_accent_hex()}]{'─' * 40}[/]")
                _cprint(f"  ✅ Background task #{task_num} complete")
                _cprint(f"  Prompt: \"{prompt[:60]}{'...' if len(prompt) > 60 else ''}\"")
                ChatConsole().print(f"[{_accent_hex()}]{'─' * 40}[/]")
                if response:
                    try:
                        from son_of_anton_cli.skin_engine import get_active_skin
                        _skin = get_active_skin()
                        label = _skin.get_branding("response_label", "⚛ Son of Anton")
                        _resp_color = _maybe_remap_for_light_mode(_skin.get_color("response_border", "#CD7F32"))
                        _resp_text = _maybe_remap_for_light_mode(_skin.get_color("banner_text", "#FFF8DC"))
                    except Exception:
                        label = "⚛ Son of Anton"
                        _resp_color = "#CD7F32"
                        _resp_text = "#FFF8DC"

                    _chat_console = ChatConsole()
                    _chat_console.print(Panel(
                        _render_final_assistant_content(response, mode=self.final_response_markdown),
                        title=f"[{_resp_color} bold]{label} (background #{task_num})[/]",
                        title_align="left",
                        border_style=_resp_color,
                        style=_resp_text,
                        box=rich_box.HORIZONTALS,
                        padding=(1, 4),
                        width=self._scrollback_box_width(),
                    ))
                else:
                    _cprint("  (No response generated)")

                # Play bell if enabled
                if self.bell_on_complete:
                    sys.stdout.write("\a")
                    sys.stdout.flush()

            except Exception as e:
                # Same TUI refresh pattern as success path (#2718)
                if self._app:
                    self._app.invalidate()
                    time.sleep(0.05)
                print()
                _cprint(f"  ❌ Background task #{task_num} failed: {e}")
            finally:
                try:
                    set_sudo_password_callback(None)
                    set_approval_callback(None)
                    set_secret_capture_callback(None)
                except Exception:
                    pass
                self._background_tasks.pop(task_id, None)
                # Clear spinner only if no foreground agent owns it
                if not self._agent_running:
                    self._spinner_text = ""
                if self._app:
                    self._invalidate(min_interval=0)

        thread = threading.Thread(target=run_background, daemon=True, name=f"bg-task-{task_id}")
        self._background_tasks[task_id] = thread
        thread.start()

    def _handle_bundles_command(self, cmd: str) -> None:
        """In-session ``/bundles`` — show installed skill bundles.

        Mirrors ``son-of-anton bundles list`` but renders inside the running
        CLI so users can discover what's available without dropping out
        of their session. Bundles are loaded via ``/<bundle-name>``.
        """
        from cli import ChatConsole, _BOLD, _DIM, _RST, _accent_hex, _cprint
        from son_of_anton_cli.slash_exec import CommandContext, execute_command

        reply = execute_command("bundles", CommandContext(surface="cli"))
        if "error" in reply.data:
            _cprint(f"\033[1;31mBundle subsystem unavailable: {reply.data['error']}{_RST}")
            return

        bundles = reply.data["bundles"]
        if not bundles:
            _cprint("  No skill bundles installed.")
            _cprint(
                f"  {_DIM}Create one with: son-of-anton bundles create "
                f"<name> --skill <s1> --skill <s2>{_RST}"
            )
            _cprint(f"  {_DIM}Directory: {reply.data['dir']}{_RST}")
            return

        _cprint(f"\n  ▣ {_BOLD}Skill Bundles{_RST} ({len(bundles)} installed):")
        for info in bundles:
            skill_count = len(info.get("skills", []))
            desc = info.get("description") or f"Load {skill_count} skills"
            ChatConsole().print(
                f"    [bold {_accent_hex()}]/{info['slug']:<20}[/] "
                f"[dim]-[/] {_escape(desc)} [dim]({skill_count} skills)[/]"
            )
            for s in info.get("skills", []):
                ChatConsole().print(f"        [dim]· {_escape(s)}[/]")
        _cprint(
            f"\n  {_DIM}Invoke a bundle with /<slug>. "
            f"Manage with `son-of-anton bundles`.{_RST}"
        )


    def _handle_refine_command(self, cmd: str) -> None:
        """Dispatch /refine — run the memory/skill review fork on demand.

        Same machinery as the automatic post-turn self-improvement loop
        (``AIAgent._spawn_background_review``), but user-triggered and with
        optional focus instructions. Writes go to the memory + skill stores
        in a background fork; the live conversation and prompt cache are
        never touched.
        """
        from cli import _DIM, _RST, _cprint

        parts = (cmd or "").strip().split(None, 1)
        focus = parts[1].strip() if len(parts) > 1 else ""

        agent = getattr(self, "agent", None)
        if agent is None:
            _cprint(f"  {_DIM}Nothing to refine yet — send a message first.{_RST}")
            return

        snapshot = list(getattr(self, "conversation_history", None) or [])
        if not snapshot:
            _cprint(f"  {_DIM}Nothing to refine yet — the conversation is empty.{_RST}")
            return

        review_skills = "skill_manage" in getattr(agent, "valid_tool_names", set())
        try:
            agent._spawn_background_review(
                messages_snapshot=snapshot,
                review_memory=True,
                review_skills=review_skills,
                focus=focus or None,
            )
        except Exception as exc:
            _cprint(f"  /refine failed to start: {exc}")
            return
        tail = f" (focus: {focus})" if focus else ""
        _cprint(
            f"  ⚗ Reviewing this conversation in the background{tail} — "
            f"any memory/skill updates will be reported when done."
        )

    def _handle_goal_command(self, cmd: str) -> None:
        """Dispatch /goal subcommands: set / draft / show / gate / status / pause / resume / clear."""
        from cli import _DIM, _RST, _cprint
        parts = (cmd or "").strip().split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        mgr = self._get_goal_manager()
        if mgr is None:
            _cprint(f"  {_DIM}Goals unavailable (no active session).{_RST}")
            return

        lower = arg.lower()

        # Bare /goal or /goal status → show current state
        if not arg or lower == "status":
            _cprint(f"  {mgr.status_line()}")
            return

        # /goal show → print the active goal's completion contract
        if lower == "show":
            _cprint(f"  {mgr.status_line()}")
            _cprint(f"  {mgr.render_contract()}")
            return

        # /goal draft <objective> → expand plain text into a structured
        # completion contract (outcome / verification / constraints /
        # boundaries / stop_when) and set it as the active goal. Adapted
        # from Codex's "let the agent draft the goal" guidance: the contract
        # makes "done" evidence-based instead of a loose vibe check.
        if lower.startswith("draft"):
            objective = arg[len("draft"):].strip()
            if not objective:
                _cprint("  Usage: /goal draft <objective in plain language>")
                return
            self._handle_goal_draft(objective)
            return

        if lower == "pause":
            state = mgr.pause(reason="user-paused")
            if state is None:
                _cprint(f"  {_DIM}No goal set.{_RST}")
            else:
                _cprint(f"  ⏸ Goal paused: {state.goal}")
            return

        if lower == "resume":
            state = mgr.resume()
            if state is None:
                _cprint(f"  {_DIM}No goal to resume.{_RST}")
            else:
                _cprint(f"  ▶ Goal resumed: {state.goal}")
                # Resume must restart work, not just flip persisted state
                # (#75362): queue the canonical continuation prompt the same
                # way /goal <text> queues its kickoff, so the loop takes the
                # next step without the user sending another message.
                prompt = mgr.next_continuation_prompt()
                queued = False
                if prompt:
                    try:
                        self._pending_input.put(prompt)
                        queued = True
                    except Exception:
                        pass
                if queued:
                    _cprint(f"  {_DIM}Continuing now — taking the next step.{_RST}")
                else:
                    _cprint(
                        f"  {_DIM}Send any message to kick off the next step.{_RST}"
                    )
            return

        if lower in {"clear", "stop", "done"}:
            had = mgr.has_goal()
            mgr.clear()
            if had:
                _cprint("  ✓ Goal cleared.")
            else:
                _cprint(f"  {_DIM}No active goal.{_RST}")
            return

        # /goal wait <pid> [reason] — park the loop on a background process so
        # it stops re-poking the agent every turn while it waits on CI / a
        # build / a long job. The barrier auto-clears when the PID exits.
        if lower == "wait" or lower.startswith("wait "):
            wait_arg = arg[len("wait"):].strip()
            if not wait_arg:
                _cprint("  Usage: /goal wait <pid> [reason]")
                return
            wtokens = wait_arg.split(None, 1)
            try:
                pid = int(wtokens[0])
            except ValueError:
                _cprint("  /goal wait: <pid> must be an integer process id.")
                return
            reason = wtokens[1].strip() if len(wtokens) > 1 else ""
            try:
                mgr.wait_on(pid, reason=reason)
            except (RuntimeError, ValueError) as exc:
                _cprint(f"  /goal wait: {exc}")
                return
            rtxt = f" ({reason})" if reason else ""
            _cprint(f"  ⏳ Goal parked on pid {pid}{rtxt}. Loop pauses until it exits.")
            return

        # /goal unwait — drop the wait barrier and resume normal looping.
        if lower == "unwait":
            if mgr.stop_waiting():
                _cprint("  ▶ Wait barrier cleared — goal loop resumes.")
            else:
                _cprint(f"  {_DIM}No wait barrier set.{_RST}")
            return

        # /goal gate ... — manage deterministic quality gates. A gate is a
        # shell command that must pass before the judge may declare the goal
        # done; a failing gate's output becomes the continuation prompt.
        if lower == "gate" or lower.startswith("gate "):
            gate_arg = arg[len("gate"):].strip()
            gate_lower = gate_arg.lower()
            if not gate_arg or gate_lower == "list":
                for line in mgr.render_gates().splitlines():
                    _cprint(f"  {line}")
                return
            if gate_lower.startswith("add "):
                command = gate_arg[len("add"):].strip()
                try:
                    gate = mgr.add_gate(command)
                except (RuntimeError, ValueError) as exc:
                    _cprint(f"  /goal gate add: {exc}")
                    return
                _cprint(
                    f"  ⚿ Gate added: $ {gate.command} "
                    f"({gate.max_retries} retries, {gate.timeout_seconds}s timeout). "
                    f"It must pass before the goal can complete."
                )
                return
            if gate_lower.startswith("remove ") or gate_lower.startswith("rm "):
                idx_text = gate_arg.split(None, 1)[1].strip()
                try:
                    removed = mgr.remove_gate(int(idx_text))
                except (RuntimeError, ValueError, IndexError) as exc:
                    _cprint(f"  /goal gate remove: {exc}")
                    return
                _cprint(f"  ✓ Gate removed: $ {removed}")
                return
            if gate_lower == "clear":
                try:
                    prev = mgr.clear_gates()
                except RuntimeError as exc:
                    _cprint(f"  /goal gate clear: {exc}")
                    return
                _cprint(f"  ✓ Cleared {prev} gate{'s' if prev != 1 else ''}.")
                return
            _cprint("  Usage: /goal gate [list | add <command> | remove <N> | clear]")
            return

        # Otherwise treat the arg as the goal text. Inline `field: value`
        # lines (verify:, constraints:, boundaries:, stop when:) are parsed
        # into a completion contract; the remaining prose is the headline.
        # A plain free-form goal with no such lines behaves exactly as before.
        from son_of_anton_cli.goals import parse_contract

        headline, contract = parse_contract(arg)
        goal_text = headline or arg
        try:
            state = mgr.set(goal_text, contract=contract if not contract.is_empty() else None)
        except ValueError as exc:
            _cprint(f"  Invalid goal: {exc}")
            return

        _cprint(f"  ⊙ Goal set ({state.max_turns}-turn budget): {state.goal}")
        if state.has_contract():
            _cprint(f"  {_DIM}Completion contract:{_RST}")
            for line in state.contract.render_block().splitlines():
                _cprint(f"    {line}")
        _cprint(
            f"  {_DIM}After each turn, a judge model checks if the goal is done"
            f"{' against the contract above' if state.has_contract() else ''}. "
            f"Son of Anton keeps working until it is, you pause/clear it, or the budget is "
            f"exhausted. Use /goal status, /goal show, /goal pause, /goal resume, /goal clear.{_RST}"
        )
        # Kick the loop off immediately so the user doesn't have to send a
        # separate message after setting the goal.
        try:
            self._pending_input.put(state.goal)
        except Exception:
            pass

    def _handle_goal_draft(self, objective: str) -> None:
        """Draft a structured completion contract from a plain objective and
        set it as the active goal. Falls back to a bare goal if the aux model
        can't produce a contract."""
        from cli import _DIM, _RST, _cprint
        from son_of_anton_cli.goals import draft_contract

        mgr = self._get_goal_manager()
        if mgr is None:
            _cprint(f"  {_DIM}Goals unavailable (no active session).{_RST}")
            return

        _cprint(f"  {_DIM}Drafting completion contract…{_RST}")
        try:
            contract = draft_contract(objective)
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).debug("goal draft failed: %s", exc)
            contract = None

        try:
            state = mgr.set(objective, contract=contract)
        except ValueError as exc:
            _cprint(f"  Invalid goal: {exc}")
            return

        _cprint(f"  ⊙ Goal set ({state.max_turns}-turn budget): {state.goal}")
        if state.has_contract():
            _cprint(f"  {_DIM}Drafted completion contract:{_RST}")
            for line in state.contract.render_block().splitlines():
                _cprint(f"    {line}")
            _cprint(
                f"  {_DIM}Tighten any field by re-setting the goal with inline "
                f"lines (e.g. verify: <command>), then /goal resume. "
                f"Use /goal show to review.{_RST}"
            )
        else:
            _cprint(
                f"  {_DIM}Couldn't draft a contract (aux model unavailable) — "
                f"running as a free-form goal. The per-turn judge still applies.{_RST}"
            )
        try:
            self._pending_input.put(state.goal)
        except Exception:
            pass



    def _handle_skin_command(self, cmd: str):
        """Handle /skin [name] — show or change the display skin."""
        from cli import _ACCENT, _THINKING, save_config_value
        try:
            from son_of_anton_cli.skin_engine import list_skins, set_active_skin, get_active_skin_name
        except ImportError:
            print("Skin engine not available.")
            return

        parts = cmd.strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            # Show current skin and list available
            current = get_active_skin_name()
            skins = list_skins()
            print(f"\n  Current skin: {current}")
            print("  Available skins:")
            for s in skins:
                marker = " ●" if s["name"] == current else "  "
                source = f" ({s['source']})" if s["source"] == "user" else ""
                print(f"   {marker} {s['name']}{source} — {s['description']}")
            print("\n  Usage: /skin <name>")
            print(f"  Custom skins: drop a YAML file in {display_son_of_anton_home()}/skins/\n")
            return

        new_skin = parts[1].strip().lower()
        available = {s["name"] for s in list_skins()}
        if new_skin not in available:
            print(f"  Unknown skin: {new_skin}")
            print(f"  Available: {', '.join(sorted(available))}")
            return

        set_active_skin(new_skin)
        _ACCENT.reset()  # Re-resolve ANSI color for the new skin
        _THINKING.reset()  # Re-resolve the reasoning-text accent too
        # _DIM is now a fixed dim+italic ANSI escape (terminal-default fg)
        # so it doesn't need re-resolving on skin switch.
        if save_config_value("display.skin", new_skin):
            print(f"  Skin set to: {new_skin} (saved)")
        else:
            print(f"  Skin set to: {new_skin}")
        print("  Note: banner colors will update on next session start.")
        if self._apply_tui_skin_style():
            print("  Prompt + TUI colors updated.")

    def _compose_in_editor(self, initial_text: str = "") -> str:
        """Open ``$VISUAL``/``$EDITOR`` on a temp markdown file and return the
        saved buffer (comment lines starting with ``#!`` stripped).

        Returns the composed prompt text, or an empty string if the editor
        could not be launched or the buffer was left empty. Factored out so
        the read-back/strip logic is unit-testable without spawning an editor.

        ctrl+g in the TUI is the only way in: it seeds the buffer with the
        draft already typed and sends whatever comes back. There used to be a
        ``/prompt`` command doing the same thing one keystroke slower, which
        is why this is a method rather than a closure.
        """
        import os
        import shlex
        import subprocess
        import tempfile

        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            editor = "nano"

        header = (
            "#! Compose your prompt below. Lines starting with '#!' are ignored.\n"
            "#! Save and quit to send; leave empty to cancel.\n\n"
        )
        fd, path = tempfile.mkstemp(suffix=".md", prefix="son_of_anton_prompt_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(header)
                if initial_text:
                    fh.write(initial_text)
            try:
                subprocess.call([*shlex.split(editor), path])
            except Exception:
                # Fall back to a bare invocation (editor value may not be a
                # simple argv-splittable string on some platforms).
                subprocess.call(f"{editor} {shlex.quote(path)}", shell=True)
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        lines = [ln for ln in raw.splitlines() if not ln.startswith("#!")]
        return "\n".join(lines).strip()

    def _commit_identity(self) -> tuple[str, str]:
        """The ``git.author_name`` / ``git.author_email`` pair for ``/commit``.

        This is the AUTHOR identity for ``git commit --author`` only — the
        committer always stays whatever the repository or the user's global
        config already says, so the log reads "authored by the configured
        account, committed by you". Either key being blank means "don't
        override anything" — git then uses its normal identity for both.

        Read through the canonical loader rather than ``self.config``: cli.py
        carries its own smaller defaults table, so a key that lives only in
        ``DEFAULT_CONFIG`` is absent from the session copy.
        """
        try:
            from son_of_anton_cli.config import load_config_readonly

            git_cfg = (load_config_readonly() or {}).get("git") or {}
        except Exception:
            git_cfg = (self.config or {}).get("git") or {}
        name = str(git_cfg.get("author_name") or "").strip()
        email = str(git_cfg.get("author_email") or "").strip()
        if not (name and email):
            return "", ""
        return name, email

    def build_commit_prompt(self, extra: str = "") -> str:
        """The turn ``/commit`` hands to the agent.

        Written as a task for the agent rather than run as shell here on
        purpose: reviewing a diff and matching a repository's commit style is
        the model's job, and it already has the terminal tool. Keeping it a
        prompt means zero new model-tool surface.
        """
        name, email = self._commit_identity()
        lines = [
            "You are a summarization agent. Your job is to review all uncommitted git "
            "changes in the current repository, write an accurate commit message "
            "matching the existing style, and then stage+commit them.",
        ]
        if name and email:
            lines.append(
                f"Author the commit as {name} <{email}>: commit with "
                f"`git commit --author=\"{name} <{email}>\" -m \"<message>\"`. "
                "Leave the committer untouched — it must stay whatever identity the "
                "repository or your global git config already sets (do not override "
                "the committer; no GIT_COMMITTER_NAME / GIT_COMMITTER_EMAIL, no "
                "-c user.name / user.email)."
            )
        else:
            lines.append(
                "Commit with the repository's already-configured git identity; do not "
                "override author or committer."
            )
        if extra.strip():
            lines.append(f"Additional instructions from the user: {extra.strip()}")
        return "\n\n".join(lines)

    def _handle_commit_command(self, cmd_original: str) -> None:
        """Handle /commit — queue the review-and-commit task as the next turn.

        Uses the same one-shot ``_pending_agent_seed`` the interactive loop
        already consumes for /blueprint, so the agent runs it as a normal turn
        with its normal tools and approvals.
        """
        parts = (cmd_original or "").strip().split(None, 1)
        extra = parts[1] if len(parts) > 1 else ""
        self._pending_agent_seed = self.build_commit_prompt(extra)

    def _handle_focus_command(self, cmd_original: str) -> None:
        """Toggle or inspect focus view — the reduced-output display mode.

        Usage:
            /focus            → toggle
            /focus on|off     → explicit
            /focus status     → show current state

        Focus view is a DISPLAY-ONLY mode.  It composes with the existing
        ``/verbose`` tool-progress machinery rather than adding a second
        suppression mechanism: turning it on snaps ``tool_progress_mode`` to
        ``"off"`` (the same value ``/verbose off`` uses, honoured by
        ``agent/tool_executor.py`` and ``_on_tool_progress``) after stashing
        whatever mode the user had, and turning it off restores that mode
        verbatim.  On top of that it adds the two things ``/verbose off``
        lacks: a per-turn hidden-line count with a recovery hint, and a
        persistent ``focus`` segment in the status bar.

        Nothing here touches conversation history, the system prompt, or any
        request payload — the model sees an identical turn either way.
        """
        from cli import _cprint, save_config_value
        from son_of_anton_cli.colors import Colors as _Colors
        from son_of_anton_cli.focus_view import (
            FOCUS_CONFIG_KEY,
            FOCUS_TOOL_PROGRESS_MODE,
            format_focus_status,
            format_focus_toggle_message,
            normalize_tool_progress_mode,
            resolve_focus_arg,
        )

        arg = ""
        try:
            parts = (cmd_original or "").strip().split(None, 1)
            if len(parts) > 1:
                arg = parts[1].strip()
        except Exception:
            arg = ""

        current = bool(getattr(self, "_focus_view_enabled", False))
        action, target = resolve_focus_arg(arg, current)

        if action == "usage":
            _cprint("  Usage: /focus [on|off|status]")
            return

        # The mode /focus off will restore. While focus is ON the live
        # tool_progress_mode is "off", so the pre-focus mode is the stash.
        restore_mode = normalize_tool_progress_mode(
            getattr(self, "_focus_saved_tool_progress", None)
            if current
            else getattr(self, "tool_progress_mode", "all")
        )

        if action == "status":
            body = format_focus_status(current, restore_mode)
            head, _, tail = body.partition("\n")
            label, _, rest = head.partition(":")
            state_color = _Colors.GREEN if current else _Colors.DIM
            _cprint(
                f"  {_Colors.BOLD}{label}:{_Colors.RESET}"
                f"{state_color}{rest}{_Colors.RESET}"
                + (f"\n{_Colors.DIM}  {tail.strip()}{_Colors.RESET}" if tail else "")
            )
            return

        if target == current:
            # Idempotent explicit set — report without rewriting config.
            _cprint(f"  {format_focus_toggle_message(current, restore_mode)}")
            return

        if target:
            # Stash the user's configured mode, then reuse the EXISTING
            # suppression path by snapping to "off".
            self._focus_saved_tool_progress = restore_mode
            self._set_tool_progress_mode(FOCUS_TOOL_PROGRESS_MODE)
        else:
            self._set_tool_progress_mode(restore_mode)
            self._focus_saved_tool_progress = None

        self._focus_view_enabled = bool(target)
        self._focus_hidden_lines = 0
        save_config_value(FOCUS_CONFIG_KEY, bool(target))

        state = (
            f"{_Colors.GREEN}enabled{_Colors.RESET}" if target
            else f"{_Colors.DIM}disabled{_Colors.RESET}"
        )
        message = format_focus_toggle_message(bool(target), restore_mode)
        # Re-colour just the enabled/disabled word so the line matches siblings.
        for word in ("enabled", "disabled"):
            if word in message:
                message = message.replace(word, state, 1)
                break
        _cprint(f"  {message}")

    def _set_tool_progress_mode(self, mode: str) -> None:
        """Set the live tool-progress mode on both the CLI and the agent.

        Extracted so ``/focus`` and ``/verbose`` share one write path — the
        agent copy is what ``agent/tool_executor.py`` gates on, and forgetting
        it means the new mode only takes effect after an agent rebuild.
        """
        from son_of_anton_cli.focus_view import normalize_tool_progress_mode

        normalized = normalize_tool_progress_mode(mode)
        self.tool_progress_mode = normalized
        agent = getattr(self, "agent", None)
        if agent is not None:
            try:
                agent.tool_progress_mode = normalized
            except Exception:
                pass

    def _note_focus_hidden_line(self, function_name: str) -> None:
        """Count one tool line that focus view is suppressing this turn.

        Counted against the mode the user had BEFORE focus snapped things to
        "off", so a user who already ran ``/verbose off`` is never told that
        focus hid lines it did not hide.
        """
        if not getattr(self, "_focus_view_enabled", False):
            return
        from son_of_anton_cli.focus_view import would_display_tool_line

        saved = getattr(self, "_focus_saved_tool_progress", None)
        last = getattr(self, "_focus_last_counted_tool", None)
        if not would_display_tool_line(saved, function_name, last):
            return
        self._focus_last_counted_tool = function_name
        self._focus_hidden_lines = int(getattr(self, "_focus_hidden_lines", 0)) + 1

    def _emit_focus_recovery_line(self) -> None:
        """Print the dim post-turn recovery line and reset the counter."""
        count = int(getattr(self, "_focus_hidden_lines", 0) or 0)
        self._focus_hidden_lines = 0
        self._focus_last_counted_tool = None
        if not getattr(self, "_focus_view_enabled", False):
            return
        from son_of_anton_cli.focus_view import format_hidden_line

        line = format_hidden_line(count)
        if not line:
            return
        try:
            from cli import _DIM, _RST, _cprint

            _cprint(f"  {_DIM}{line}{_RST}")
        except Exception:
            pass

    def _handle_approvals_command(self, cmd_original: str) -> None:
        """Show or persist the profile-wide dangerous-command approval mode."""
        from cli import _cprint
        from son_of_anton_cli.approval_mode import run_approval_mode_command

        parts = (cmd_original or "").strip().split(None, 1)
        requested = parts[1] if len(parts) > 1 else None
        result = run_approval_mode_command(requested)
        _cprint(f"  {result.message}")


    def _handle_timestamps_command(self, cmd_original: str) -> None:
        """Toggle or inspect ``display.timestamps`` from the CLI.

        When on, submitted and streamed message labels carry an ``[HH:MM]``
        suffix and ``/history`` prefixes each turn with its time (for turns
        that carry a stored timestamp).

        Usage:
            /timestamps           → toggle
            /timestamps on|off    → explicit
            /timestamps status    → show current state
        """
        from cli import _cprint, save_config_value
        from son_of_anton_cli.colors import Colors as _Colors

        arg = ""
        try:
            parts = (cmd_original or "").strip().split(None, 1)
            if len(parts) > 1:
                arg = parts[1].strip().lower()
        except Exception:
            arg = ""

        current = bool(getattr(self, "show_timestamps", False))

        if arg in {"status", "?"}:
            state = "ON" if current else "OFF"
            _cprint(f"  {_Colors.BOLD}Message timestamps:{_Colors.RESET} {state}")
            return

        if arg in {"on", "enable", "true", "1"}:
            new_state = True
        elif arg in {"off", "disable", "false", "0"}:
            new_state = False
        elif arg == "":
            new_state = not current
        else:
            _cprint("  Usage: /timestamps [on|off|status]")
            return

        self.show_timestamps = new_state
        if save_config_value("display.timestamps", new_state):
            state = (
                f"{_Colors.GREEN}ON{_Colors.RESET}" if new_state
                else f"{_Colors.DIM}OFF{_Colors.RESET}"
            )
            _cprint(f"  Message timestamps: {state}")
        else:
            _cprint("  Failed to save timestamps setting to config.yaml")

    def _handle_reasoning_command(self, cmd: str):
        """Handle /reasoning — manage effort level and display toggle.

        Usage:
            /reasoning              Show current effort level and display state
            /reasoning <level>      Set effort for this session only (none, minimal, low, medium, high, xhigh, max, ultra)
            /reasoning <level> --global  Persist reasoning effort to config.yaml
            /reasoning show|on      Show model thinking/reasoning in output
            /reasoning hide|off     Hide model thinking/reasoning from output
            /reasoning full         Show complete thinking (no 10-line clamp)
            /reasoning clamp        Collapse long thinking to the first 10 lines
        """
        from cli import CLI_CONFIG, _ACCENT, _DIM, _RST, _cprint, _parse_reasoning_config, save_config_value
        parts = cmd.strip().split(maxsplit=1)

        if len(parts) < 2:
            # Show current state
            rc = self.reasoning_config
            if rc is None:
                level = "medium (default)"
            elif rc.get("enabled") is False:
                level = "none (disabled)"
            else:
                level = rc.get("effort", "medium")
            display_state = "on ✓" if self.show_reasoning else "off"
            full_state = "full" if getattr(self, "reasoning_full", False) else "clamped to 10 lines"
            _cprint(f"  {_ACCENT}Reasoning effort:  {level}{_RST}")
            _cprint(f"  {_ACCENT}Reasoning display: {display_state} ({full_state}){_RST}")
            _cprint(f"  {_DIM}Usage: /reasoning <none|minimal|low|medium|high|xhigh|max|ultra|show|hide|full|clamp> [--global]{_RST}")
            return

        arg = parts[1].strip().lower()
        arg_tokens = arg.split()
        # Session scope is the default; --global opts into persisting to
        # config.yaml. --session is accepted as an explicit no-op for parity
        # with /model and the gateway /reasoning handler.
        explicit_global = "--global" in arg_tokens
        if explicit_global or "--session" in arg_tokens:
            arg = " ".join(
                token for token in arg_tokens
                if token not in ("--global", "--session")
            )

        # Display toggle
        if arg in {"show", "on"}:
            self.show_reasoning = True
            if self.agent:
                self.agent.reasoning_callback = self._current_reasoning_callback()
            save_config_value("display.show_reasoning", True)
            _cprint(f"  {_ACCENT}✓ Reasoning display: ON (saved){_RST}")
            _cprint(f"  {_DIM}  Model thinking will be shown during and after each response.{_RST}")
            return
        if arg in {"hide", "off"}:
            self.show_reasoning = False
            if self.agent:
                self.agent.reasoning_callback = self._current_reasoning_callback()
            save_config_value("display.show_reasoning", False)
            _cprint(f"  {_ACCENT}✓ Reasoning display: OFF (saved){_RST}")
            return

        # Full / clamped recap toggle
        if arg in {"full", "all"}:
            self.reasoning_full = True
            save_config_value("display.reasoning_full", True)
            _cprint(f"  {_ACCENT}✓ Reasoning display: FULL (saved){_RST}")
            _cprint(f"  {_DIM}  The post-response recap box will print complete thinking.{_RST}")
            if not self.show_reasoning:
                _cprint(f"  {_DIM}  Note: reasoning display is OFF — run /reasoning show to see it.{_RST}")
            return
        if arg in {"clamp", "collapse", "short"}:
            self.reasoning_full = False
            save_config_value("display.reasoning_full", False)
            _cprint(f"  {_ACCENT}✓ Reasoning display: CLAMPED to 10 lines (saved){_RST}")
            return

        # Effort level change
        parsed = _parse_reasoning_config(arg)
        if parsed is None:
            _cprint(f"  {_DIM}(._.) Unknown argument: {arg}{_RST}")
            _cprint(f"  {_DIM}Valid levels: none, minimal, low, medium, high, xhigh, max, ultra{_RST}")
            _cprint(f"  {_DIM}Display:      show, hide{_RST}")
            _cprint(f"  {_DIM}Scope:        session-scoped by default, --global to persist{_RST}")
            return

        self.reasoning_config = parsed
        self.agent = None  # Force agent re-init with new reasoning config

        if explicit_global and save_config_value("agent.reasoning_effort", arg):
            agent_cfg = CLI_CONFIG.get("agent")
            if not isinstance(agent_cfg, dict):
                agent_cfg = {}
                CLI_CONFIG["agent"] = agent_cfg
            agent_cfg["reasoning_effort"] = arg
            _cprint(f"  {_ACCENT}✓ Reasoning effort set to '{arg}' (saved to config){_RST}")
        elif explicit_global:
            _cprint(f"  {_ACCENT}✓ Reasoning effort set to '{arg}' (session only; config save failed){_RST}")
        else:
            _cprint(f"  {_ACCENT}✓ Reasoning effort set to '{arg}' (this session — use --global to persist){_RST}")

    def _handle_busy_command(self, cmd: str):
        """Handle /busy — control what Enter does while Son of Anton is working.

        Usage:
            /busy               Show current busy input mode
            /busy status        Show current busy input mode
            /busy queue         Queue input for the next turn instead of interrupting
            /busy steer         Inject Enter mid-run via /steer (after next tool call)
            /busy interrupt     Redirect the current run on Enter (default)
        """
        from cli import _ACCENT, _DIM, _RST, _cprint, save_config_value
        parts = cmd.strip().split(maxsplit=1)
        if len(parts) < 2 or parts[1].strip().lower() == "status":
            _cprint(f"  {_ACCENT}Busy input mode: {self.busy_input_mode}{_RST}")
            if self.busy_input_mode == "queue":
                _behavior = "queues for next turn"
            elif self.busy_input_mode == "steer":
                _behavior = "steers into current run (after next tool call)"
            else:
                _behavior = "redirects current run immediately"
            _cprint(f"  {_DIM}Enter while busy: {_behavior}{_RST}")
            _cprint(f"  {_DIM}Usage: /busy [queue|steer|interrupt|status]{_RST}")
            return

        arg = parts[1].strip().lower()
        if arg not in {"queue", "interrupt", "steer"}:
            _cprint(f"  {_DIM}(._.) Unknown argument: {arg}{_RST}")
            _cprint(f"  {_DIM}Usage: /busy [queue|steer|interrupt|status]{_RST}")
            return

        self.busy_input_mode = arg
        if save_config_value("display.busy_input_mode", arg):
            if arg == "queue":
                behavior = "Enter will queue follow-up input while Son of Anton is busy."
            elif arg == "steer":
                behavior = "Enter will steer your message into the current run (after the next tool call)."
            else:
                behavior = "Enter will redirect the current run while Son of Anton is busy; /stop still cancels it."
            _cprint(f"  {_ACCENT}✓ Busy input mode set to '{arg}' (saved to config){_RST}")
            _cprint(f"  {_DIM}{behavior}{_RST}")
        else:
            _cprint(f"  {_ACCENT}✓ Busy input mode set to '{arg}' (session only){_RST}")





    def _handle_voice_command(self, command: str):
        """Handle /voice [on|off|tts|status] command."""
        from cli import _cprint
        parts = command.strip().split(maxsplit=1)
        subcommand = parts[1].lower().strip() if len(parts) > 1 else ""

        if subcommand == "on":
            self._enable_voice_mode()
        elif subcommand == "off":
            self._disable_voice_mode()
        elif subcommand == "tts":
            self._toggle_voice_tts()
        elif subcommand == "status":
            self._show_voice_status()
        elif subcommand == "":
            # Toggle
            if self._voice_mode:
                self._disable_voice_mode()
            else:
                self._enable_voice_mode()
        else:
            _cprint(f"Unknown voice subcommand: {subcommand}")
            _cprint("Usage: /voice [on|off|tts|status]")

    def _handle_wake_command(self, command: str):
        """Handle /wake [on|off|status] — the 'Hey Son of Anton' hotword listener.

        The toggle IS the config: an explicit on/off (or bare toggle) also
        writes ``wake_word.enabled`` to config.yaml so the choice persists
        across sessions. Startup auto-arm (_maybe_start_wake_word) only reads.
        """
        from cli import _cprint
        parts = command.strip().split(maxsplit=1)
        subcommand = parts[1].lower().strip() if len(parts) > 1 else ""

        if subcommand == "on":
            if self._start_wake_word_listener(announce=True):
                self._persist_wake_word_enabled(True)
        elif subcommand == "off":
            self._stop_wake_word_listener(announce=True)
            self._persist_wake_word_enabled(False)
        elif subcommand in ("", "status"):
            if subcommand == "":
                # Bare /wake toggles.
                if getattr(self, "_wake_word_active", False):
                    self._stop_wake_word_listener(announce=True)
                    self._persist_wake_word_enabled(False)
                elif self._start_wake_word_listener(announce=True):
                    self._persist_wake_word_enabled(True)
            else:
                self._show_wake_word_status()
        else:
            _cprint(f"Unknown wake subcommand: {subcommand}")
            _cprint("Usage: /wake [on|off|status]")

    def _persist_wake_word_enabled(self, enabled: bool):
        """Save ``wake_word.enabled`` so the /wake toggle sticks for future sessions."""
        from cli import _cprint, _DIM, _RST, save_config_value

        try:
            from tools.wake_word import load_wake_word_config

            if bool(load_wake_word_config().get("enabled")) == enabled:
                return  # already persisted — don't rewrite config or re-announce
        except Exception:
            pass
        if save_config_value("wake_word.enabled", enabled):
            _cprint(f"{_DIM}Wake word {'enabled' if enabled else 'disabled'} in config "
                    f"(wake_word.enabled: {str(enabled).lower()}).{_RST}")
